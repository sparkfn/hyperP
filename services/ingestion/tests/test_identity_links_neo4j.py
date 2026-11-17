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
from src import identity_link_promotion as promotion_module
from src.golden_profile import compute_golden_profile
from src.graph import queries
from src.graph.client import Neo4jClient
from src.graph.person_list_summary_deltas import PersonListSummaryDeltaCollector
from src.graph.schema_init import apply_schema
from src.identity_link_member_evidence import MemberDisposition, MemberEvidence
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
from src.pipeline_writes import find_identity_candidates, persist_nric_contradiction_review
from src.record_lifecycle import load_locked_source_state
from src.review_taxonomy import case_key

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
    *,
    checksum_valid: bool | None = None,
) -> None:
    with driver.session() as session:
        session.run(
            "MERGE (identifier:Identifier {identifier_type: $identifier_type, "
            "identifier_scope: 'global', normalized_value: $normalized_value}) "
            "SET identifier.identifier_id = $identifier_id, "
            "identifier.checksum_valid = $checksum_valid",
            identifier_id=identifier_id,
            identifier_type=identifier_type,
            normalized_value=normalized_value,
            checksum_valid=checksum_valid,
        ).consume()


def _seed_parked_record(
    driver: Driver,
    source_record_pk: str,
    observed_at: str,
    *,
    source_record_id: str | None = None,
    source_record_version: int = 1,
    normalized_payload: str | None = None,
    record_hash: str | None = None,
    record_type: str | None = None,
    extraction_confidence: float | None = None,
) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (system:SourceSystem {source_key: 'onediver'}) "
            "CREATE (record:SourceRecord {source_record_pk: $source_record_pk, "
            "source_record_id: $source_record_id, "
            "source_instance_id: 'legacy-default', "
            "source_record_version: $source_record_version, "
            "record_hash: $record_hash, "
            "lifecycle_status: 'parked', link_status: 'unlinked', "
            "attribution_status: 'parked', reeval_pending: true, "
            "observed_at: datetime($observed_at), "
            "record_type: $record_type, "
            "extraction_confidence: $extraction_confidence, "
            "normalized_payload: $normalized_payload})-[:FROM_SOURCE]->(system)",
            source_record_pk=source_record_pk,
            source_record_id=source_record_id or source_record_pk,
            source_record_version=source_record_version,
            record_hash=record_hash,
            normalized_payload=normalized_payload,
            record_type=record_type,
            extraction_confidence=extraction_confidence,
            observed_at=observed_at,
        ).consume()


def _seed_source_version(
    driver: Driver,
    source_record_pk: str,
    observed_at: str,
    *,
    source_record_id: str,
    source_record_version: int,
    lifecycle_status: str,
    link_status: str = "unlinked",
    attribution_status: str = "parked",
    is_latest: bool = False,
) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (system:SourceSystem {source_key: 'onediver'}) "
            "CREATE (record:SourceRecord {source_record_pk: $source_record_pk, "
            "source_record_id: $source_record_id, source_instance_id: 'legacy-default', "
            "source_record_version: $source_record_version, record_hash: 'sha256:version', "
            "lifecycle_status: $lifecycle_status, link_status: $link_status, "
            "attribution_status: $attribution_status, is_latest: $is_latest, "
            "reeval_pending: false, observed_at: datetime($observed_at)})"
            "-[:FROM_SOURCE]->(system)",
            source_record_pk=source_record_pk,
            source_record_id=source_record_id,
            source_record_version=source_record_version,
            lifecycle_status=lifecycle_status,
            link_status=link_status,
            attribution_status=attribution_status,
            is_latest=is_latest,
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


