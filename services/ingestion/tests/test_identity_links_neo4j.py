"""Neo4j 5.26 integration coverage for the C3 identity-link writers."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import cast
from unittest.mock import MagicMock
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, ManagedTransaction, Record
from src.golden_profile import compute_golden_profile
from src.graph import queries
from src.graph.client import Neo4jClient
from src.graph.person_list_summary_deltas import PersonListSummaryDeltaCollector
from src.graph.schema_init import apply_schema
from src.identity_link_promotion import (
    anchor_person_by_nric,
    promote_profile,
    promote_profiles_for_nric,
)
from src.identity_link_writes import (
    fetch_cluster_key_closure,
    fetch_record_identifier_keys,
    lock_identifiers,
    materialize_profile_for_record,
    recompute_participant_counts,
    recompute_participant_links,
)
from src.identity_links import (
    IdentifierKey,
    IdentityLinkConflictError,
    MaterializationOutcome,
    PromotionOutcome,
    link_id,
)
from src.matching.deterministic import evaluate_deterministic
from src.matching.snapshot import fetch_candidate_snapshots
from src.models import (
    CandidateResult,
    IdentityCandidate,
    MatchDecision,
    MatchResult,
    NormalizedIdentifier,
    QualityFlag,
    RecordType,
    SourceRecordEnvelope,
)
from src.nric_anchor import NameDob
from src.pipeline import IngestPipeline
from src.pipeline_person_merge import merge_person_pair
from src.pipeline_writes import find_identity_candidates
from src.record_lifecycle import load_locked_source_state

_ENV_PREFIX = "HYPERP_NEO4J_CONTROL_MIGRATION_TEST"
_NRIC_DIGEST = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_OTHER_DIGEST = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
_PHONE = "+6591234567"


def _configuration() -> tuple[str, str, str, str] | None:
    uri = os.getenv(f"{_ENV_PREFIX}_URI")
    user = os.getenv(f"{_ENV_PREFIX}_USER")
    password = os.getenv(f"{_ENV_PREFIX}_PASSWORD")
    service_host = os.getenv(f"{_ENV_PREFIX}_SERVICE_HOST")
    values = (uri, user, password, service_host)
    if all(value is None for value in values):
        return None
    if any(value is None or not value.strip() for value in values):
        pytest.fail("identity link test requires complete disposable Neo4j configuration")
    assert uri is not None
    assert user is not None
    assert password is not None
    assert service_host is not None
    return uri, user, password, service_host


def _validate_target(uri: str, service_host: str) -> None:
    parsed = urlparse(uri)
    allowed_hosts = {"localhost", "127.0.0.1", "::1", service_host}
    if parsed.scheme != "bolt" or parsed.hostname not in allowed_hosts:
        pytest.fail("identity link test requires an explicitly disposable Bolt target")
    if parsed.username is not None or parsed.password is not None:
        pytest.fail("identity link test URI must not embed credentials")


def _require_neo4j_526(driver: Driver) -> None:
    with driver.session() as session:
        row = session.run(
            "CALL dbms.components() YIELD versions RETURN versions[0] AS version"
        ).single(strict=True)
    version = row["version"]
    if not isinstance(version, str) or version.split(".")[:2] != ["5", "26"]:
        pytest.fail("identity link test requires Neo4j 5.26")


@pytest.fixture
def neo4j_driver() -> Iterator[Driver]:
    configuration = _configuration()
    if configuration is None:
        pytest.skip("disposable Neo4j control migration database is not configured")
    uri, user, password, service_host = configuration
    _validate_target(uri, service_host)
    driver = GraphDatabase.driver(uri, auth=(user, password))
    cleanup_owned = False
    try:
        driver.verify_connectivity()
        _require_neo4j_526(driver)
        with driver.session() as session:
            count = session.run(
                "MATCH (node) WHERE NOT node:PersonListSummaryCounter RETURN count(node) AS count"
            ).single(strict=True)["count"]
        if count != 0:
            pytest.fail("identity link test database must be empty")
        cleanup_owned = True
        yield driver
    finally:
        try:
            if cleanup_owned:
                with driver.session() as session:
                    session.run(
                        "MATCH (node) WHERE NOT node:PersonListSummaryCounter DETACH DELETE node"
                    ).consume()
        finally:
            driver.close()


class _Client(Neo4jClient):
    def __init__(self, driver: Driver) -> None:
        self._driver = driver
        self._bounded_timeout_seconds = None


def _apply_schema(driver: Driver) -> None:
    apply_schema(_Client(driver))


def _seed_source_system(driver: Driver) -> None:
    field_trust = json.dumps({"nric": "tier_4", "phone": "tier_1", "email": "tier_3"})
    with driver.session() as session:
        session.run(
            "MERGE (system:SourceSystem {source_key: 'onediver'}) "
            "SET system.field_trust = $field_trust",
            field_trust=field_trust,
        ).consume()


def _seed_identifier(
    driver: Driver,
    identifier_id: str,
    identifier_type: str,
    normalized_value: str,
) -> None:
    with driver.session() as session:
        session.run(
            "MERGE (identifier:Identifier {identifier_type: $identifier_type, "
            "identifier_scope: 'global', normalized_value: $normalized_value}) "
            "SET identifier.identifier_id = $identifier_id",
            identifier_id=identifier_id,
            identifier_type=identifier_type,
            normalized_value=normalized_value,
        ).consume()


def _seed_parked_record(
    driver: Driver,
    source_record_pk: str,
    observed_at: str,
    *,
    source_record_id: str | None = None,
    source_record_version: int = 1,
    normalized_payload: str | None = None,
) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (system:SourceSystem {source_key: 'onediver'}) "
            "CREATE (record:SourceRecord {source_record_pk: $source_record_pk, "
            "source_record_id: $source_record_id, "
            "source_instance_id: 'legacy-default', "
            "source_record_version: $source_record_version, "
            "lifecycle_status: 'parked', link_status: 'unlinked', "
            "attribution_status: 'parked', reeval_pending: true, "
            "observed_at: datetime($observed_at), "
            "normalized_payload: $normalized_payload})-[:FROM_SOURCE]->(system)",
            source_record_pk=source_record_pk,
            source_record_id=source_record_id or source_record_pk,
            source_record_version=source_record_version,
            normalized_payload=normalized_payload,
            observed_at=observed_at,
        ).consume()


def _seed_assertion(
    driver: Driver,
    source_record_pk: str,
    identifier_id: str,
    assertion_id: str,
    *,
    quality_flag: str | None = "valid",
    is_verified: bool | None = True,
) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (record:SourceRecord {source_record_pk: $source_record_pk}) "
            "MATCH (identifier:Identifier {identifier_id: $identifier_id}) "
            "MERGE (record)-[assertion:ASSERTS {assertion_id: $assertion_id}]->(identifier) "
            "ON CREATE SET assertion.field_path = 'identifiers', assertion.role = 'subject', "
            "assertion.quality_flag = $quality_flag, assertion.is_verified = $is_verified, "
            "assertion.created_at = datetime()",
            source_record_pk=source_record_pk,
            identifier_id=identifier_id,
            assertion_id=assertion_id,
            quality_flag=quality_flag,
            is_verified=is_verified,
        ).consume()


def _seed_profile(driver: Driver, profile_id: str, member_pk: str) -> None:
    with driver.session() as session:
        session.run(
            "CREATE (profile:Profile {profile_id: $profile_id, status: 'open', "
            "evidence_version: 1, "
            "materialized_at: datetime('2026-01-01T00:00:00Z')}) "
            "WITH profile "
            "MATCH (record:SourceRecord {source_record_pk: $member_pk}) "
            "SET record.link_status = 'profile', record.attribution_status = 'profile', "
            "record.reeval_pending = false "
            "MERGE (record)-[:IN_PROFILE]->(profile)",
            profile_id=profile_id,
            member_pk=member_pk,
        ).consume()


def _seed_profile_link(driver: Driver, profile_id: str, identifier_id: str) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (profile:Profile {profile_id: $profile_id}) "
            "MATCH (identifier:Identifier {identifier_id: $identifier_id}) "
            "CREATE (profile)-[:HAS_IDENTIFIER {link_id: $link_id, "
            "assertion_count: 1, source_count: 1, verified_count: 1, max_tier: 'tier_4', "
            "score: 0.0, exclusivity: 0.0, rank_score: 0.0, basis: 'ASSERTS', "
            "status: 'active', policy_version: 'c3-provisional', computed_at: datetime()}]"
            "->(identifier)",
            profile_id=profile_id,
            identifier_id=identifier_id,
            link_id=link_id(profile_id, identifier_id),
        ).consume()


def _nric_identifier(digest: str = _NRIC_DIGEST) -> NormalizedIdentifier:
    return NormalizedIdentifier(
        identifier_type="nric",
        normalized_value=digest,
        is_verified=True,
        quality_flag=QualityFlag.VALID,
        checksum_valid=True,
    )


def _execute[T](driver: Driver, work: Callable[[ManagedTransaction], T]) -> T:
    with driver.session() as session:
        return session.execute_write(work)


def _materialize(driver: Driver, source_record_pk: str) -> MaterializationOutcome:
    return _execute(driver, lambda tx: materialize_profile_for_record(tx, source_record_pk))


def _link_snapshot(driver: Driver) -> list[dict[str, object]]:
    with driver.session() as session:
        rows = [
            dict(row)
            for row in session.run(
                "MATCH (participant)-[link:HAS_IDENTIFIER]->(identifier:Identifier) "
                "RETURN coalesce(participant.person_id, participant.profile_id) AS participant_id, "
                "identifier.identifier_id AS identifier_id, "
                "properties(link) AS properties"
            )
        ]
    return sorted(rows, key=lambda row: (str(row["participant_id"]), str(row["identifier_id"])))


def _profile_snapshot(driver: Driver) -> list[dict[str, object]]:
    with driver.session() as session:
        rows = [
            dict(row)
            for row in session.run(
                "MATCH (profile:Profile) "
                "RETURN profile.profile_id AS profile_id, profile.status AS status, "
                "profile.evidence_version AS evidence_version, "
                "profile.materialized_at AS materialized_at, "
                "profile.updated_at AS updated_at, "
                "profile.promoted_person_id AS promoted_person_id"
            )
        ]
    return sorted(rows, key=lambda row: str(row["profile_id"]))


def _scalar(driver: Driver, query: str, **parameters: object) -> int:
    with driver.session() as session:
        row: Record = session.run(query, **parameters).single(strict=True)
        value = row["count"]
    assert isinstance(value, int)
    return value


def test_materialization_and_link_recompute_are_zero_diff(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_parked_record(neo4j_driver, "sr-1", "2026-01-01T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-1", "ident-phone", "assert-1")

    first = _materialize(neo4j_driver, "sr-1")
    assert first.status == "parked"
    assert _scalar(neo4j_driver, "MATCH (profile:Profile) RETURN count(profile) AS count") == 0

    _seed_parked_record(neo4j_driver, "sr-2", "2026-02-01T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-2", "ident-phone", "assert-2")
    created = _materialize(neo4j_driver, "sr-2")
    assert created.status == "created"
    profile_id = created.profile_id
    assert profile_id is not None

    links = _link_snapshot(neo4j_driver)
    assert len(links) == 1
    properties = links[0]["properties"]
    assert isinstance(properties, dict)
    assert properties["assertion_count"] == 2
    assert properties["source_count"] == 2
    assert properties["verified_count"] == 2
    # The phone tier comes from the JSON field_trust string stored on SourceSystem.
    assert properties["max_tier"] == "tier_1"
    assert properties["first_seen_at"] == "2026-01-01T00:00:00Z"
    assert properties["last_seen_at"] == "2026-02-01T00:00:00Z"
    assert properties["last_confirmed_at"] == "2026-02-01T00:00:00Z"
    assert properties["score"] == 0.0
    assert properties["exclusivity"] == 0.0
    assert properties["rank_score"] == 0.0
    # Neo4j removes null-valued properties, so an unset golden_rank is absent.
    assert properties.get("golden_rank") is None
    assert properties["basis"] == "ASSERTS"
    assert properties["status"] == "active"
    assert properties["policy_version"] == "c3-provisional"

    before_links = _link_snapshot(neo4j_driver)
    before_profiles = _profile_snapshot(neo4j_driver)
    _materialize(neo4j_driver, "sr-2")
    _execute(
        neo4j_driver,
        lambda tx: recompute_participant_links(tx, "profile", profile_id),
    )

    assert _link_snapshot(neo4j_driver) == before_links
    assert _profile_snapshot(neo4j_driver) == before_profiles
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (record:SourceRecord)-[:IN_PROFILE]->(:Profile) RETURN count(record) AS count",
        )
        == 2
    )


def test_bridged_profiles_union_to_lowest_id_and_retire_losers(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_identifier(neo4j_driver, "ident-email-a", "email", "a@example.com")
    _seed_identifier(neo4j_driver, "ident-email-b", "email", "b@example.com")
    _seed_parked_record(neo4j_driver, "sr-a", "2026-01-01T00:00:00Z")
    _seed_parked_record(neo4j_driver, "sr-b", "2026-02-01T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-a", "ident-phone", "assert-a1")
    _seed_assertion(neo4j_driver, "sr-a", "ident-email-a", "assert-a2")
    _seed_assertion(neo4j_driver, "sr-b", "ident-phone", "assert-b1")
    _seed_assertion(neo4j_driver, "sr-b", "ident-email-b", "assert-b2")
    _seed_profile(neo4j_driver, "profile-aaa", "sr-a")
    _seed_profile(neo4j_driver, "profile-bbb", "sr-b")
    _seed_profile_link(neo4j_driver, "profile-aaa", "ident-phone")
    _seed_profile_link(neo4j_driver, "profile-bbb", "ident-phone")

    outcome = _materialize(neo4j_driver, "sr-b")

    assert outcome.profile_id == "profile-aaa"
    assert outcome.absorbed_profile_ids == ("profile-bbb",)
    profiles = {str(row["profile_id"]): row for row in _profile_snapshot(neo4j_driver)}
    assert profiles["profile-aaa"]["status"] == "open"
    assert profiles["profile-bbb"]["status"] == "retired"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord)-[member:IN_PROFILE]->"
            "(profile:Profile {profile_id: 'profile-bbb'}) "
            "RETURN count(member) AS count",
        )
        == 0
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord)-[:IN_PROFILE]->(profile:Profile {profile_id: 'profile-aaa'}) "
            "RETURN count(*) AS count",
        )
        == 2
    )
    keeper_links = {
        str(link["identifier_id"])
        for link in _link_snapshot(neo4j_driver)
        if link["participant_id"] == "profile-aaa"
    }
    assert keeper_links == {"ident-phone", "ident-email-a", "ident-email-b"}
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:Profile {profile_id: 'profile-bbb'})-[link:HAS_IDENTIFIER]->() "
            "RETURN count(link) AS count",
        )
        == 0
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (identifier:Identifier {identifier_id: 'ident-phone'}) "
            "RETURN identifier.participant_count AS count",
        )
        # A growing Profile counts its two members (both IN_PROFILE the keeper).
        == 2
    )


def _promote(
    driver: Driver,
    profile_id: str,
    identifier: NormalizedIdentifier | None = None,
) -> PromotionOutcome:
    resolved = identifier or _nric_identifier()
    return _execute(
        driver,
        lambda tx: promote_profile(
            tx,
            profile_id=profile_id,
            identifier=resolved,
            source_key="onediver",
            record_type=RecordType.IDENTITY,
            incoming=NameDob(full_name="Alice Tan", dob="1990-01-01"),
        ),
    )


def test_gate_approved_promotion_is_complete_idempotent_and_one_way(
    neo4j_driver: Driver,
) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_parked_record(neo4j_driver, "sr-1", "2026-01-01T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-1", "ident-nric", "assert-nric")
    _seed_assertion(neo4j_driver, "sr-1", "ident-phone", "assert-phone")
    _seed_profile(neo4j_driver, "profile-1", "sr-1")

    rejected = _promote(
        neo4j_driver,
        "profile-1",
        NormalizedIdentifier(
            identifier_type="nric", normalized_value="S1234567D", checksum_valid=True
        ),
    )
    assert rejected.status == "rejected"
    assert rejected.reason == "no_key"

    outcome = _promote(neo4j_driver, "profile-1")
    assert outcome.status == "promoted"
    assert outcome.person_created is True
    person_id = outcome.person_id
    assert person_id is not None

    assert _scalar(neo4j_driver, "MATCH (person:Person) RETURN count(person) AS count") == 1
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:Person)-[:ANCHORED_BY]->(:Identifier {identifier_id: 'ident-nric'}) "
            "RETURN count(*) AS count",
        )
        == 1
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:Profile {profile_id: 'profile-1'})-[:PROMOTED_TO]->(:Person) "
            "RETURN count(*) AS count",
        )
        == 1
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-1'})-[:LINKED_TO]->(:Person) "
            "RETURN count(*) AS count",
        )
        == 1
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-1'})-[:IN_PROFILE]->"
            "(:Profile {profile_id: 'profile-1'}) RETURN count(*) AS count",
        )
        == 1
    )
    with neo4j_driver.session() as session:
        record_row = session.run(
            "MATCH (record:SourceRecord {source_record_pk: 'sr-1'}) "
            "RETURN record.link_status AS link_status, "
            "record.attribution_status AS attribution_status, "
            "record.reeval_pending AS reeval_pending"
        ).single(strict=True)
    assert record_row["link_status"] == "linked"
    assert record_row["attribution_status"] == "person"
    assert record_row["reeval_pending"] is False
    profiles = {str(row["profile_id"]): row for row in _profile_snapshot(neo4j_driver)}
    assert profiles["profile-1"]["status"] == "promoted"
    assert profiles["profile-1"]["promoted_person_id"] == person_id
    person_link_properties = {
        str(link["identifier_id"]): link["properties"]
        for link in _link_snapshot(neo4j_driver)
        if link["participant_id"] == person_id
    }
    assert set(person_link_properties) == {"ident-nric", "ident-phone"}
    phone_properties = person_link_properties["ident-phone"]
    assert isinstance(phone_properties, dict)
    assert phone_properties["max_tier"] == "tier_1"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (identifier:Identifier {identifier_id: 'ident-nric'}) "
            "RETURN identifier.participant_count AS count",
        )
        == 1
    )

    before_links = _link_snapshot(neo4j_driver)
    before_profiles = _profile_snapshot(neo4j_driver)
    retry = _promote(neo4j_driver, "profile-1")
    assert retry.status == "already_promoted"
    assert retry.person_id == person_id
    assert _link_snapshot(neo4j_driver) == before_links
    assert _profile_snapshot(neo4j_driver) == before_profiles
    assert _scalar(neo4j_driver, "MATCH (person:Person) RETURN count(person) AS count") == 1

    with pytest.raises(IdentityLinkConflictError):
        _promote(neo4j_driver, "profile-1", _nric_identifier(_OTHER_DIGEST))


def test_concurrent_promotion_converges_on_one_anchor(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    for source_record_pk, profile_id in (("sr-a", "profile-a"), ("sr-b", "profile-b")):
        _seed_parked_record(neo4j_driver, source_record_pk, "2026-01-01T00:00:00Z")
        _seed_assertion(neo4j_driver, source_record_pk, "ident-nric", f"assert-{profile_id}-nric")
        _seed_assertion(neo4j_driver, source_record_pk, "ident-phone", f"assert-{profile_id}-phone")
        _seed_profile(neo4j_driver, profile_id, source_record_pk)

    forward = [
        IdentifierKey(
            identifier_type="nric", identifier_scope="global", normalized_value=_NRIC_DIGEST
        ),
        IdentifierKey(identifier_type="phone", identifier_scope="global", normalized_value=_PHONE),
    ]
    barrier = threading.Barrier(2)

    def _promote_in_transaction(profile_id: str, keys: list[IdentifierKey]) -> PromotionOutcome:
        barrier.wait(timeout=10)
        with neo4j_driver.session() as session:
            return session.execute_write(lambda tx: _locked_promotion(tx, profile_id, keys))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_promote_in_transaction, "profile-a", forward)
        second = pool.submit(_promote_in_transaction, "profile-b", list(reversed(forward)))
        outcomes = [first.result(timeout=30), second.result(timeout=30)]

    assert sorted(str(outcome.status) for outcome in outcomes) == ["promoted", "promoted"]
    assert _scalar(neo4j_driver, "MATCH (person:Person) RETURN count(person) AS count") == 1
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:Person)-[:ANCHORED_BY]->(:Identifier {identifier_id: 'ident-nric'}) "
            "RETURN count(*) AS count",
        )
        == 1
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:Profile)-[link:HAS_IDENTIFIER]->(:Identifier) "
            "WITH link.link_id AS link_id, count(*) AS duplicates "
            "WHERE duplicates > 1 RETURN count(link_id) AS count",
        )
        == 0
    )
    assert _scalar(neo4j_driver, "MATCH (profile:Profile) RETURN count(profile) AS count") == 2
    with neo4j_driver.session() as session:
        person_ids = {
            row["promoted_person_id"]
            for row in session.run(
                "MATCH (profile:Profile) RETURN profile.promoted_person_id AS promoted_person_id"
            )
        }
    assert len(person_ids) == 1


def _locked_promotion(
    tx: ManagedTransaction,
    profile_id: str,
    keys: list[IdentifierKey],
) -> PromotionOutcome:
    lock_identifiers(tx, keys)
    return promote_profile(
        tx,
        profile_id=profile_id,
        identifier=_nric_identifier(),
        source_key="onediver",
        record_type=RecordType.IDENTITY,
        incoming=NameDob(full_name="Alice Tan", dob="1990-01-01"),
    )


def _seed_person(driver: Driver, person_id: str) -> None:
    with driver.session() as session:
        session.run(
            "CREATE (person:Person {person_id: $person_id, status: 'active'})",
            person_id=person_id,
        ).consume()


def _seed_person_has_link(
    driver: Driver,
    person_id: str,
    identifier_id: str,
    *,
    verified_count: int = 1,
) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (person:Person {person_id: $person_id}) "
            "MATCH (identifier:Identifier {identifier_id: $identifier_id}) "
            "CREATE (person)-[:HAS_IDENTIFIER {link_id: $link_id, status: 'active', "
            "computed_at: datetime(), verified_count: $verified_count}]->(identifier)",
            person_id=person_id,
            identifier_id=identifier_id,
            link_id=link_id(person_id, identifier_id),
            verified_count=verified_count,
        ).consume()


def _phone_identifier(value: str = _PHONE) -> NormalizedIdentifier:
    return NormalizedIdentifier(
        identifier_type="phone", normalized_value=value, quality_flag=QualityFlag.VALID
    )


def _participants(
    driver: Driver,
    identifiers: list[NormalizedIdentifier],
    source_record_pk: str,
) -> tuple[list[CandidateResult], list[IdentityCandidate]]:
    return _execute(
        driver,
        lambda tx: find_identity_candidates(tx, identifiers, source_record_pk),
    )


def test_parked_participant_is_returned_but_profile_members_are_not_unlinked(
    neo4j_driver: Driver,
) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_parked_record(neo4j_driver, "sr-parked", "2026-01-01T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-parked", "ident-phone", "assert-parked")
    _seed_parked_record(neo4j_driver, "sr-incoming", "2026-01-02T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-incoming", "ident-phone", "assert-incoming")
    _seed_parked_record(neo4j_driver, "sr-member", "2026-01-03T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-member", "ident-phone", "assert-member")
    _seed_profile(neo4j_driver, "profile-1", "sr-member")
    _seed_profile_link(neo4j_driver, "profile-1", "ident-phone")

    _people, others = _participants(neo4j_driver, [_phone_identifier()], "sr-incoming")

    participants = {(item.participant_kind, item.participant_id) for item in others}
    assert ("source_record", "sr-parked") in participants
    assert ("profile", "profile-1") in participants
    assert ("source_record", "sr-incoming") not in participants
    assert ("source_record", "sr-member") not in participants


def test_digest_nric_recall_finds_anchored_person_and_merges_without_plaintext(
    neo4j_driver: Driver,
) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_person(neo4j_driver, "person-anchored")
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_person_has_link(neo4j_driver, "person-anchored", "ident-nric")

    people, _others = _participants(neo4j_driver, [_nric_identifier()], "sr-incoming")
    assert [candidate.person_id for candidate in people] == ["person-anchored"]

    result = _execute(
        neo4j_driver,
        lambda tx: evaluate_deterministic(
            tx,
            "person-anchored",
            [_nric_identifier()],
            [],
            RecordType.IDENTITY,
            v2_evidence=True,
            include_plaintext_carrier=False,
        ),
    )
    assert result is not None
    assert result.decision is MatchDecision.MERGE


def test_over_cap_hub_only_corroborates_a_direct_key(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_identifier(neo4j_driver, "ident-email", "email", "hub@example.com")
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (phone:Identifier {identifier_id: 'ident-phone'}) "
            "MATCH (email:Identifier {identifier_id: 'ident-email'}) "
            "SET phone.participant_count = 100, email.participant_count = 1"
        ).consume()
    _seed_person(neo4j_driver, "person-corroborated")
    _seed_person(neo4j_driver, "person-hub-only")
    _seed_person_has_link(neo4j_driver, "person-corroborated", "ident-phone")
    _seed_person_has_link(neo4j_driver, "person-corroborated", "ident-email")
    _seed_person_has_link(neo4j_driver, "person-hub-only", "ident-phone")

    people, _others = _participants(
        neo4j_driver,
        [
            _phone_identifier(),
            NormalizedIdentifier(
                identifier_type="email",
                normalized_value="hub@example.com",
                quality_flag=QualityFlag.VALID,
            ),
        ],
        "sr-incoming",
    )

    assert [candidate.person_id for candidate in people] == ["person-corroborated"]


def test_participant_candidates_are_capped_at_one_hundred(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-limit", "phone", "+6599999999")
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (system:SourceSystem {source_key: 'onediver'}) "
            "UNWIND range(1, 101) AS index "
            "CREATE (record:SourceRecord {source_record_pk: 'sr-limit-' + toString(index), "
            "lifecycle_status: 'parked', link_status: 'unlinked', "
            "attribution_status: 'parked', reeval_pending: true, "
            "observed_at: datetime('2026-01-01T00:00:00Z')})-[:FROM_SOURCE]->(system)"
        ).consume()
        session.run(
            "MATCH (identifier:Identifier {identifier_id: 'ident-limit'}) "
            "UNWIND range(1, 101) AS index "
            "MATCH (record:SourceRecord {source_record_pk: 'sr-limit-' + toString(index)}) "
            "CREATE (record)-[:ASSERTS {assertion_id: 'assert-limit-' + toString(index), "
            "quality_flag: 'valid', is_verified: true}]->(identifier)"
        ).consume()

    _people, others = _participants(
        neo4j_driver,
        [
            NormalizedIdentifier(
                identifier_type="phone",
                normalized_value="+6599999999",
                quality_flag=QualityFlag.VALID,
            )
        ],
        "sr-incoming",
    )

    assert len(others) == 100
    assert others[0].participant_id == "sr-limit-1"


def _seed_linked_record(
    driver: Driver,
    source_record_pk: str,
    person_id: str,
    *,
    link_status: str,
    record_type: str = "identity",
) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (person:Person {person_id: $person_id}) "
            "CREATE (record:SourceRecord {source_record_pk: $source_record_pk, "
            "lifecycle_status: 'parked', link_status: $link_status, "
            "attribution_status: 'person', reeval_pending: false, "
            "record_type: $record_type, "
            "observed_at: datetime('2026-01-01T00:00:00Z')})"
            "-[:LINKED_TO {linked_at: datetime()}]->(person)",
            source_record_pk=source_record_pk,
            person_id=person_id,
            link_status=link_status,
            record_type=record_type,
        ).consume()


def test_provisional_review_records_do_not_feed_person_links(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_person(neo4j_driver, "person-1")
    _seed_linked_record(neo4j_driver, "sr-provisional", "person-1", link_status="pending_review")
    _seed_assertion(neo4j_driver, "sr-provisional", "ident-phone", "assert-provisional")

    _execute(
        neo4j_driver,
        lambda tx: recompute_participant_links(tx, "person", "person-1"),
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:Person {person_id: 'person-1'})-[:HAS_IDENTIFIER]->() "
            "RETURN count(*) AS count",
        )
        == 0
    )

    with neo4j_driver.session() as session:
        session.run(
            "MATCH (record:SourceRecord {source_record_pk: 'sr-provisional'}) "
            "SET record.link_status = 'linked'"
        ).consume()
    _execute(
        neo4j_driver,
        lambda tx: recompute_participant_links(tx, "person", "person-1"),
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:Person {person_id: 'person-1'})-[:HAS_IDENTIFIER]->"
            "(:Identifier {identifier_id: 'ident-phone'}) RETURN count(*) AS count",
        )
        == 1
    )


def test_batch_snapshot_derives_system_sourcing_from_attributed_records(
    neo4j_driver: Driver,
) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_identifier(neo4j_driver, "ident-phone-chat", "phone", "+6598765432")
    _seed_person(neo4j_driver, "person-system")
    _seed_person(neo4j_driver, "person-chat")
    _seed_person_has_link(neo4j_driver, "person-system", "ident-phone")
    _seed_person_has_link(neo4j_driver, "person-chat", "ident-phone-chat")
    _seed_linked_record(neo4j_driver, "sr-identity", "person-system", link_status="linked")
    _seed_assertion(neo4j_driver, "sr-identity", "ident-phone", "assert-identity")
    _seed_linked_record(
        neo4j_driver,
        "sr-conversation",
        "person-chat",
        link_status="linked",
        record_type="conversation",
    )
    _seed_assertion(neo4j_driver, "sr-conversation", "ident-phone-chat", "assert-chat")

    snapshots = _execute(
        neo4j_driver,
        lambda tx: fetch_candidate_snapshots(
            tx, ["person-system", "person-chat"], include_legacy=False
        ),
    )

    system_phone = snapshots["person-system"].phones_by_value()[_PHONE]
    assert system_phone["is_verified"] is True
    assert system_phone["is_system_sourced"] is True
    chat_phone = snapshots["person-chat"].phones_by_value()["+6598765432"]
    assert chat_phone["is_system_sourced"] is False


# --- PR #506 fix-round regressions ----------------------------------------


def _seed_person_nric(driver: Driver, person_id: str, identifier_id: str, nric_key: str) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (person:Person {person_id: $person_id}) "
            "MATCH (identifier:Identifier {identifier_id: $identifier_id}) "
            "SET person.nric_key = $nric_key "
            "MERGE (person)-[:ANCHORED_BY]->(identifier)",
            person_id=person_id,
            identifier_id=identifier_id,
            nric_key=nric_key,
        ).consume()


def _seed_asserted_fact(
    driver: Driver,
    source_record_pk: str,
    attribute_name: str,
    attribute_value: str,
    assertion_id: str,
) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (record:SourceRecord {source_record_pk: $source_record_pk}) "
            "MERGE (fact:ProfileFact {attribute_name: $attribute_name, "
            "attribute_value: $attribute_value}) "
            "MERGE (record)-[assertion:ASSERTS {assertion_id: $assertion_id}]->(fact) "
            "ON CREATE SET assertion.field_path = 'attributes', "
            "assertion.role = 'subject', assertion.created_at = datetime()",
            source_record_pk=source_record_pk,
            attribute_name=attribute_name,
            attribute_value=attribute_value,
            assertion_id=assertion_id,
        ).consume()


def _attach_profile_member(driver: Driver, profile_id: str, member_pk: str) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (profile:Profile {profile_id: $profile_id}) "
            "MATCH (record:SourceRecord {source_record_pk: $member_pk}) "
            "SET record.link_status = 'profile', record.attribution_status = 'profile', "
            "record.reeval_pending = false "
            "MERGE (record)-[:IN_PROFILE]->(profile)",
            profile_id=profile_id,
            member_pk=member_pk,
        ).consume()


def _set_participant_count(driver: Driver, identifier_id: str, count: int) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (identifier:Identifier {identifier_id: $identifier_id}) "
            "SET identifier.participant_count = $count",
            identifier_id=identifier_id,
            count=count,
        ).consume()


def _record_attribution_snapshot(driver: Driver) -> list[dict[str, object]]:
    with driver.session() as session:
        rows = [
            dict(row)
            for row in session.run(
                "MATCH (record:SourceRecord) "
                "RETURN record.source_record_pk AS source_record_pk, "
                "record.lifecycle_status AS lifecycle_status, "
                "record.link_status AS link_status, "
                "record.attribution_status AS attribution_status, "
                "record.reeval_pending AS reeval_pending, "
                "record.activated_at AS activated_at, record.updated_at AS updated_at"
            )
        ]
    return sorted(rows, key=lambda row: str(row["source_record_pk"]))


def _identity_projection_snapshot(driver: Driver) -> list[dict[str, object]]:
    with driver.session() as session:
        rows = [
            dict(row)
            for row in session.run(
                "MATCH (:Person)-[rel:HAS_FACT]->(record:SourceRecord) "
                "RETURN 'HAS_FACT' AS kind, record.source_record_pk AS target, "
                "properties(rel) AS properties "
                "UNION ALL "
                "MATCH (:Person)-[rel:LIVES_AT]->(address:Address) "
                "RETURN 'LIVES_AT' AS kind, address.address_id AS target, "
                "properties(rel) AS properties"
            )
        ]
    return sorted(rows, key=lambda row: (str(row["kind"]), str(row["target"])))


def _single_person_id(driver: Driver) -> str:
    with driver.session() as session:
        row = session.run("MATCH (person:Person) RETURN person.person_id AS person_id").single(
            strict=True
        )
    value = row["person_id"]
    assert isinstance(value, str)
    return value


def test_over_cap_hub_does_not_bridge_unrelated_parked_records(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-hub", "phone", _PHONE)
    _set_participant_count(neo4j_driver, "ident-hub", 100)
    _seed_parked_record(neo4j_driver, "sr-hub-a", "2026-01-01T00:00:00Z")
    _seed_parked_record(neo4j_driver, "sr-hub-b", "2026-02-01T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-hub-a", "ident-hub", "assert-hub-a")
    _seed_assertion(neo4j_driver, "sr-hub-b", "ident-hub", "assert-hub-b")

    outcome = _materialize(neo4j_driver, "sr-hub-b")

    assert outcome.status == "parked"
    assert _scalar(neo4j_driver, "MATCH (profile:Profile) RETURN count(profile) AS count") == 0
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord)-[:IN_PROFILE]->(:Profile) RETURN count(*) AS count",
        )
        == 0
    )


def test_over_cap_hub_with_a_shared_direct_key_still_clusters(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-hub", "phone", _PHONE)
    _seed_identifier(neo4j_driver, "ident-direct", "email", "shared@example.com")
    _set_participant_count(neo4j_driver, "ident-hub", 100)
    _set_participant_count(neo4j_driver, "ident-direct", 1)
    _seed_parked_record(neo4j_driver, "sr-a", "2026-01-01T00:00:00Z")
    _seed_parked_record(neo4j_driver, "sr-b", "2026-02-01T00:00:00Z")
    for source_record_pk in ("sr-a", "sr-b"):
        _seed_assertion(
            neo4j_driver, source_record_pk, "ident-hub", f"assert-{source_record_pk}-hub"
        )
        _seed_assertion(
            neo4j_driver, source_record_pk, "ident-direct", f"assert-{source_record_pk}-direct"
        )

    outcome = _materialize(neo4j_driver, "sr-b")

    assert outcome.status == "created"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord)-[:IN_PROFILE]->(:Profile) RETURN count(*) AS count",
        )
        == 2
    )


def test_concurrent_materialization_bridges_through_a_shared_key_once(
    neo4j_driver: Driver,
) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-k1", "email", "k1@example.com")
    _seed_identifier(neo4j_driver, "ident-k2", "email", "k2@example.com")
    _seed_identifier(neo4j_driver, "ident-k3", "phone", _PHONE)
    _seed_parked_record(neo4j_driver, "sr-a", "2026-01-01T00:00:00Z")
    _seed_parked_record(neo4j_driver, "sr-b", "2026-02-01T00:00:00Z")
    # Record A enters through k1/k3, record B through k2/k3: k3 bridges them.
    _seed_assertion(neo4j_driver, "sr-a", "ident-k1", "assert-a1")
    _seed_assertion(neo4j_driver, "sr-a", "ident-k3", "assert-a3")
    _seed_assertion(neo4j_driver, "sr-b", "ident-k2", "assert-b2")
    _seed_assertion(neo4j_driver, "sr-b", "ident-k3", "assert-b3")

    barrier = threading.Barrier(2)

    def _locked_materialize(
        tx: ManagedTransaction, source_record_pk: str
    ) -> MaterializationOutcome:
        source_keys = fetch_record_identifier_keys(tx, source_record_pk)
        closure = fetch_cluster_key_closure(tx, source_keys, source_record_pk)
        locked = frozenset(
            {
                (key["identifier_type"], key["identifier_scope"], key["normalized_value"])
                for key in source_keys
            }
            | closure
        )
        lock_identifiers(
            tx,
            [
                IdentifierKey(
                    identifier_type=identifier_type,
                    identifier_scope=scope,
                    normalized_value=value,
                )
                for identifier_type, scope, value in sorted(locked)
            ],
        )
        return materialize_profile_for_record(tx, source_record_pk, locked_keys=locked)

    def _materialize_in_transaction(source_record_pk: str) -> MaterializationOutcome:
        barrier.wait(timeout=10)
        with neo4j_driver.session() as session:
            return session.execute_write(lambda tx: _locked_materialize(tx, source_record_pk))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_materialize_in_transaction, "sr-a")
        second = pool.submit(_materialize_in_transaction, "sr-b")
        outcomes = [first.result(timeout=30), second.result(timeout=30)]

    assert sorted(str(outcome.status) for outcome in outcomes) == ["created", "joined"]
    assert _scalar(neo4j_driver, "MATCH (profile:Profile) RETURN count(profile) AS count") == 1
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord)-[:IN_PROFILE]->(:Profile) RETURN count(*) AS count",
        )
        == 2
    )


def test_promotion_discovers_profile_through_shared_non_nric_key(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    payload = json.dumps(
        {
            "identifiers": [],
            "addresses": [
                {
                    "country_code": "SG",
                    "postal_code": "018989",
                    "street_name": "Marina Boulevard",
                    "street_number": "10",
                    "unit_number": "12-34",
                    "normalized_full": "10 Marina Boulevard 12-34",
                    "quality_flag": "valid",
                }
            ],
            "attributes": [
                {
                    "attribute_name": "full_name",
                    "attribute_value": "Alice Tan",
                    "quality_flag": "partial_parse",
                },
                {
                    "attribute_name": "dob",
                    "attribute_value": "1990-01-01",
                    "quality_flag": "valid",
                },
                {
                    "attribute_name": "race_ethnicity",
                    "attribute_value": "Chinese",
                    "quality_flag": "valid",
                },
            ],
        }
    )
    _seed_parked_record(
        neo4j_driver,
        "sr-p1",
        "2026-01-01T00:00:00Z",
        normalized_payload=payload,
    )
    _seed_parked_record(neo4j_driver, "sr-p2", "2026-01-01T00:00:00Z")
    for source_record_pk in ("sr-p1", "sr-p2"):
        _seed_assertion(neo4j_driver, source_record_pk, "ident-phone", f"assert-{source_record_pk}")
    _seed_profile(neo4j_driver, "profile-phone", "sr-p1")
    _attach_profile_member(neo4j_driver, "profile-phone", "sr-p2")
    _seed_profile_link(neo4j_driver, "profile-phone", "ident-phone")
    _seed_parked_record(neo4j_driver, "sr-nric", "2026-03-01T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-nric", "ident-nric", "assert-nric")
    _seed_assertion(neo4j_driver, "sr-nric", "ident-phone", "assert-nric-phone")

    phone_key = IdentifierKey(
        identifier_type="phone", identifier_scope="global", normalized_value=_PHONE
    )
    promoted = _execute(
        neo4j_driver,
        lambda tx: promote_profiles_for_nric(
            tx,
            _nric_identifier(),
            identifier_keys=[phone_key],
            source_key="onediver",
            record_type=RecordType.IDENTITY,
            incoming=NameDob(full_name="Alice Tan", dob="1990-01-01"),
        ),
    )

    assert promoted == ("profile-phone",)
    person_id = _single_person_id(neo4j_driver)
    profiles = {str(row["profile_id"]): row for row in _profile_snapshot(neo4j_driver)}
    assert profiles["profile-phone"]["status"] == "promoted"
    assert profiles["profile-phone"]["promoted_person_id"] == person_id
    members = [
        row
        for row in _record_attribution_snapshot(neo4j_driver)
        if row["source_record_pk"] in {"sr-p1", "sr-p2"}
    ]
    assert {str(row["lifecycle_status"]) for row in members} == {"active"}
    assert {str(row["link_status"]) for row in members} == {"linked"}
    assert {str(row["attribution_status"]) for row in members} == {"person"}
    with neo4j_driver.session() as session:
        facts = {
            str(row["attribute_name"]): row
            for row in session.run(
                "MATCH (:Person {person_id: $person_id})-[fact:HAS_FACT]->(:SourceRecord) "
                "WHERE coalesce(fact.is_active, true) = true "
                "RETURN fact.attribute_name AS attribute_name, "
                "fact.quality_flag AS quality_flag, keys(fact) AS keys",
                person_id=person_id,
            )
        }
        addresses = [
            row
            for row in session.run(
                "MATCH (:Person {person_id: $person_id})-[lives:LIVES_AT]->(address:Address) "
                "WHERE coalesce(lives.is_active, true) = true "
                "MATCH (:SourceRecord {source_record_pk: $source_record_pk})"
                "-[:DESCRIBES_ADDRESS]->(address) "
                "RETURN lives.source_record_pk AS source_record_pk",
                person_id=person_id,
                source_record_pk="sr-p1",
            )
        ]
    # Only C1 fact-registry attributes are projected, each with its own quality;
    # provisional trust/confidence stay unset rather than fabricated, and
    # addresses keep their record provenance.
    assert set(facts) == {"full_name", "dob"}
    assert facts["full_name"]["quality_flag"] == "partial_parse"
    assert facts["dob"]["quality_flag"] == "valid"
    for fact in facts.values():
        assert "source_trust_tier" not in fact["keys"]
        assert "confidence" not in fact["keys"]
    assert [row["source_record_pk"] for row in addresses] == ["sr-p1"]
    snapshots = _execute(
        neo4j_driver,
        lambda tx: fetch_candidate_snapshots(tx, [person_id], include_legacy=False),
    )
    assert _PHONE in snapshots[person_id].phones_by_value()

    before_projection = _identity_projection_snapshot(neo4j_driver)
    before_records = _record_attribution_snapshot(neo4j_driver)
    before_links = _link_snapshot(neo4j_driver)

    def _rerun_reattribution(tx: ManagedTransaction) -> None:
        row = tx.run(
            queries.REATTRIBUTE_PROFILE_MEMBERS_TO_PERSON,
            profile_id="profile-phone",
            person_id=person_id,
        ).single()
        assert row is not None
        assert row["reattributed_members"] == []

    _execute(neo4j_driver, _rerun_reattribution)
    assert _identity_projection_snapshot(neo4j_driver) == before_projection
    assert _record_attribution_snapshot(neo4j_driver) == before_records
    assert _link_snapshot(neo4j_driver) == before_links


def _shadow_envelope(nric: bool) -> SourceRecordEnvelope:
    identifiers: list[dict[str, object]] = []
    if nric:
        identifiers.append({"type": "nric", "value": _NRIC_DIGEST, "is_verified": True})
    identifiers.append({"type": "phone", "value": _PHONE, "is_verified": True})
    return SourceRecordEnvelope(
        source_system="onediver",
        source_record_id="shadow-record",
        record_type=RecordType.IDENTITY,
        observed_at="2026-03-01T00:00:00+00:00",
        record_hash="sha256:shadow",
        identifiers=identifiers,
        raw_payload={},
    )


def test_shadow_projection_never_promotes_or_reattributes(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_person(neo4j_driver, "person-legacy")
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_parked_record(neo4j_driver, "sr-member", "2026-01-01T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-member", "ident-phone", "assert-member")
    _seed_profile(neo4j_driver, "profile-1", "sr-member")
    _seed_profile_link(neo4j_driver, "profile-1", "ident-phone")
    _seed_profile_link(neo4j_driver, "profile-1", "ident-nric")
    _seed_linked_record(neo4j_driver, "sr-incoming", "person-legacy", link_status="linked")
    _seed_assertion(neo4j_driver, "sr-incoming", "ident-nric", "assert-incoming-nric")
    _seed_assertion(neo4j_driver, "sr-incoming", "ident-phone", "assert-incoming-phone")

    pipeline = IngestPipeline(cast(object, MagicMock()), identity_model="v2_shadow")
    before_records = _record_attribution_snapshot(neo4j_driver)
    _execute(
        neo4j_driver,
        lambda tx: pipeline._project_shadow_identity(
            tx,
            _shadow_envelope(nric=True),
            [_nric_identifier(), _phone_identifier()],
            [],
            "sr-incoming",
            "person-legacy",
        ),
    )

    profiles = {str(row["profile_id"]): row for row in _profile_snapshot(neo4j_driver)}
    assert profiles["profile-1"]["status"] == "open"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord)-[link:LINKED_TO]->(:Person) "
            "WHERE coalesce(link.is_active, true) = true RETURN count(*) AS count",
        )
        == 1
    )
    assert _record_attribution_snapshot(neo4j_driver) == before_records
    assert _scalar(neo4j_driver, "MATCH (person:Person) RETURN count(person) AS count") == 1
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:Person {person_id: 'person-legacy'})-[:HAS_IDENTIFIER]->"
            "(:Identifier {identifier_id: 'ident-nric'}) RETURN count(*) AS count",
        )
        == 1
    )


def test_shadow_anchor_never_overwrites_another_persons_digest(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_person(neo4j_driver, "person-owner")
    _seed_person(neo4j_driver, "person-legacy")
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_person_nric(neo4j_driver, "person-owner", "ident-nric", _NRIC_DIGEST)
    _seed_linked_record(neo4j_driver, "sr-incoming", "person-legacy", link_status="linked")

    pipeline = IngestPipeline(cast(object, MagicMock()), identity_model="v2_shadow")
    _execute(
        neo4j_driver,
        lambda tx: pipeline._project_shadow_identity(
            tx,
            _shadow_envelope(nric=True),
            [_nric_identifier(), _phone_identifier()],
            [],
            "sr-incoming",
            "person-legacy",
        ),
    )

    assert (
        _scalar(
            neo4j_driver,
            "MATCH (person:Person {person_id: 'person-legacy'}) "
            "WHERE person.nric_key IS NOT NULL RETURN count(person) AS count",
        )
        == 0
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:Person {person_id: 'person-legacy'})-[:ANCHORED_BY]->(:Identifier) "
            "RETURN count(*) AS count",
        )
        == 0
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (person:Person)-[:ANCHORED_BY]->(:Identifier {identifier_id: 'ident-nric'}) "
            "RETURN count(person) AS count",
        )
        == 1
    )


def test_v2_anchor_does_not_attach_a_hard_blocked_person(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_person(neo4j_driver, "person-anchored")
    _seed_person(neo4j_driver, "person-owner")
    _seed_person_nric(neo4j_driver, "person-anchored", "ident-nric", _NRIC_DIGEST)
    _seed_person_has_link(neo4j_driver, "person-anchored", "ident-nric")
    _seed_person_has_link(neo4j_driver, "person-owner", "ident-phone")
    _seed_parked_record(neo4j_driver, "sr-member", "2026-01-01T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-member", "ident-nric", "assert-member-nric")
    _seed_profile(neo4j_driver, "profile-blocked", "sr-member")
    _seed_profile_link(neo4j_driver, "profile-blocked", "ident-nric")
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (anchored:Person {person_id: 'person-anchored'}) "
            "MATCH (owner:Person {person_id: 'person-owner'}) "
            "CREATE (anchored)-[:NO_MATCH_LOCK {created_at: datetime()}]->(owner)"
        ).consume()

    pipeline = IngestPipeline(cast(object, MagicMock()), identity_model="v2")
    resolved = _execute(
        neo4j_driver,
        lambda tx: pipeline._resolve_v2_person(
            tx,
            _shadow_envelope(nric=True),
            [_nric_identifier(), _phone_identifier()],
            [],
            MatchResult(decision=MatchDecision.NO_MATCH, confidence=1.0, reasons=["hard_blocked"]),
            [],
        ),
    )

    assert resolved == (None, False)
    profiles = {str(row["profile_id"]): row for row in _profile_snapshot(neo4j_driver)}
    assert profiles["profile-blocked"]["status"] == "open"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord)-[:LINKED_TO]->(:Person) RETURN count(*) AS count",
        )
        == 0
    )


def test_anchor_resolves_merged_holder_to_active_survivor(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_person(neo4j_driver, "person-absorbed")
    _seed_person(neo4j_driver, "person-survivor")
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_person_nric(neo4j_driver, "person-absorbed", "ident-nric", _NRIC_DIGEST)
    _seed_parked_record(neo4j_driver, "sr-member", "2026-01-01T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-member", "ident-nric", "assert-member-nric")
    _seed_profile(neo4j_driver, "profile-promoted", "sr-member")
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (absorbed:Person {person_id: 'person-absorbed'}) "
            "MATCH (survivor:Person {person_id: 'person-survivor'}) "
            "SET absorbed.status = 'merged' "
            "CREATE (absorbed)-[:MERGED_INTO {merge_event_id: 'me-1', "
            "actor: 'match_engine', timestamp: datetime()}]->(survivor)"
        ).consume()
        session.run(
            "MATCH (profile:Profile {profile_id: 'profile-promoted'}) "
            "SET profile.status = 'promoted', "
            "profile.promoted_person_id = 'person-absorbed'"
        ).consume()

    person_id, created = _execute(
        neo4j_driver,
        lambda tx: anchor_person_by_nric(tx, _nric_identifier()),
    )

    assert (person_id, created) == ("person-survivor", False)
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:Person {person_id: 'person-survivor'})-[:ANCHORED_BY]->"
            "(:Identifier {identifier_id: 'ident-nric'}) RETURN count(*) AS count",
        )
        == 1
    )
    assert _scalar(neo4j_driver, "MATCH (person:Person) RETURN count(person) AS count") == 2

    retry = _promote(neo4j_driver, "profile-promoted")
    assert retry.status == "already_promoted"
    assert retry.person_id == "person-survivor"


def test_replaced_record_version_stops_evidencing_the_person(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-old-phone", "phone", "+6590000001")
    _seed_identifier(neo4j_driver, "ident-new-phone", "phone", "+6590000002")
    _seed_person(neo4j_driver, "person-1")
    _seed_linked_record(neo4j_driver, "sr-old", "person-1", link_status="linked")
    _seed_linked_record(neo4j_driver, "sr-new", "person-1", link_status="linked")
    _seed_assertion(neo4j_driver, "sr-old", "ident-old-phone", "assert-old")
    _seed_assertion(neo4j_driver, "sr-new", "ident-new-phone", "assert-new")
    _execute(
        neo4j_driver,
        lambda tx: recompute_participant_links(tx, "person", "person-1"),
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:Person {person_id: 'person-1'})-[:HAS_IDENTIFIER]->() "
            "RETURN count(*) AS count",
        )
        == 2
    )
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (record:SourceRecord {source_record_pk: 'sr-old'}) "
            "SET record.lifecycle_status = 'superseded'"
        ).consume()

    _execute(
        neo4j_driver,
        lambda tx: recompute_participant_links(tx, "person", "person-1"),
    )

    linked = {
        str(link["identifier_id"])
        for link in _link_snapshot(neo4j_driver)
        if link["participant_id"] == "person-1"
    }
    assert linked == {"ident-new-phone"}
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (identifier:Identifier {identifier_id: 'ident-old-phone'}) "
            "RETURN identifier.participant_count AS count",
        )
        == 0
    )


def test_golden_profile_reads_has_identifier_evidence(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_person(neo4j_driver, "person-has-only")
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_identifier(neo4j_driver, "ident-email", "email", "has-only@example.com")
    _seed_person_has_link(neo4j_driver, "person-has-only", "ident-phone")
    _seed_person_has_link(neo4j_driver, "person-has-only", "ident-email")

    profile = _execute(
        neo4j_driver,
        lambda tx: compute_golden_profile(tx, "person-has-only", v2_evidence=True),
    )

    assert profile["preferred_phone"] == _PHONE
    assert profile["preferred_email"] == "has-only@example.com"


def test_v1_golden_profile_ignores_has_only_identifiers(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_person(neo4j_driver, "person-has-only")
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_identifier(neo4j_driver, "ident-email", "email", "has-only@example.com")
    _seed_person_has_link(neo4j_driver, "person-has-only", "ident-phone")
    _seed_person_has_link(neo4j_driver, "person-has-only", "ident-email")

    profile = _execute(
        neo4j_driver,
        lambda tx: compute_golden_profile(tx, "person-has-only"),
    )

    assert profile["preferred_phone"] is None
    assert profile["preferred_email"] is None


def test_promotion_activates_only_the_newest_version_of_an_identity(
    neo4j_driver: Driver,
) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_parked_record(
        neo4j_driver,
        "sr-v1",
        "2026-01-01T00:00:00Z",
        source_record_id="dup-ident",
        source_record_version=1,
    )
    _seed_parked_record(
        neo4j_driver,
        "sr-v2",
        "2026-02-01T00:00:00Z",
        source_record_id="dup-ident",
        source_record_version=2,
    )
    for source_record_pk in ("sr-v1", "sr-v2"):
        _seed_assertion(neo4j_driver, source_record_pk, "ident-phone", f"assert-{source_record_pk}")
    _seed_profile(neo4j_driver, "profile-versioned", "sr-v1")
    _attach_profile_member(neo4j_driver, "profile-versioned", "sr-v2")
    _seed_profile_link(neo4j_driver, "profile-versioned", "ident-phone")

    outcome = _execute(
        neo4j_driver,
        lambda tx: promote_profile(
            tx,
            profile_id="profile-versioned",
            identifier=_nric_identifier(),
            source_key="onediver",
            record_type=RecordType.IDENTITY,
            incoming=NameDob(full_name="Alice Tan", dob="1990-01-01"),
        ),
    )

    assert outcome.status == "promoted"
    person_id = _single_person_id(neo4j_driver)
    linked = {
        str(row["source_record_pk"])
        for row in _record_attribution_snapshot(neo4j_driver)
        if str(row["link_status"]) == "linked"
    }
    assert linked == {"sr-v2"}
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-v2'})-[link:LINKED_TO]->(:Person) "
            "WHERE coalesce(link.is_active, true) = true RETURN count(*) AS count",
        )
        == 1
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-v1'})-[:LINKED_TO]->(:Person) "
            "RETURN count(*) AS count",
        )
        == 0
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (record:SourceRecord {source_record_pk: 'sr-v2'}) "
            "WHERE record.is_latest = true AND record.lifecycle_status = 'active' "
            "RETURN count(record) AS count",
        )
        == 1
    )
    state = _execute(
        neo4j_driver,
        lambda tx: load_locked_source_state(tx, "onediver", "dup-ident", "legacy-default"),
    )
    assert state.active is not None
    assert state.active.source_record_pk == "sr-v2"
    assert state.pending is None
    assert person_id


def test_promotion_rejects_member_asserting_a_different_nric(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-other-nric", "nric", _OTHER_DIGEST)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_parked_record(neo4j_driver, "sr-conflict", "2026-01-01T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-conflict", "ident-phone", "assert-phone")
    _seed_assertion(neo4j_driver, "sr-conflict", "ident-other-nric", "assert-other-nric")
    _seed_profile(neo4j_driver, "profile-conflict", "sr-conflict")
    _seed_profile_link(neo4j_driver, "profile-conflict", "ident-phone")

    outcome = _execute(
        neo4j_driver,
        lambda tx: promote_profile(
            tx,
            profile_id="profile-conflict",
            identifier=_nric_identifier(),
            source_key="onediver",
            record_type=RecordType.IDENTITY,
            incoming=NameDob(full_name="Alice Tan", dob="1990-01-01"),
        ),
    )

    assert outcome.status == "rejected"
    assert outcome.reason == "nric_conflict"
    profiles = {str(row["profile_id"]): row for row in _profile_snapshot(neo4j_driver)}
    assert profiles["profile-conflict"]["status"] == "open"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord)-[:LINKED_TO]->(:Person) RETURN count(*) AS count",
        )
        == 0
    )
    assert _scalar(neo4j_driver, "MATCH (person:Person) RETURN count(person) AS count") == 0


def test_promotion_rejects_member_with_contradictory_dob(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_parked_record(neo4j_driver, "sr-dob", "2026-01-01T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-dob", "ident-phone", "assert-phone")
    _seed_asserted_fact(neo4j_driver, "sr-dob", "dob", "1980-02-02", "assert-dob")
    _seed_profile(neo4j_driver, "profile-dob", "sr-dob")
    _seed_profile_link(neo4j_driver, "profile-dob", "ident-phone")

    outcome = _execute(
        neo4j_driver,
        lambda tx: promote_profile(
            tx,
            profile_id="profile-dob",
            identifier=_nric_identifier(),
            source_key="onediver",
            record_type=RecordType.IDENTITY,
            incoming=NameDob(full_name="Alice Tan", dob="1990-01-01"),
        ),
    )

    assert outcome.status == "rejected"
    assert outcome.reason == "nric_contradiction"
    profiles = {str(row["profile_id"]): row for row in _profile_snapshot(neo4j_driver)}
    assert profiles["profile-dob"]["status"] == "open"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord)-[:LINKED_TO]->(:Person) RETURN count(*) AS count",
        )
        == 0
    )


def test_legacy_golden_uses_verified_legacy_identifier_and_ignores_has(
    neo4j_driver: Driver,
) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-phone-a", "phone", "+6590000001")
    _seed_identifier(neo4j_driver, "ident-phone-b", "phone", "+6590000002")
    _seed_person(neo4j_driver, "person-golden")
    _seed_linked_record(neo4j_driver, "sr-golden", "person-golden", link_status="linked")
    _seed_assertion(neo4j_driver, "sr-golden", "ident-phone-a", "assert-a")
    _seed_assertion(neo4j_driver, "sr-golden", "ident-phone-b", "assert-b")
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (person:Person {person_id: 'person-golden'}) "
            "MATCH (identifier:Identifier {identifier_id: $identifier_id}) "
            "CREATE (person)-[:HAS_IDENTIFIER {link_id: $link_id, status: 'active', "
            "verified_count: $verified_count, last_confirmed_at: $confirmed, "
            "computed_at: datetime()}]->(identifier)",
            identifier_id="ident-phone-a",
            link_id=link_id("person-golden", "ident-phone-a"),
            verified_count=0,
            confirmed="2026-06-03T00:00:00Z",
        ).consume()
        session.run(
            "MATCH (person:Person {person_id: 'person-golden'}) "
            "MATCH (identifier:Identifier {identifier_id: $identifier_id}) "
            "CREATE (person)-[:HAS_IDENTIFIER {link_id: $link_id, status: 'active', "
            "verified_count: 1, last_confirmed_at: '2026-06-01T00:00:00Z', "
            "computed_at: datetime()}]->(identifier)",
            identifier_id="ident-phone-b",
            link_id=link_id("person-golden", "ident-phone-b"),
        ).consume()
        session.run(
            "MATCH (person:Person {person_id: 'person-golden'}) "
            "MATCH (identifier:Identifier {identifier_id: 'ident-phone-a'}) "
            "CREATE (person)-[:IDENTIFIED_BY {is_active: true, is_verified: true, "
            "source_record_pk: 'sr-golden', source_system_key: 'onediver', "
            "last_confirmed_at: '2026-06-02T00:00:00Z'}]->(identifier)"
        ).consume()

    profile = _execute(
        neo4j_driver,
        lambda tx: compute_golden_profile(tx, "person-golden"),
    )

    # v1/shadow golden uses only the legacy projection: the verified legacy edge
    # wins and neither HAS row is a golden source.
    assert profile["preferred_phone"] == "+6590000001"


def test_profile_member_growth_counts_toward_the_expansion_cap(
    neo4j_driver: Driver,
) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (profile:Profile {profile_id: 'profile-growth', status: 'open', "
            "evidence_version: 1, materialized_at: datetime('2026-01-01T00:00:00Z')})"
        ).consume()
        session.run(
            "MATCH (profile:Profile {profile_id: 'profile-growth'}) "
            "MATCH (identifier:Identifier {identifier_id: 'ident-phone'}) "
            "UNWIND range(1, 51) AS index "
            "CREATE (record:SourceRecord {source_record_pk: 'sr-growth-' + toString(index), "
            "source_record_id: 'growth-' + toString(index), "
            "source_instance_id: 'legacy-default', source_record_version: 1, "
            "lifecycle_status: 'parked', link_status: 'profile', "
            "attribution_status: 'profile', reeval_pending: false, "
            "observed_at: datetime('2026-01-01T00:00:00Z')})-[:IN_PROFILE]->(profile) "
            "CREATE (record)-[:ASSERTS {assertion_id: 'assert-growth-' + toString(index)}]"
            "->(identifier)"
        ).consume()

    _execute(
        neo4j_driver,
        lambda tx: recompute_participant_counts(tx, {("phone", "global", _PHONE)}),
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (identifier:Identifier {identifier_id: 'ident-phone'}) "
            "RETURN identifier.participant_count AS count",
        )
        == 51
    )

    _seed_parked_record(neo4j_driver, "sr-growth-late", "2026-02-01T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-growth-late", "ident-phone", "assert-growth-late")

    outcome = _materialize(neo4j_driver, "sr-growth-late")

    assert outcome.status == "parked"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord)-[:IN_PROFILE]->(:Profile {profile_id: 'profile-growth'}) "
            "RETURN count(*) AS count",
        )
        == 51
    )


def test_pair_audit_merge_maintains_has_projection(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-survivor-phone", "phone", "+6590000011")
    _seed_identifier(neo4j_driver, "ident-absorbed-phone", "phone", "+6590000012")
    _seed_person(neo4j_driver, "person-survivor")
    _seed_person(neo4j_driver, "person-absorbed")
    _seed_linked_record(neo4j_driver, "sr-survivor", "person-survivor", link_status="linked")
    _seed_linked_record(neo4j_driver, "sr-absorbed", "person-absorbed", link_status="linked")
    _seed_assertion(neo4j_driver, "sr-survivor", "ident-survivor-phone", "assert-survivor")
    _seed_assertion(neo4j_driver, "sr-absorbed", "ident-absorbed-phone", "assert-absorbed")
    _seed_person_has_link(neo4j_driver, "person-survivor", "ident-survivor-phone")
    _seed_person_has_link(neo4j_driver, "person-absorbed", "ident-absorbed-phone")

    _execute(
        neo4j_driver,
        lambda tx: merge_person_pair(
            tx,
            absorbed_id="person-absorbed",
            survivor_id="person-survivor",
            match_decision_id="md-merge",
            reason="pair audit",
            collector=PersonListSummaryDeltaCollector(),
        ),
    )

    survivor_links = {
        str(link["identifier_id"])
        for link in _link_snapshot(neo4j_driver)
        if link["participant_id"] == "person-survivor"
    }
    absorbed_links = {
        str(link["identifier_id"])
        for link in _link_snapshot(neo4j_driver)
        if link["participant_id"] == "person-absorbed"
    }
    assert survivor_links == {"ident-survivor-phone", "ident-absorbed-phone"}
    assert absorbed_links == set()


def test_v2_review_with_anchorable_nric_does_not_anchor_or_promote(
    neo4j_driver: Driver,
) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_parked_record(neo4j_driver, "sr-review-member", "2026-01-01T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-review-member", "ident-phone", "assert-review")
    _seed_profile(neo4j_driver, "profile-review", "sr-review-member")
    _seed_profile_link(neo4j_driver, "profile-review", "ident-phone")
    pipeline = IngestPipeline(cast(object, MagicMock()), identity_model="v2")

    resolved = _execute(
        neo4j_driver,
        lambda tx: pipeline._resolve_v2_person(
            tx,
            _shadow_envelope(nric=True),
            [_nric_identifier(), _phone_identifier()],
            [],
            MatchResult(decision=MatchDecision.REVIEW, confidence=0.7, reasons=["review"]),
            [],
        ),
    )

    assert resolved == (None, False)
    assert _scalar(neo4j_driver, "MATCH (person:Person) RETURN count(person) AS count") == 0
    profiles = {str(row["profile_id"]): row for row in _profile_snapshot(neo4j_driver)}
    assert profiles["profile-review"]["status"] == "open"


def test_v2_blocked_anchor_is_not_returned_as_engine_match(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_person(neo4j_driver, "person-anchored")
    _seed_person(neo4j_driver, "person-owner")
    _seed_person_nric(neo4j_driver, "person-anchored", "ident-nric", _NRIC_DIGEST)
    _seed_person_has_link(neo4j_driver, "person-anchored", "ident-nric")
    _seed_person_has_link(neo4j_driver, "person-owner", "ident-phone")
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (anchored:Person {person_id: 'person-anchored'}) "
            "MATCH (owner:Person {person_id: 'person-owner'}) "
            "CREATE (anchored)-[:NO_MATCH_LOCK {created_at: datetime()}]->(owner)"
        ).consume()
    pipeline = IngestPipeline(cast(object, MagicMock()), identity_model="v2")

    resolved = _execute(
        neo4j_driver,
        lambda tx: pipeline._resolve_v2_person(
            tx,
            _shadow_envelope(nric=True),
            [_nric_identifier(), _phone_identifier()],
            [],
            MatchResult(
                decision=MatchDecision.MERGE,
                confidence=1.0,
                matched_person_id="person-anchored",
                reasons=["hard_blocked"],
            ),
            [],
        ),
    )

    assert resolved == (None, False)
