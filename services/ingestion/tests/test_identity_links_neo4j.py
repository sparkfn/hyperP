"""Neo4j 5.26 integration coverage for the C3 identity-link writers."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, ManagedTransaction, Record
from src.graph.client import Neo4jClient
from src.graph.schema_init import apply_schema
from src.identity_link_writes import (
    lock_identifiers,
    materialize_profile_for_record,
    promote_profile,
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
    NormalizedIdentifier,
    QualityFlag,
    RecordType,
)
from src.nric_anchor import NameDob
from src.pipeline_writes import find_identity_candidates

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


def _seed_parked_record(driver: Driver, source_record_pk: str, observed_at: str) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (system:SourceSystem {source_key: 'onediver'}) "
            "CREATE (record:SourceRecord {source_record_pk: $source_record_pk, "
            "lifecycle_status: 'parked', link_status: 'unlinked', "
            "attribution_status: 'parked', reeval_pending: true, "
            "observed_at: datetime($observed_at)})-[:FROM_SOURCE]->(system)",
            source_record_pk=source_record_pk,
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
        == 1
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
            "CREATE (person)-[:HAS_IDENTIFIER {link_id: $link_id, computed_at: datetime(), "
            "verified_count: $verified_count}]->(identifier)",
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