def _seed_address(driver: Driver) -> None:
    """Seed the Address node a promoted member's payload resolves onto."""
    with driver.session() as session:
        session.run(
            "MERGE (address:Address {country_code: 'SG', postal_code: '018989', "
            "street_name: 'Marina Boulevard', street_number: '10', unit_number: '12-34'}) "
            "ON CREATE SET address.address_id = randomUUID(), "
            "address.normalized_full = '10 Marina Boulevard 12-34'"
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
    # Two tier_1 verified assertions: noisy-OR is 1.0; the recount sees two
    # profile members asserting the phone, so exclusivity is 1/2 and the rank
    # (anchored at the link's own last observation) is 0.5.
    assert properties["score"] == 1.0
    assert properties["exclusivity"] == 0.5
    assert properties["rank_score"] == 0.5
    # Neo4j removes null-valued properties, so an unset golden_rank is absent.
    assert properties.get("golden_rank") is None
    assert properties["basis"] == "ASSERTS"
    assert properties["status"] == "active"
    assert properties["policy_version"] == "c4-w1"

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


def test_link_weight_decay_anchors_at_per_type_reference(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-email", "email", "shared@example.com")
    _seed_identifier(neo4j_driver, "ident-phone-old", "phone", "+6591111111")
    _seed_identifier(neo4j_driver, "ident-phone-new", "phone", "+6592222222")
    _seed_parked_record(neo4j_driver, "sr-old", "2026-01-01T00:00:00Z")
    _seed_parked_record(neo4j_driver, "sr-new", "2027-01-01T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-old", "ident-email", "assert-old-email")
    _seed_assertion(neo4j_driver, "sr-new", "ident-email", "assert-new-email")
    _seed_assertion(neo4j_driver, "sr-old", "ident-phone-old", "assert-old-phone")
    _seed_assertion(neo4j_driver, "sr-new", "ident-phone-new", "assert-new-phone")

    created = _materialize(neo4j_driver, "sr-new")
    assert created.status == "created"
    profile_id = created.profile_id
    assert profile_id is not None

    snapshots = {
        str(row["identifier_id"]): row["properties"] for row in _link_snapshot(neo4j_driver)
    }
    assert set(snapshots) == {"ident-email", "ident-phone-new", "ident-phone-old"}
    old_phone = snapshots["ident-phone-old"]
    new_phone = snapshots["ident-phone-new"]
    email = snapshots["ident-email"]
    assert isinstance(old_phone, dict)
    assert isinstance(new_phone, dict)
    assert isinstance(email, dict)
    # The phone reference time is the newest phone observation (2027-01-01),
    # so the older phone link decays over exactly one 365-day half-life.
    assert old_phone["score"] == 1.0
    assert old_phone["exclusivity"] == 1.0
    assert old_phone["rank_score"] == 0.5
    assert new_phone["score"] == 1.0
    assert new_phone["exclusivity"] == 1.0
    assert new_phone["rank_score"] == 1.0
    assert email["score"] == 0.9039
    assert email["exclusivity"] == 0.5
    assert email["rank_score"] == 0.45195
    for properties in (old_phone, new_phone, email):
        assert properties["policy_version"] == "c4-w1"

    before = _link_snapshot(neo4j_driver)
    _execute(
        neo4j_driver,
        lambda tx: recompute_participant_links(tx, "profile", profile_id),
    )
    assert _link_snapshot(neo4j_driver) == before


def test_link_weights_read_conversation_confidence_from_evidence(
    neo4j_driver: Driver,
) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_parked_record(
        neo4j_driver,
        "sr-c1",
        "2026-01-01T00:00:00Z",
        record_type="conversation",
        extraction_confidence=0.8,
    )
    _seed_parked_record(
        neo4j_driver,
        "sr-c2",
        "2026-02-01T00:00:00Z",
        record_type="conversation",
        extraction_confidence=0.8,
    )
    _seed_assertion(neo4j_driver, "sr-c1", "ident-phone", "assert-c1")
    _seed_assertion(neo4j_driver, "sr-c2", "ident-phone", "assert-c2")

    created = _materialize(neo4j_driver, "sr-c2")
    assert created.status == "created"

    links = _link_snapshot(neo4j_driver)
    assert len(links) == 1
    properties = links[0]["properties"]
    assert isinstance(properties, dict)
    # tier_1 drops to tier_2 (0.85) for conversation records:
    # s = 0.85 * 0.8 * 1.15 = 0.782 each, noisy-OR over two records.
    assert properties["score"] == 0.952476
    assert properties["exclusivity"] == 0.5
    assert properties["rank_score"] == 0.476238
    assert properties["policy_version"] == "c4-w1"


def test_golden_top3_pin_drives_preferred_phone(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_person(neo4j_driver, "person-1")
    _seed_identifier(neo4j_driver, "ident-old", "phone", "+6591111111")
    _seed_identifier(neo4j_driver, "ident-new", "phone", "+6592222222")
    _seed_linked_record(
        neo4j_driver,
        "sr-old",
        "person-1",
        link_status="linked",
        observed_at="2026-01-01T00:00:00Z",
    )
    _seed_linked_record(
        neo4j_driver,
        "sr-new",
        "person-1",
        link_status="linked",
        observed_at="2027-01-01T00:00:00Z",
    )
    _seed_assertion(neo4j_driver, "sr-old", "ident-old", "assert-old")
    _seed_assertion(neo4j_driver, "sr-new", "ident-new", "assert-new")
    _set_person_overrides(
        neo4j_driver,
        "person-1",
        json.dumps({"preferred_phone": {"source_record_pk": "sr-old"}}),
    )

    _execute(neo4j_driver, lambda tx: recompute_participant_links(tx, "person", "person-1"))
    _execute(neo4j_driver, lambda tx: compute_golden_profile(tx, "person-1", v2_evidence=True))

    links = {str(row["identifier_id"]): row["properties"] for row in _link_snapshot(neo4j_driver)}
    # The pin outranks the higher rank_score: rank 1 equals preferred_*.
    assert links["ident-old"]["golden_rank"] == 1
    assert links["ident-new"]["golden_rank"] == 2
    persons = _person_golden_snapshot(neo4j_driver)
    assert len(persons) == 1
    assert persons[0]["preferred_phone"] == "+6591111111"
    assert persons[0]["golden_json"] is not None
    assert json.loads(str(persons[0]["golden_json"]))["phone"]["value"] == "+6591111111"

    before_links = _link_snapshot(neo4j_driver)
    before_persons = _person_golden_snapshot(neo4j_driver)
    _execute(neo4j_driver, lambda tx: recompute_participant_links(tx, "person", "person-1"))
    _execute(neo4j_driver, lambda tx: compute_golden_profile(tx, "person-1", v2_evidence=True))
    assert _link_snapshot(neo4j_driver) == before_links
    assert _person_golden_snapshot(neo4j_driver) == before_persons


def test_golden_top3_decay_anchors_per_identifier_type(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_person(neo4j_driver, "person-1")
    _seed_identifier(neo4j_driver, "ident-old", "phone", "+6591111111")
    _seed_identifier(neo4j_driver, "ident-new", "phone", "+6592222222")
    _seed_identifier(neo4j_driver, "ident-email", "email", "shared@example.com")
    _seed_linked_record(
        neo4j_driver,
        "sr-old",
        "person-1",
        link_status="linked",
        observed_at="2026-01-01T00:00:00Z",
    )
    _seed_linked_record(
        neo4j_driver,
        "sr-new",
        "person-1",
        link_status="linked",
        observed_at="2027-01-01T00:00:00Z",
    )
    _seed_assertion(neo4j_driver, "sr-old", "ident-old", "assert-old")
    _seed_assertion(neo4j_driver, "sr-new", "ident-new", "assert-new")
    _seed_assertion(neo4j_driver, "sr-old", "ident-email", "assert-email")

    _execute(neo4j_driver, lambda tx: recompute_participant_links(tx, "person", "person-1"))
    _execute(neo4j_driver, lambda tx: compute_golden_profile(tx, "person-1", v2_evidence=True))

    links = {str(row["identifier_id"]): row["properties"] for row in _link_snapshot(neo4j_driver)}
    assert links["ident-new"]["golden_rank"] == 1
    assert links["ident-old"]["golden_rank"] == 2
    assert links["ident-old"]["rank_score"] == 0.5
    # The email's own-type reference is its own last observation, so it does
    # not decay against the newer phone reference.
    assert links["ident-email"]["golden_rank"] == 1
    assert links["ident-email"]["rank_score"] == 0.69
    persons = _person_golden_snapshot(neo4j_driver)
    assert len(persons) == 1
    assert persons[0]["preferred_phone"] == "+6592222222"
    assert persons[0]["preferred_email"] == "shared@example.com"
    assert persons[0]["golden_conflicts"] == "[]"
    assert persons[0]["golden_json"] is not None
    golden = json.loads(str(persons[0]["golden_json"]))
    assert set(golden) == {"email", "phone"}
    assert golden["phone"]["value"] == "+6592222222"
    assert golden["email"]["value"] == "shared@example.com"

    before_links = _link_snapshot(neo4j_driver)
    before_persons = _person_golden_snapshot(neo4j_driver)
    _execute(neo4j_driver, lambda tx: recompute_participant_links(tx, "person", "person-1"))
    _execute(neo4j_driver, lambda tx: compute_golden_profile(tx, "person-1", v2_evidence=True))
    assert _link_snapshot(neo4j_driver) == before_links
    assert _person_golden_snapshot(neo4j_driver) == before_persons


def test_weighted_snapshot_returns_every_matching_link(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_person(neo4j_driver, "person-1")
    for index in range(1, 6):
        _seed_identifier(neo4j_driver, f"ident-phone-{index}", "phone", f"+659000000{index}")
        _seed_linked_record(neo4j_driver, f"sr-{index}", "person-1", link_status="linked")
        _seed_assertion(neo4j_driver, f"sr-{index}", f"ident-phone-{index}", f"assert-{index}")
    _execute(neo4j_driver, lambda tx: recompute_participant_links(tx, "person", "person-1"))

    snapshots = _execute(
        neo4j_driver,
        lambda tx: fetch_candidate_snapshots(tx, ["person-1"], include_legacy=False),
    )
    phones = snapshots["person-1"].phones_by_value()

    # The snapshot returns all links (no top-3 truncation) with their weights.
    assert len(phones) == 5
    for record in phones.values():
        assert record["link_weight"] == 1.0
        assert record["participant_count"] == 1
    assert snapshots["person-1"].anchored is False


def _seed_match_decision(driver: Driver) -> str:
    with driver.session() as session:
        row = session.run(
            queries.CREATE_MATCH_DECISION,
            engine_type="heuristic",
            engine_version="v0.1.0",
            decision="no_match",
            confidence=0.0,
            reasons=[],
            blocking_conflicts=[],
            review_candidate_person_ids=[],
            feature_snapshot="{}",
            policy_version="c4-v1",
        ).single(strict=True)
    value = row["match_decision_id"]
    assert isinstance(value, str)
    return value


def _create_keyed_case(driver: Driver, match_decision_id: str, case_key_value: str) -> str:
    with driver.session() as session:
        row = session.run(
            queries.CREATE_REVIEW_CASE_BY_KEY,
            case_key=case_key_value,
            reason_code="anchored_ambiguity",
            priority=100,
            sla_due_at="2026-06-01T00:00:00Z",
            match_decision_id=match_decision_id,
        ).single(strict=True)
    value = row["review_case_id"]
    assert isinstance(value, str)
    return value


def _keyed_case_state(driver: Driver, case_key_value: str) -> tuple[str, int, str, int]:
    with driver.session() as session:
        row = session.run(
            "MATCH (rc:ReviewCase {case_key: $case_key}) "
            "RETURN rc.review_case_id AS review_case_id, "
            "rc.occurrence_count AS occurrence_count, "
            "rc.queue_state AS queue_state",
            case_key=case_key_value,
        ).single(strict=True)
        review_case_id = row["review_case_id"]
        occurrence_count = row["occurrence_count"]
        queue_state = row["queue_state"]
        edge_count = session.run(
            "MATCH (:ReviewCase {case_key: $case_key})-[edge:FOR_DECISION]->() "
            "RETURN count(edge) AS count",
            case_key=case_key_value,
        ).single(strict=True)["count"]
    assert isinstance(review_case_id, str)
    assert isinstance(occurrence_count, int)
    assert isinstance(queue_state, str)
    assert isinstance(edge_count, int)
    return review_case_id, occurrence_count, queue_state, edge_count


def test_review_case_by_key_recurs_without_reopening(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    match_decision_id = _seed_match_decision(neo4j_driver)
    case_key_value = case_key("anchored_ambiguity", "record-1", ["person-a", "person-b"])

    first = _create_keyed_case(neo4j_driver, match_decision_id, case_key_value)
    second = _create_keyed_case(neo4j_driver, match_decision_id, case_key_value)
    assert first == second
    assert _keyed_case_state(neo4j_driver, case_key_value) == (first, 2, "open", 1)

    with neo4j_driver.session() as session:
        session.run(
            "MATCH (rc:ReviewCase {case_key: $case_key}) "
            "SET rc.queue_state = 'resolved', rc.resolution = 'confirmed_match', "
            "rc.resolved_at = datetime()",
            case_key=case_key_value,
        ).consume()
    third = _create_keyed_case(neo4j_driver, match_decision_id, case_key_value)
    assert third == first
    assert _keyed_case_state(neo4j_driver, case_key_value) == (first, 3, "resolved", 1)


def test_nric_contradiction_rejection_persists_one_review_case(
    neo4j_driver: Driver,
) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-holder-phone", "phone", _PHONE)
    _seed_person(neo4j_driver, "person-legacy-holder")
    _seed_person_nric(neo4j_driver, "person-legacy-holder", "ident-nric", _NRIC_DIGEST)
    _seed_linked_record(
        neo4j_driver, "sr-legacy-holder", "person-legacy-holder", link_status="linked"
    )
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (person:Person {person_id: 'person-legacy-holder'}) "
            "MATCH (record:SourceRecord {source_record_pk: 'sr-legacy-holder'}) "
            "CREATE (person)-[:HAS_FACT {attribute_name: 'dob', "
            "attribute_value: '1980-02-02', quality_flag: 'valid', is_active: true}]->(record)"
        ).consume()
    _seed_linked_record(neo4j_driver, "sr-incoming", "person-legacy-holder", link_status="linked")
    _seed_parked_record(neo4j_driver, "sr-holder-member", "2026-01-02T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-holder-member", "ident-holder-phone", "assert-holder-phone")
    _seed_asserted_fact(neo4j_driver, "sr-holder-member", "dob", "1990-03-03", "assert-holder-dob")
    _seed_profile(neo4j_driver, "profile-holder-b", "sr-holder-member")
    _seed_profile_link(neo4j_driver, "profile-holder-b", "ident-holder-phone")

    outcome = _execute(
        neo4j_driver,
        lambda tx: promote_profiles_for_nric(
            tx,
            _nric_identifier(),
            identifier_keys=[
                IdentifierKey(
                    identifier_type="phone",
                    identifier_scope="global",
                    normalized_value=_PHONE,
                )
            ],
            source_key="onediver",
            record_type=RecordType.IDENTITY,
            incoming=NameDob(full_name=None, dob=None),
        ),
    )

    assert outcome.promoted_profile_ids == ()
    assert len(outcome.contradictions) == 1
    assert outcome.contradictions[0].profile_id == "profile-holder-b"
    assert outcome.contradictions[0].holder_person_id == "person-legacy-holder"

    review_case_id = _execute(
        neo4j_driver,
        lambda tx: persist_nric_contradiction_review(
            tx,
            source_record_pk="sr-incoming",
            profile_id="profile-holder-b",
            holder_person_id="person-legacy-holder",
            source_system="onediver",
        ),
    )
    assert isinstance(review_case_id, str)
    with neo4j_driver.session() as session:
        reason = session.run(
            "MATCH (rc:ReviewCase {review_case_id: $review_case_id}) "
            "RETURN rc.reason_code AS reason_code",
            review_case_id=review_case_id,
        ).single(strict=True)["reason_code"]
        assert reason == "nric_contradiction"
        left = session.run(
            "MATCH (:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->"
            "(md:MatchDecision)-[:ABOUT_LEFT]->(record:SourceRecord) "
            "RETURN record.source_record_pk AS pk",
            review_case_id=review_case_id,
        ).single(strict=True)["pk"]
        assert left == "sr-incoming"
        right = session.run(
            "MATCH (:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->"
            "(md:MatchDecision)-[:ABOUT_RIGHT]->(person:Person) "
            "RETURN person.person_id AS person_id",
            review_case_id=review_case_id,
        ).single(strict=True)["person_id"]
        assert right == "person-legacy-holder"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:ReviewCase {reason_code: 'nric_contradiction'}) RETURN count(*) AS count",
        )
        == 1
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
    *,
    incoming: NameDob | None = None,
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
            incoming=incoming or NameDob(full_name="Alice Tan", dob="1990-01-01"),
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
    observed_at: str = "2026-01-01T00:00:00Z",
) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (person:Person {person_id: $person_id}) "
            "CREATE (record:SourceRecord {source_record_pk: $source_record_pk, "
            "lifecycle_status: 'parked', link_status: $link_status, "
            "attribution_status: 'person', reeval_pending: false, "
            "record_type: $record_type, "
            "observed_at: datetime($observed_at)})"
            "-[:LINKED_TO {linked_at: datetime()}]->(person)",
            source_record_pk=source_record_pk,
            person_id=person_id,
            link_status=link_status,
            record_type=record_type,
            observed_at=observed_at,
        ).consume()


def _set_person_overrides(driver: Driver, person_id: str, overrides: str) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (person:Person {person_id: $person_id}) "
            "SET person.survivorship_overrides = $overrides",
            person_id=person_id,
            overrides=overrides,
        ).consume()


def _person_golden_snapshot(driver: Driver) -> list[dict[str, object]]:
    with driver.session() as session:
        rows = [
            dict(row)
            for row in session.run(
                "MATCH (person:Person) "
                "RETURN person.person_id AS person_id, "
                "person.preferred_phone AS preferred_phone, "
                "person.preferred_email AS preferred_email, "
                "person.golden_json AS golden_json, "
                "person.golden_conflicts AS golden_conflicts"
            )
        ]
    return sorted(rows, key=lambda row: str(row["person_id"]))


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
    _seed_address(neo4j_driver)
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

    assert promoted.promoted_profile_ids == ("profile-phone",)
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
            source_record_pks=["sr-p1", "sr-p2"],
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
            source_record_pk="sr-shadow-staged",
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
    # Rankable links need active evidencing records: seed them and recompute
    # so the v2 golden stage derives rank-1 picks from real C4 link weights.
    _seed_linked_record(neo4j_driver, "sr-phone", "person-has-only", link_status="linked")
    _seed_linked_record(neo4j_driver, "sr-email", "person-has-only", link_status="linked")
    _seed_assertion(neo4j_driver, "sr-phone", "ident-phone", "assert-phone")
    _seed_assertion(neo4j_driver, "sr-email", "ident-email", "assert-email")
    _execute(
        neo4j_driver,
        lambda tx: recompute_participant_links(tx, "person", "person-has-only"),
    )

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
    _seed_identifier(neo4j_driver, "ident-v1-email", "email", "v1-only@example.com")
    _seed_parked_record(
        neo4j_driver,
        "sr-v1",
        "2026-01-01T00:00:00Z",
        source_record_id="dup-ident",
        source_record_version=1,
        record_hash="sha256:v1",
    )
    _seed_parked_record(
        neo4j_driver,
        "sr-v2",
        "2026-02-01T00:00:00Z",
        source_record_id="dup-ident",
        source_record_version=2,
        record_hash="sha256:v2",
    )
    for source_record_pk in ("sr-v1", "sr-v2"):
        _seed_assertion(neo4j_driver, source_record_pk, "ident-phone", f"assert-{source_record_pk}")
    _seed_assertion(neo4j_driver, "sr-v1", "ident-v1-email", "assert-v1-email")
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
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (record:SourceRecord {source_record_pk: 'sr-v1'}) "
            "WHERE record.lifecycle_status = 'superseded' AND record.is_latest = false "
            "AND record.reeval_pending = false "
            "RETURN count(record) AS count",
        )
        == 1
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-v1'})"
            "-[:PREVIOUS_VERSION_OF]->(:SourceRecord {source_record_pk: 'sr-v2'}) "
            "RETURN count(*) AS count",
        )
        == 1
    )
    # The superseded member's sole key is still refreshed by the promotion's
    # participant-count pass: no Person, open Profile, or parked record remains.
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (identifier:Identifier {identifier_id: 'ident-v1-email'}) "
            "RETURN coalesce(identifier.participant_count, -1) AS count",
        )
        == 0
    )
    state = _execute(
        neo4j_driver,
        lambda tx: load_locked_source_state(tx, "onediver", "dup-ident", "legacy-default"),
    )
    assert state.active is not None
    assert state.active.source_record_pk == "sr-v2"
    assert state.pending is None
    assert person_id


def test_promotion_reopens_member_when_a_lower_version_is_active(
    neo4j_driver: Driver,
) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_identifier(neo4j_driver, "ident-reopen-email", "email", "reopen-only@example.com")
    _seed_parked_record(
        neo4j_driver,
        "sr-reopen",
        "2026-02-01T00:00:00Z",
        source_record_id="reopen-ident",
        source_record_version=2,
        record_hash="sha256:reopen-v2",
    )
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (system:SourceSystem {source_key: 'onediver'}) "
            "CREATE (record:SourceRecord {source_record_pk: 'sr-active-v1', "
            "source_record_id: 'reopen-ident', source_instance_id: 'legacy-default', "
            "source_record_version: 1, record_hash: 'sha256:reopen-v1', "
            "lifecycle_status: 'active', link_status: 'linked', "
            "attribution_status: 'person', is_latest: true, reeval_pending: false, "
            "observed_at: datetime('2026-01-01T00:00:00Z')})-[:FROM_SOURCE]->(system)"
        ).consume()
    _seed_parked_record(
        neo4j_driver, "sr-activate", "2026-01-02T00:00:00Z", record_hash="sha256:activate"
    )
    _seed_assertion(neo4j_driver, "sr-reopen", "ident-phone", "assert-reopen-phone")
    _seed_assertion(neo4j_driver, "sr-reopen", "ident-reopen-email", "assert-reopen-email")
    _seed_assertion(neo4j_driver, "sr-activate", "ident-phone", "assert-activate-phone")
    _seed_profile(neo4j_driver, "profile-reopen", "sr-reopen")
    _attach_profile_member(neo4j_driver, "profile-reopen", "sr-activate")
    _seed_profile_link(neo4j_driver, "profile-reopen", "ident-phone")

    outcome = _execute(
        neo4j_driver,
        lambda tx: promote_profile(
            tx,
            profile_id="profile-reopen",
            identifier=_nric_identifier(),
            source_key="onediver",
            record_type=RecordType.IDENTITY,
            incoming=NameDob(full_name="Alice Tan", dob="1990-01-01"),
        ),
    )

    assert outcome.status == "promoted"
    records = {
        str(row["source_record_pk"]): row for row in _record_attribution_snapshot(neo4j_driver)
    }
    reopened = records["sr-reopen"]
    assert reopened["lifecycle_status"] == "parked"
    assert reopened["link_status"] == "unlinked"
    assert reopened["attribution_status"] == "parked"
    assert reopened["reeval_pending"] is True
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-reopen'})"
            "-[:IN_PROFILE]->(:Profile {profile_id: 'profile-reopen'}) "
            "RETURN count(*) AS count",
        )
        == 0
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-reopen'})"
            "-[:LINKED_TO]->(:Person) RETURN count(*) AS count",
        )
        == 0
    )
    assert records["sr-activate"]["link_status"] == "linked"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-activate'})"
            "-[:LINKED_TO]->(:Person) RETURN count(*) AS count",
        )
        == 1
    )
    # Reopened as an unlinked parked candidate, discoverable for a later record.
    _people, participants = _participants(neo4j_driver, [_phone_identifier()], "later-record")
    assert ("source_record", "sr-reopen") in {
        (item.participant_kind, item.participant_id) for item in participants
    }
    # Its sole email key is counted from the reopened parked record.
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (identifier:Identifier {identifier_id: 'ident-reopen-email'}) "
            "RETURN coalesce(identifier.participant_count, -1) AS count",
        )
        == 1
    )


def test_promotion_reopens_member_asserting_a_different_nric(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-other-nric", "nric", _OTHER_DIGEST, checksum_valid=True)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_parked_record(neo4j_driver, "sr-conflict", "2026-01-01T00:00:00Z")
    _seed_parked_record(neo4j_driver, "sr-ok", "2026-01-02T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-conflict", "ident-phone", "assert-conflict-phone")
    _seed_assertion(neo4j_driver, "sr-conflict", "ident-other-nric", "assert-other-nric")
    _seed_assertion(neo4j_driver, "sr-ok", "ident-phone", "assert-ok-phone")
    _seed_profile(neo4j_driver, "profile-conflict", "sr-conflict")
    _attach_profile_member(neo4j_driver, "profile-conflict", "sr-ok")
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

    assert outcome.status == "promoted"
    records = {
        str(row["source_record_pk"]): row for row in _record_attribution_snapshot(neo4j_driver)
    }
    reopened = records["sr-conflict"]
    assert reopened["lifecycle_status"] == "parked"
    assert reopened["link_status"] == "unlinked"
    assert reopened["attribution_status"] == "parked"
    assert reopened["reeval_pending"] is True
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-conflict'})"
            "-[:IN_PROFILE]->(:Profile {profile_id: 'profile-conflict'}) "
            "RETURN count(*) AS count",
        )
        == 0
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-conflict'})"
            "-[:LINKED_TO]->(:Person) RETURN count(*) AS count",
        )
        == 0
    )
    assert records["sr-ok"]["link_status"] == "linked"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-ok'})"
            "-[:LINKED_TO]->(:Person) RETURN count(*) AS count",
        )
        == 1
    )
    # The reopened member is the sole asserter of its NRIC key, so the refresh
    # pass counts exactly one parked record.
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (identifier:Identifier {identifier_id: 'ident-other-nric'}) "
            "RETURN coalesce(identifier.participant_count, -1) AS count",
        )
        == 1
    )


def test_promotion_excludes_member_with_contradictory_dob(neo4j_driver: Driver) -> None:
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

    # The only member contradicts the incoming holder facts, so nothing remains
    # to promote and the Profile is left untouched.
    assert outcome.status == "rejected"
    assert outcome.reason == "no_promotable_members"
    profiles = {str(row["profile_id"]): row for row in _profile_snapshot(neo4j_driver)}
    assert profiles["profile-dob"]["status"] == "open"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord)-[:LINKED_TO]->(:Person) RETURN count(*) AS count",
        )
        == 0
    )


def test_promotion_rejects_cross_member_dob_contradiction(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_parked_record(neo4j_driver, "sr-a", "2026-01-01T00:00:00Z")
    _seed_parked_record(neo4j_driver, "sr-b", "2026-01-02T00:00:00Z")
    for source_record_pk in ("sr-a", "sr-b"):
        _seed_assertion(neo4j_driver, source_record_pk, "ident-phone", f"assert-{source_record_pk}")
    _seed_asserted_fact(neo4j_driver, "sr-a", "dob", "1980-02-02", "assert-a-dob")
    _seed_asserted_fact(neo4j_driver, "sr-b", "dob", "1970-03-03", "assert-b-dob")
    _seed_profile(neo4j_driver, "profile-pair", "sr-a")
    _attach_profile_member(neo4j_driver, "profile-pair", "sr-b")
    _seed_profile_link(neo4j_driver, "profile-pair", "ident-phone")

    outcome = _execute(
        neo4j_driver,
        lambda tx: promote_profile(
            tx,
            profile_id="profile-pair",
            identifier=_nric_identifier(),
            source_key="onediver",
            record_type=RecordType.IDENTITY,
            incoming=NameDob(full_name=None, dob=None),
        ),
    )

    assert outcome.status == "rejected"
    assert outcome.reason == "nric_contradiction"
    profiles = {str(row["profile_id"]): row for row in _profile_snapshot(neo4j_driver)}
    assert profiles["profile-pair"]["status"] == "open"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord)-[:LINKED_TO]->(:Person) RETURN count(*) AS count",
        )
        == 0
    )
    assert _scalar(neo4j_driver, "MATCH (person:Person) RETURN count(person) AS count") == 0
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord)-[:IN_PROFILE]->"
            "(:Profile {profile_id: 'profile-pair'}) RETURN count(*) AS count",
        )
        == 2
    )


def test_promotion_supersedes_parked_member_over_any_higher_version(
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
        source_record_id="version-ident",
        source_record_version=1,
    )
    _seed_source_version(
        neo4j_driver,
        "sr-v2",
        "2026-02-01T00:00:00Z",
        source_record_id="version-ident",
        source_record_version=2,
        lifecycle_status="rejected",
    )
    _seed_parked_record(neo4j_driver, "sr-ok", "2026-01-02T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-v1", "ident-phone", "assert-v1-phone")
    _seed_assertion(neo4j_driver, "sr-ok", "ident-phone", "assert-ok-phone")
    _seed_profile(neo4j_driver, "profile-version", "sr-v1")
    _attach_profile_member(neo4j_driver, "profile-version", "sr-ok")
    _seed_profile_link(neo4j_driver, "profile-version", "ident-phone")

    outcome = _promote(neo4j_driver, "profile-version")

    assert outcome.status == "promoted"
    records = {
        str(row["source_record_pk"]): row for row in _record_attribution_snapshot(neo4j_driver)
    }
    assert records["sr-v1"]["lifecycle_status"] == "superseded"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-v1'})"
            "-[:PREVIOUS_VERSION_OF]->(:SourceRecord {source_record_pk: 'sr-v2'}) "
            "RETURN count(*) AS count",
        )
        == 1
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-v1'})"
            "-[:LINKED_TO]->(:Person) RETURN count(*) AS count",
        )
        == 0
    )
    assert records["sr-ok"]["link_status"] == "linked"


def test_promotion_reopens_member_omitted_by_activation_guard(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_parked_record(
        neo4j_driver,
        "sr-member",
        "2026-01-01T00:00:00Z",
        source_record_id="guard-ident",
        source_record_version=1,
    )
    _seed_source_version(
        neo4j_driver,
        "sr-open",
        "2026-02-01T00:00:00Z",
        source_record_id="guard-ident",
        source_record_version=2,
        lifecycle_status="active",
        is_latest=True,
    )
    _seed_assertion(neo4j_driver, "sr-member", "ident-phone", "assert-member-phone")
    _seed_profile(neo4j_driver, "profile-guard", "sr-member")
    _seed_profile_link(neo4j_driver, "profile-guard", "ident-phone")

    def _force_activate(
        _tx: ManagedTransaction,
        _profile_id: str,
        members: list[MemberEvidence],
    ) -> list[MemberDisposition]:
        return [MemberDisposition(member.source_record_pk, "activate", None) for member in members]

    monkeypatch.setattr(promotion_module, "member_dispositions", _force_activate)

    outcome = _promote(neo4j_driver, "profile-guard")

    assert outcome.status == "promoted"
    records = {
        str(row["source_record_pk"]): row for row in _record_attribution_snapshot(neo4j_driver)
    }
    reopened = records["sr-member"]
    assert reopened["lifecycle_status"] == "parked"
    assert reopened["link_status"] == "unlinked"
    assert reopened["attribution_status"] == "parked"
    assert reopened["reeval_pending"] is True
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-member'})"
            "-[:IN_PROFILE]->(:Profile {profile_id: 'profile-guard'}) "
            "RETURN count(*) AS count",
        )
        == 0
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-member'})"
            "-[:LINKED_TO]->(:Person) RETURN count(*) AS count",
        )
        == 0
    )


def test_promotion_supersedes_excluded_member_with_a_higher_version(
    neo4j_driver: Driver,
) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-other-nric", "nric", _OTHER_DIGEST, checksum_valid=True)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_parked_record(
        neo4j_driver,
        "sr-excluded",
        "2026-01-01T00:00:00Z",
        source_record_id="excluded-ident",
        source_record_version=1,
    )
    _seed_source_version(
        neo4j_driver,
        "sr-excluded-v2",
        "2026-02-01T00:00:00Z",
        source_record_id="excluded-ident",
        source_record_version=2,
        lifecycle_status="rejected",
    )
    _seed_parked_record(neo4j_driver, "sr-eligible", "2026-01-02T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-excluded", "ident-phone", "assert-excluded-phone")
    _seed_assertion(neo4j_driver, "sr-excluded", "ident-other-nric", "assert-excluded-nric")
    _seed_assertion(neo4j_driver, "sr-eligible", "ident-phone", "assert-eligible-phone")
    _seed_profile(neo4j_driver, "profile-excluded", "sr-excluded")
    _attach_profile_member(neo4j_driver, "profile-excluded", "sr-eligible")
    _seed_profile_link(neo4j_driver, "profile-excluded", "ident-phone")

    outcome = _promote(neo4j_driver, "profile-excluded")

    assert outcome.status == "promoted"
    records = {
        str(row["source_record_pk"]): row for row in _record_attribution_snapshot(neo4j_driver)
    }
    assert records["sr-excluded"]["lifecycle_status"] == "superseded"
    assert records["sr-excluded"]["link_status"] == "profile"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-excluded'})"
            "-[:PREVIOUS_VERSION_OF]->(:SourceRecord {source_record_pk: 'sr-excluded-v2'}) "
            "RETURN count(*) AS count",
        )
        == 1
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-excluded'})"
            "-[:IN_PROFILE]->(:Profile {profile_id: 'profile-excluded'}) "
            "RETURN count(*) AS count",
        )
        == 1
    )
    assert records["sr-eligible"]["link_status"] == "linked"


def test_promotion_leaves_reviewer_activated_member_untouched(
    neo4j_driver: Driver,
) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_source_version(
        neo4j_driver,
        "sr-review-open",
        "2026-01-01T00:00:00Z",
        source_record_id="review-ident",
        source_record_version=1,
        lifecycle_status="active",
        is_latest=True,
    )
    _seed_source_version(
        neo4j_driver,
        "sr-review-member",
        "2026-02-01T00:00:00Z",
        source_record_id="review-ident",
        source_record_version=2,
        lifecycle_status="active",
        link_status="linked",
        attribution_status="person",
        is_latest=True,
    )
    _seed_parked_record(neo4j_driver, "sr-review-ok", "2026-01-02T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-review-ok", "ident-phone", "assert-review-ok")
    _seed_profile(neo4j_driver, "profile-review", "sr-review-member")
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (record:SourceRecord {source_record_pk: 'sr-review-member'}) "
            "SET record.link_status = 'linked', record.attribution_status = 'person'"
        ).consume()
    _attach_profile_member(neo4j_driver, "profile-review", "sr-review-ok")
    _seed_profile_link(neo4j_driver, "profile-review", "ident-phone")

    outcome = _promote(neo4j_driver, "profile-review")

    assert outcome.status == "promoted"
    records = {
        str(row["source_record_pk"]): row for row in _record_attribution_snapshot(neo4j_driver)
    }
    untouched = records["sr-review-member"]
    assert untouched["lifecycle_status"] == "active"
    assert untouched["link_status"] == "linked"
    assert untouched["attribution_status"] == "person"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-review-member'})"
            "-[:IN_PROFILE]->(:Profile {profile_id: 'profile-review'}) "
            "RETURN count(*) AS count",
        )
        == 1
    )


def test_promotion_stops_at_first_profile_whose_members_contradict_prior(
    neo4j_driver: Driver,
) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-email-a", "email", "member-a@example.com")
    _seed_identifier(neo4j_driver, "ident-phone-b", "phone", _PHONE)
    _seed_parked_record(neo4j_driver, "sr-member-a", "2026-01-01T00:00:00Z")
    _seed_parked_record(neo4j_driver, "sr-member-b", "2026-01-02T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-member-a", "ident-email-a", "assert-member-a")
    _seed_assertion(neo4j_driver, "sr-member-b", "ident-phone-b", "assert-member-b")
    _seed_asserted_fact(neo4j_driver, "sr-member-a", "dob", "1980-02-02", "assert-member-a-dob")
    _seed_asserted_fact(neo4j_driver, "sr-member-b", "dob", "1990-03-03", "assert-member-b-dob")
    _seed_profile(neo4j_driver, "profile-a", "sr-member-a")
    _seed_profile_link(neo4j_driver, "profile-a", "ident-email-a")
    _seed_profile(neo4j_driver, "profile-b", "sr-member-b")
    _seed_profile_link(neo4j_driver, "profile-b", "ident-phone-b")

    promoted = _execute(
        neo4j_driver,
        lambda tx: promote_profiles_for_nric(
            tx,
            _nric_identifier(),
            identifier_keys=[
                IdentifierKey(
                    identifier_type="email",
                    identifier_scope="global",
                    normalized_value="member-a@example.com",
                ),
                IdentifierKey(
                    identifier_type="phone",
                    identifier_scope="global",
                    normalized_value=_PHONE,
                ),
            ],
            source_key="onediver",
            record_type=RecordType.IDENTITY,
            incoming=NameDob(full_name=None, dob=None),
        ),
    )

    assert promoted.promoted_profile_ids == ("profile-a",)
    profiles = {str(row["profile_id"]): row for row in _profile_snapshot(neo4j_driver)}
    assert profiles["profile-a"]["status"] == "promoted"
    assert profiles["profile-b"]["status"] == "open"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-member-b'})"
            "-[:LINKED_TO]->(:Person) RETURN count(*) AS count",
        )
        == 0
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-member-b'})"
            "-[:IN_PROFILE]->(:Profile {profile_id: 'profile-b'}) "
            "RETURN count(*) AS count",
        )
        == 1
    )


def test_promotion_ignores_superseded_member_contradiction(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_parked_record(
        neo4j_driver,
        "sr-corrected-v1",
        "2026-01-01T00:00:00Z",
        source_record_id="corrected-ident",
        source_record_version=1,
    )
    _seed_parked_record(
        neo4j_driver,
        "sr-corrected-v2",
        "2026-02-01T00:00:00Z",
        source_record_id="corrected-ident",
        source_record_version=2,
    )
    for source_record_pk in ("sr-corrected-v1", "sr-corrected-v2"):
        _seed_assertion(neo4j_driver, source_record_pk, "ident-phone", f"assert-{source_record_pk}")
    _seed_asserted_fact(
        neo4j_driver, "sr-corrected-v1", "dob", "1980-02-02", "assert-corrected-v1-dob"
    )
    _seed_asserted_fact(
        neo4j_driver, "sr-corrected-v2", "dob", "1990-03-03", "assert-corrected-v2-dob"
    )
    _seed_profile(neo4j_driver, "profile-corrected", "sr-corrected-v1")
    _attach_profile_member(neo4j_driver, "profile-corrected", "sr-corrected-v2")
    _seed_profile_link(neo4j_driver, "profile-corrected", "ident-phone")

    outcome = _promote(
        neo4j_driver,
        "profile-corrected",
        incoming=NameDob(full_name=None, dob=None),
    )

    assert outcome.status == "promoted"
    records = {
        str(row["source_record_pk"]): row for row in _record_attribution_snapshot(neo4j_driver)
    }
    assert records["sr-corrected-v1"]["lifecycle_status"] == "superseded"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-corrected-v1'})"
            "-[:PREVIOUS_VERSION_OF]->(:SourceRecord {source_record_pk: 'sr-corrected-v2'}) "
            "RETURN count(*) AS count",
        )
        == 1
    )
    assert records["sr-corrected-v2"]["link_status"] == "linked"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (record:SourceRecord {source_record_pk: 'sr-corrected-v2'}) "
            "WHERE record.lifecycle_status = 'active' AND record.is_latest = true "
            "RETURN count(record) AS count",
        )
        == 1
    )


def test_promotion_skips_member_owned_by_its_own_lifecycle(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_source_version(
        neo4j_driver,
        "sr-pending",
        "2026-01-01T00:00:00Z",
        source_record_id="pending-ident",
        source_record_version=1,
        lifecycle_status="pending_review",
    )
    _seed_parked_record(neo4j_driver, "sr-parked-ok", "2026-01-02T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-parked-ok", "ident-phone", "assert-parked-ok")
    _seed_profile(neo4j_driver, "profile-lifecycle", "sr-pending")
    _attach_profile_member(neo4j_driver, "profile-lifecycle", "sr-parked-ok")
    _seed_profile_link(neo4j_driver, "profile-lifecycle", "ident-phone")

    outcome = _promote(neo4j_driver, "profile-lifecycle")

    assert outcome.status == "promoted"
    records = {
        str(row["source_record_pk"]): row for row in _record_attribution_snapshot(neo4j_driver)
    }
    skipped = records["sr-pending"]
    assert skipped["lifecycle_status"] == "pending_review"
    assert skipped["link_status"] == "profile"
    assert skipped["attribution_status"] == "profile"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-pending'})"
            "-[:LINKED_TO]->(:Person) RETURN count(*) AS count",
        )
        == 0
    )
    assert records["sr-parked-ok"]["link_status"] == "linked"


def test_promotion_supersedes_member_with_open_lower_and_higher_version(
    neo4j_driver: Driver,
) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-phone", "phone", _PHONE)
    _seed_parked_record(
        neo4j_driver,
        "sr-winner-v2",
        "2026-02-01T00:00:00Z",
        source_record_id="winner-ident",
        source_record_version=2,
    )
    _seed_source_version(
        neo4j_driver,
        "sr-winner-v1",
        "2026-01-01T00:00:00Z",
        source_record_id="winner-ident",
        source_record_version=1,
        lifecycle_status="active",
        is_latest=True,
    )
    _seed_parked_record(
        neo4j_driver,
        "sr-winner-v3",
        "2026-03-01T00:00:00Z",
        source_record_id="winner-ident",
        source_record_version=3,
    )
    _seed_assertion(neo4j_driver, "sr-winner-v2", "ident-phone", "assert-winner-v2")
    _seed_profile(neo4j_driver, "profile-winner", "sr-winner-v2")
    _seed_profile_link(neo4j_driver, "profile-winner", "ident-phone")

    outcome = _promote(neo4j_driver, "profile-winner")

    assert outcome.status == "rejected"
    assert outcome.reason == "no_promotable_members"
    records = {
        str(row["source_record_pk"]): row for row in _record_attribution_snapshot(neo4j_driver)
    }
    assert records["sr-winner-v2"]["lifecycle_status"] == "superseded"
    assert records["sr-winner-v2"]["link_status"] == "profile"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-winner-v2'})"
            "-[:PREVIOUS_VERSION_OF]->(:SourceRecord {source_record_pk: 'sr-winner-v3'}) "
            "RETURN count(*) AS count",
        )
        == 1
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-winner-v2'})"
            "-[:LINKED_TO]->(:Person) RETURN count(*) AS count",
        )
        == 0
    )


def test_promotion_rejects_durable_person_contradiction_on_second_call(
    neo4j_driver: Driver,
) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-email-a", "email", "durable-a@example.com")
    _seed_identifier(neo4j_driver, "ident-phone-b", "phone", _PHONE)
    _seed_parked_record(neo4j_driver, "sr-durable-a", "2026-01-01T00:00:00Z")
    _seed_parked_record(neo4j_driver, "sr-durable-b", "2026-01-02T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-durable-a", "ident-email-a", "assert-durable-a")
    _seed_assertion(neo4j_driver, "sr-durable-b", "ident-phone-b", "assert-durable-b")
    _seed_asserted_fact(neo4j_driver, "sr-durable-a", "dob", "1980-02-02", "assert-durable-a-dob")
    _seed_asserted_fact(neo4j_driver, "sr-durable-b", "dob", "1990-03-03", "assert-durable-b-dob")
    _seed_profile(neo4j_driver, "profile-durable-a", "sr-durable-a")
    _seed_profile_link(neo4j_driver, "profile-durable-a", "ident-email-a")
    _seed_profile(neo4j_driver, "profile-durable-b", "sr-durable-b")
    _seed_profile_link(neo4j_driver, "profile-durable-b", "ident-phone-b")
    email_key = IdentifierKey(
        identifier_type="email",
        identifier_scope="global",
        normalized_value="durable-a@example.com",
    )
    phone_key = IdentifierKey(
        identifier_type="phone", identifier_scope="global", normalized_value=_PHONE
    )
    dob_less = NameDob(full_name=None, dob=None)

    first = _execute(
        neo4j_driver,
        lambda tx: promote_profiles_for_nric(
            tx,
            _nric_identifier(),
            identifier_keys=[email_key],
            source_key="onediver",
            record_type=RecordType.IDENTITY,
            incoming=dob_less,
        ),
    )
    assert first.promoted_profile_ids == ("profile-durable-a",)

    second = _execute(
        neo4j_driver,
        lambda tx: promote_profiles_for_nric(
            tx,
            _nric_identifier(),
            identifier_keys=[phone_key],
            source_key="onediver",
            record_type=RecordType.IDENTITY,
            incoming=dob_less,
        ),
    )
    assert second.promoted_profile_ids == ()
    profiles = {str(row["profile_id"]): row for row in _profile_snapshot(neo4j_driver)}
    assert profiles["profile-durable-a"]["status"] == "promoted"
    assert profiles["profile-durable-b"]["status"] == "open"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-durable-b'})"
            "-[:LINKED_TO]->(:Person) RETURN count(*) AS count",
        )
        == 0
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-durable-b'})"
            "-[:IN_PROFILE]->(:Profile {profile_id: 'profile-durable-b'}) "
            "RETURN count(*) AS count",
        )
        == 1
    )


def test_promotion_rejects_legacy_person_fact_contradiction(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-holder-phone", "phone", _PHONE)
    _seed_person(neo4j_driver, "person-legacy-holder")
    _seed_person_nric(neo4j_driver, "person-legacy-holder", "ident-nric", _NRIC_DIGEST)
    _seed_linked_record(
        neo4j_driver, "sr-legacy-holder", "person-legacy-holder", link_status="linked"
    )
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (person:Person {person_id: 'person-legacy-holder'}) "
            "MATCH (record:SourceRecord {source_record_pk: 'sr-legacy-holder'}) "
            "CREATE (person)-[:HAS_FACT {attribute_name: 'dob', "
            "attribute_value: '1980-02-02', quality_flag: 'valid', is_active: true}]->(record)"
        ).consume()
    _seed_parked_record(neo4j_driver, "sr-holder-member", "2026-01-02T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-holder-member", "ident-holder-phone", "assert-holder-phone")
    _seed_asserted_fact(neo4j_driver, "sr-holder-member", "dob", "1990-03-03", "assert-holder-dob")
    _seed_profile(neo4j_driver, "profile-holder-b", "sr-holder-member")
    _seed_profile_link(neo4j_driver, "profile-holder-b", "ident-holder-phone")

    promoted = _execute(
        neo4j_driver,
        lambda tx: promote_profiles_for_nric(
            tx,
            _nric_identifier(),
            identifier_keys=[
                IdentifierKey(
                    identifier_type="phone",
                    identifier_scope="global",
                    normalized_value=_PHONE,
                )
            ],
            source_key="onediver",
            record_type=RecordType.IDENTITY,
            incoming=NameDob(full_name=None, dob=None),
        ),
    )

    assert promoted.promoted_profile_ids == ()
    profiles = {str(row["profile_id"]): row for row in _profile_snapshot(neo4j_driver)}
    assert profiles["profile-holder-b"]["status"] == "open"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-holder-member'})"
            "-[:LINKED_TO]->(:Person) RETURN count(*) AS count",
        )
        == 0
    )


def test_promotion_rejects_member_asserting_conflicting_dobs(neo4j_driver: Driver) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-multi-phone", "phone", _PHONE)
    _seed_parked_record(neo4j_driver, "sr-multi-dob", "2026-01-01T00:00:00Z")
    _seed_assertion(neo4j_driver, "sr-multi-dob", "ident-multi-phone", "assert-multi-phone")
    _seed_asserted_fact(neo4j_driver, "sr-multi-dob", "dob", "1980-02-02", "assert-multi-dob-a")
    _seed_asserted_fact(neo4j_driver, "sr-multi-dob", "dob", "1990-03-03", "assert-multi-dob-b")
    _seed_profile(neo4j_driver, "profile-multi-dob", "sr-multi-dob")
    _seed_profile_link(neo4j_driver, "profile-multi-dob", "ident-multi-phone")

    outcome = _promote(
        neo4j_driver,
        "profile-multi-dob",
        incoming=NameDob(full_name=None, dob=None),
    )

    assert outcome.status == "rejected"
    assert outcome.reason == "nric_contradiction"
    profiles = {str(row["profile_id"]): row for row in _profile_snapshot(neo4j_driver)}
    assert profiles["profile-multi-dob"]["status"] == "open"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord)-[:LINKED_TO]->(:Person) RETURN count(*) AS count",
        )
        == 0
    )


def test_no_activatable_promotion_supersedes_and_detaches_obsolete_member(
    neo4j_driver: Driver,
) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_identifier(neo4j_driver, "ident-nric", "nric", _NRIC_DIGEST)
    _seed_identifier(neo4j_driver, "ident-obsolete-email", "email", "obsolete@example.com")
    _seed_parked_record(
        neo4j_driver,
        "sr-obsolete",
        "2026-01-01T00:00:00Z",
        source_record_id="obsolete-ident",
        source_record_version=1,
    )
    _seed_source_version(
        neo4j_driver,
        "sr-obsolete-v2",
        "2026-02-01T00:00:00Z",
        source_record_id="obsolete-ident",
        source_record_version=2,
        lifecycle_status="rejected",
    )
    _seed_assertion(neo4j_driver, "sr-obsolete", "ident-obsolete-email", "assert-obsolete-email")
    _seed_profile(neo4j_driver, "profile-obsolete", "sr-obsolete")
    _seed_profile_link(neo4j_driver, "profile-obsolete", "ident-obsolete-email")

    outcome = _promote(neo4j_driver, "profile-obsolete")

    assert outcome.status == "rejected"
    assert outcome.reason == "no_promotable_members"
    records = {
        str(row["source_record_pk"]): row for row in _record_attribution_snapshot(neo4j_driver)
    }
    assert records["sr-obsolete"]["lifecycle_status"] == "superseded"
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-obsolete'})"
            "-[:PREVIOUS_VERSION_OF]->(:SourceRecord {source_record_pk: 'sr-obsolete-v2'}) "
            "RETURN count(*) AS count",
        )
        == 1
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-obsolete'})"
            "-[:IN_PROFILE]->(:Profile {profile_id: 'profile-obsolete'}) "
            "RETURN count(*) AS count",
        )
        == 0
    )
    profiles = {str(row["profile_id"]): row for row in _profile_snapshot(neo4j_driver)}
    assert profiles["profile-obsolete"]["status"] == "open"
    assert profiles["profile-obsolete"]["evidence_version"] == 2
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:Profile {profile_id: 'profile-obsolete'})-[:HAS_IDENTIFIER]->() "
            "RETURN count(*) AS count",
        )
        == 0
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (identifier:Identifier {identifier_id: 'ident-obsolete-email'}) "
            "RETURN coalesce(identifier.participant_count, -1) AS count",
        )
        == 0
    )


def test_over_bound_materialization_detaches_existing_profile_member(
    neo4j_driver: Driver,
) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_over_bound_cluster(neo4j_driver)
    _seed_profile(neo4j_driver, "profile-bound", "sr-cap-incoming")
    _seed_profile_link(neo4j_driver, "profile-bound", "ident-cap-0")

    outcome = _materialize(neo4j_driver, "sr-cap-incoming")

    assert outcome.status == "parked"
    assert outcome.profile_id is None
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-cap-incoming'})"
            "-[:IN_PROFILE]->(:Profile {profile_id: 'profile-bound'}) "
            "RETURN count(*) AS count",
        )
        == 0
    )
    profiles = {str(row["profile_id"]): row for row in _profile_snapshot(neo4j_driver)}
    assert profiles["profile-bound"]["evidence_version"] == 2
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:Profile {profile_id: 'profile-bound'})-[:HAS_IDENTIFIER]->() "
            "RETURN count(*) AS count",
        )
        == 0
    )
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (identifier:Identifier {identifier_id: 'ident-cap-0'}) "
            "RETURN coalesce(identifier.participant_count, -1) AS count",
        )
        == 25
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


def _seed_over_bound_cluster(driver: Driver) -> None:
    """Seed 217 chained cluster records across nine under-cap phone keys."""
    with driver.session() as session:
        session.run(
            "MATCH (system:SourceSystem {source_key: 'onediver'}) "
            "UNWIND range(0, 8) AS key_index "
            "MERGE (identifier:Identifier {identifier_type: 'phone', "
            "identifier_scope: 'global', "
            "normalized_value: '+658000910' + toString(key_index)}) "
            "SET identifier.identifier_id = 'ident-cap-' + toString(key_index), "
            "identifier.participant_count = 24 "
            "WITH system, identifier, key_index "
            "UNWIND range(1, 24) AS row_index "
            "CREATE (record:SourceRecord {"
            "source_record_pk: 'sr-cap-' + toString(key_index) + '-' + toString(row_index), "
            "source_record_id: 'cap-' + toString(key_index) + '-' + toString(row_index), "
            "source_instance_id: 'legacy-default', source_record_version: 1, "
            "record_hash: 'sha256:cap', lifecycle_status: 'parked', "
            "link_status: 'unlinked', attribution_status: 'parked', "
            "reeval_pending: true, observed_at: datetime('2026-01-01T00:00:00Z')})"
            "-[:FROM_SOURCE]->(system) "
            "CREATE (record)-[:ASSERTS {"
            "assertion_id: 'assert-cap-' + toString(key_index) + '-' + toString(row_index), "
            "field_path: 'identifiers', role: 'subject', quality_flag: 'valid', "
            "is_verified: true, created_at: datetime()}]->(identifier)"
        ).consume()

    _seed_parked_record(driver, "sr-cap-incoming", "2026-01-02T00:00:00Z")
    for key_index in range(9):
        _seed_assertion(
            driver,
            "sr-cap-incoming",
            f"ident-cap-{key_index}",
            f"assert-incoming-{key_index}",
        )


def test_cluster_record_bound_parks_incoming_beyond_max_expansion(
    neo4j_driver: Driver,
) -> None:
    _apply_schema(neo4j_driver)
    _seed_source_system(neo4j_driver)
    _seed_over_bound_cluster(neo4j_driver)

    outcome = _materialize(neo4j_driver, "sr-cap-incoming")

    assert outcome.status == "parked"
    assert outcome.profile_id is None
    assert _scalar(neo4j_driver, "MATCH (profile:Profile) RETURN count(profile) AS count") == 0
    assert (
        _scalar(
            neo4j_driver,
            "MATCH (:SourceRecord {source_record_pk: 'sr-cap-incoming'})"
            "-[:IN_PROFILE]->(:Profile) RETURN count(*) AS count",
        )
        == 0
    )
    incoming = next(
        row
        for row in _record_attribution_snapshot(neo4j_driver)
        if row["source_record_pk"] == "sr-cap-incoming"
    )
    assert incoming["link_status"] == "unlinked"
    assert incoming["attribution_status"] == "parked"
    assert incoming["reeval_pending"] is True


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
            source_record_pk="sr-shadow-staged",
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
            source_record_pk="sr-shadow-staged",
        ),
    )

    assert resolved == (None, False)
