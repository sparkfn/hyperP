"""Disposable Neo4j coverage for atomic A-S2 membership persistence."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar, cast
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, ManagedTransaction, Session
from neo4j.exceptions import ServiceUnavailable
from src.connectors.bitrix_openlines.models import CrmCompanyBindingPayload
from src.crm_company_contracts import (
    CrmCompanyDescriptionHead,
    CrmCompanyDescriptionHeadCompareAndSet,
    CrmCompanyDescriptionObservation,
    CrmCompanyMembershipHead,
    CrmCompanyMembershipHeadCompareAndSet,
    CrmCompanyMembershipObservation,
    CrmCompanyMembershipSnapshotRecord,
    CrmCompanyReference,
)
from src.crm_company_membership_writer import (
    CrmCompanyDescriptionMutation,
    CrmCompanyMembershipMutation,
    build_company_description_commit,
    build_company_membership_commit,
    membership_company_reference,
)
from src.crm_identity_associations import normalize_company_membership_snapshot
from src.graph.client import Neo4jClient
from src.graph.crm_company_membership import CrmCompanyMembershipRepository
from src.graph.queries.standalone_crm_lane_a_contracts import (
    CREATE_STANDALONE_CRM_LANE_A_CONSTRAINTS,
)
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_census_requests import (
    SourceSyncAuthority,
    SourceSyncCensusRequest,
    StandaloneCrmBudget,
    canonical_request_payload,
)
from src.standalone_crm_child_contracts import (
    CompanySourceChildEnvelope,
    ContactBindingSubposition,
    ContactSourceChildEnvelope,
    LeadSourceChildEnvelope,
    StandaloneCrmSourceAvailability,
    StandaloneCrmSourceChildBudgetAuthorization,
    StandaloneCrmSourceChildScope,
    StandaloneCrmSourceChildUnitAuthority,
)
from src.standalone_crm_unit_repository import StandaloneCrmUnitAccountingDelta

T = TypeVar("T")

_AVAILABLE_AT = "2026-08-29T00:00:00Z"
_ATTEMPT_DEADLINE = "2099-01-01T00:00:00Z"
_OCCURRENCE_DEADLINE = "2099-01-02T00:00:00Z"
_DIGEST = "sha256:" + "a" * 64
_DOMAIN_LABELS = (
    "CrmCompanyReference",
    "CrmCompanyDescriptionObservation",
    "CrmCompanyDescriptionHead",
    "CrmCompanyMembershipSnapshot",
    "CrmCompanyMembershipObservation",
    "CrmCompanyMembershipHead",
)
_AUTHORITY_LABELS = (
    "StandaloneCrmCensus",
    "StandaloneCrmCensusAttempt",
    "StandaloneCrmCensusUnit",
    "StandaloneCrmCensusFence",
    "StandaloneCrmChildPublication",
    "StandaloneCrmCensusCheckpoint",
    "BitrixExecutionSourceBinding",
    "BitrixSourceInstance",
    "SourceSystem",
)


class _Client:
    def __init__(self, driver: Driver) -> None:
        self._driver = driver

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self._driver.session() as session:
            yield session

    def execute_write(self, work: Callable[[ManagedTransaction], T]) -> T:
        with self._driver.session() as session:
            return session.execute_write(work)


@pytest.fixture
def neo4j_driver() -> Iterator[Driver]:
    uri = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_URI")
    password = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_PASSWORD")
    if uri is None or password is None:
        pytest.skip("disposable standalone CRM Lane A Neo4j database is not configured")
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    if os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_SERVICE_HOST") == "neo4j":
        allowed_hosts.add("neo4j")
    if urlparse(uri).hostname not in allowed_hosts:
        pytest.fail("A-S2 tests require an explicitly disposable Neo4j host")
    driver = GraphDatabase.driver(
        uri,
        auth=(os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_USER", "neo4j"), password),
    )
    try:
        _wait_for_driver(driver)
        _reset(driver)
        for statement in CREATE_STANDALONE_CRM_LANE_A_CONSTRAINTS[:8]:
            with driver.session() as session:
                session.run(statement).consume()
        yield driver
    finally:
        _reset(driver)
        driver.close()


def _wait_for_driver(driver: Driver) -> None:
    for _ in range(15):
        try:
            driver.verify_connectivity()
            return
        except ServiceUnavailable:
            time.sleep(1)
    pytest.fail("disposable Neo4j did not become ready")


def _reset(driver: Driver) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (node) WHERE any(label IN labels(node) WHERE label IN $labels) "
            "DETACH DELETE node",
            labels=_DOMAIN_LABELS + _AUTHORITY_LABELS,
        ).consume()
        for statement in CREATE_STANDALONE_CRM_LANE_A_CONSTRAINTS[:8]:
            session.run(f"DROP CONSTRAINT {statement.split()[2]} IF EXISTS").consume()


def _request(source_instance_id: str, control_instance_id: str) -> SourceSyncCensusRequest:
    return SourceSyncCensusRequest(
        "bitrix_chat",
        source_instance_id,
        control_instance_id,
        f"occurrence-{source_instance_id}",
        ("contact", "lead", "company"),
        StandaloneCrmBudget(2, 10, 3600, 4, 20, 2, _OCCURRENCE_DEADLINE),
        "policy-a",
        "association-a",
        "configuration-a",
        SourceSyncAuthority("mapping", "mapping-digest", "projection", "projection-digest"),
    )


def _seed_authority(
    driver: Driver,
    *,
    census_id: str = "census-a",
    source_instance_id: str = "portal-a",
    control_instance_id: str = "control-a",
) -> None:
    request_json = canonical_request_payload(_request(source_instance_id, control_instance_id))
    with driver.session() as session:
        session.run(
            """
            MERGE (source:SourceSystem {source_key: 'bitrix_chat'})
            SET source.is_active = true
            MERGE (instance:BitrixSourceInstance {
              source_key: 'bitrix_chat', source_instance_id: $source_instance_id
            })
            SET instance.status = 'active'
            MERGE (instance)-[:INSTANCE_OF]->(source)
            MERGE (:BitrixExecutionSourceBinding {
              source_key: 'bitrix_chat', source_instance_id: $source_instance_id,
              control_instance_id: $control_instance_id
            })
            MERGE (census:StandaloneCrmCensus {census_id: $census_id, generation: 1})
            SET census.source_key = 'bitrix_chat',
                census.source_instance_id = $source_instance_id,
                census.control_instance_id = $control_instance_id,
                census.census_kind = 'source_sync', census.request_json = $request_json,
                census.status = 'running', census.cancel_requested = false,
                census.created_at = datetime($available_at), census.occurrence_rows = 0
            MERGE (attempt:StandaloneCrmCensusAttempt {
              census_id: $census_id, generation: 1, fence_token: 2
            })
            SET attempt.status = 'running', attempt.attempt_deadline = datetime($attempt_deadline),
                attempt.row_count = 0
            MERGE (checkpoint:StandaloneCrmCensusCheckpoint {
              census_id: $census_id, stream_kind: 'contact'
            })
            SET checkpoint.last_committed_id = 0, checkpoint.processed_rows = 0,
                checkpoint.skipped_rows = 0, checkpoint.binding_subject_id = 5,
                checkpoint.binding_offset = 0, checkpoint.generation = 1,
                checkpoint.fence_token = 2, checkpoint.frozen_upper_id = 10,
                checkpoint.revision_id = null
            """,
            census_id=census_id,
            source_instance_id=source_instance_id,
            control_instance_id=control_instance_id,
            request_json=request_json,
            available_at=_AVAILABLE_AT,
            attempt_deadline=_ATTEMPT_DEADLINE,
        ).consume()
        for stream in ("contact", "lead", "company"):
            session.run(
                """
                MERGE (unit:StandaloneCrmCensusUnit {
                  census_id: $census_id, generation: 1, stream_kind: $stream
                })
                SET unit.state = 'running', unit.frozen_upper_id = 10
                MERGE (fence:StandaloneCrmCensusFence {
                  census_id: $census_id, generation: 1, stream_kind: $stream, token: 2
                })
                SET fence.owner_id = 'worker-a', fence.status = 'active',
                    fence.lease_until = datetime($lease_until)
                MERGE (publication:StandaloneCrmChildPublication {
                  census_id: $census_id, generation: 1, stream_kind: $stream,
                  task_name: 'source.child', task_id: $task_id
                })
                SET publication.payload_digest = $payload_digest,
                    publication.status = 'published'
                """,
                census_id=census_id,
                stream=stream,
                task_id=f"{stream}-task",
                lease_until=_ATTEMPT_DEADLINE,
                payload_digest=_DIGEST,
            ).consume()


def _scope(source_instance_id: str = "portal-a") -> StandaloneCrmSourceChildScope:
    suffix = source_instance_id.removeprefix("portal-")
    return StandaloneCrmSourceChildScope("bitrix_chat", source_instance_id, f"control-{suffix}")


def _envelope(
    stream: str,
    *,
    census_id: str = "census-a",
    source_instance_id: str = "portal-a",
    contact_binding_offset: int = 0,
) -> ContactSourceChildEnvelope | LeadSourceChildEnvelope | CompanySourceChildEnvelope:
    unit = StandaloneCrmSourceChildUnitAuthority(
        census_id, stream, 1, 2, "worker-a", "source.child", f"{stream}-task", _DIGEST
    )
    budget = StandaloneCrmSourceChildBudgetAuthorization(
        "authorization-a",
        _DIGEST,
        census_id,
        stream,
        1,
        2,
        "worker-a",
        "source.child",
        f"{stream}-task",
        _DIGEST,
        2,
        10,
        4,
        20,
        _ATTEMPT_DEADLINE,
        _OCCURRENCE_DEADLINE,
    )
    common = (
        _scope(source_instance_id),
        unit,
        10,
        0,
        StandaloneCrmSourceAvailability(_AVAILABLE_AT),
        budget,
    )
    if stream == "contact":
        return ContactSourceChildEnvelope(
            *common,
            ContactBindingSubposition(5, contact_binding_offset),
        )
    if stream == "lead":
        return LeadSourceChildEnvelope(*common)
    return CompanySourceChildEnvelope(*common)


def _membership_commit(
    *,
    payloads: tuple[CrmCompanyBindingPayload, ...] = (),
    source_instance_id: str = "portal-a",
    census_id: str = "census-a",
    expected_head: CrmCompanyMembershipHead | None = None,
    version: int = 1,
    expected_processed: int = 0,
    expected_offset: int = 0,
) -> tuple[object, CrmCompanyMembershipHead]:
    scope = _scope(source_instance_id)
    snapshot = normalize_company_membership_snapshot(
        subject_type="contact", subject_id="5", payloads=payloads
    )
    record = CrmCompanyMembershipSnapshotRecord(
        scope,
        snapshot,
        "bitrix-crm-contact-5",
        f"contact-record-{version}",
        version,
        f"contact-hash-{version}",
        "2026-08-28T00:00:00Z",
        StandaloneCrmSourceAvailability(_AVAILABLE_AT),
        len(snapshot.bindings),
    )
    observations = tuple(
        CrmCompanyMembershipObservation(
            record,
            membership_company_reference(record, binding.company_id),
            binding.sort,
            binding.role_id,
            binding.is_primary,
        )
        for binding in snapshot.bindings
    )
    head = CrmCompanyMembershipHead(scope, "contact", "5", record)
    mutation = CrmCompanyMembershipMutation(
        record,
        observations,
        CrmCompanyMembershipHeadCompareAndSet(expected_head, head),
    )
    expected = StandaloneCrmCheckpoint(
        census_id, "contact", 10, None, 0, 5, expected_offset, expected_processed, 0, 1, 2
    )
    proposed = StandaloneCrmCheckpoint(
        census_id,
        "contact",
        10,
        None,
        0,
        5,
        expected_offset + 1,
        expected_processed + 1,
        0,
        1,
        2,
    )
    commit = build_company_membership_commit(
        cast(
            ContactSourceChildEnvelope,
            _envelope(
                "contact",
                census_id=census_id,
                source_instance_id=source_instance_id,
                contact_binding_offset=expected_offset,
            ),
        ),
        mutation,
        expected,
        proposed,
        StandaloneCrmUnitAccountingDelta(1, 0, 0),
    )
    return commit, head


def _repository(driver: Driver) -> CrmCompanyMembershipRepository:
    return CrmCompanyMembershipRepository(cast(Neo4jClient, _Client(driver)))


def _lead_empty_commit() -> object:
    scope = _scope()
    snapshot = normalize_company_membership_snapshot(
        subject_type="lead", subject_id="5", payloads=()
    )
    record = CrmCompanyMembershipSnapshotRecord(
        scope,
        snapshot,
        "bitrix-crm-lead-5",
        "lead-record-5",
        1,
        "lead-hash-5",
        "2026-08-28T00:00:00Z",
        StandaloneCrmSourceAvailability(_AVAILABLE_AT),
        0,
    )
    head = CrmCompanyMembershipHead(scope, "lead", "5", record)
    mutation = CrmCompanyMembershipMutation(
        record,
        (),
        CrmCompanyMembershipHeadCompareAndSet(None, head),
    )
    expected = StandaloneCrmCheckpoint("census-a", "lead", 10, None, 0, None, None, 0, 0, 1, 2)
    proposed = StandaloneCrmCheckpoint("census-a", "lead", 10, None, 5, None, None, 1, 0, 1, 2)
    return build_company_membership_commit(
        cast(LeadSourceChildEnvelope, _envelope("lead")),
        mutation,
        expected,
        proposed,
        StandaloneCrmUnitAccountingDelta(1, 0, 0),
    )


def test_empty_snapshot_is_durable_and_exact_replay_does_not_double_account(
    neo4j_driver: Driver,
) -> None:
    _seed_authority(neo4j_driver)
    repository = _repository(neo4j_driver)
    commit, _head = _membership_commit()

    assert repository.commit_unit(commit).decision == "committed"
    assert repository.commit_unit(commit).decision == "idempotent"

    with neo4j_driver.session() as session:
        row = session.run(
            """
            MATCH (census:StandaloneCrmCensus {census_id: 'census-a'})
            MATCH (checkpoint:StandaloneCrmCensusCheckpoint {
              census_id: 'census-a', stream_kind: 'contact'
            })
            MATCH (head:CrmCompanyMembershipHead)-[:SELECTS_MEMBERSHIP_SNAPSHOT]->(snapshot)
            OPTIONAL MATCH (snapshot)-[:HAS_MEMBERSHIP_OBSERVATION]->(observation)
            RETURN census.occurrence_rows AS occurrence_rows,
              checkpoint.processed_rows AS processed_rows,
              checkpoint.binding_offset AS binding_offset,
              snapshot.binding_count AS binding_count,
              count(observation) AS observations
            """
        ).single(strict=True)
    assert dict(row) == {
        "occurrence_rows": 1,
        "processed_rows": 1,
        "binding_offset": 1,
        "binding_count": 0,
        "observations": 0,
    }


def test_empty_lead_snapshot_is_authoritative_and_durable(neo4j_driver: Driver) -> None:
    _seed_authority(neo4j_driver)
    repository = _repository(neo4j_driver)

    assert repository.commit_unit(_lead_empty_commit()).decision == "committed"

    with neo4j_driver.session() as session:
        row = session.run(
            """
            MATCH (checkpoint:StandaloneCrmCensusCheckpoint {
              census_id: 'census-a', stream_kind: 'lead', last_committed_id: 5
            })
            MATCH (:CrmCompanyMembershipHead {
              subject_kind: 'lead', subject_id: '5'
            })-[:SELECTS_MEMBERSHIP_SNAPSHOT]->(snapshot)
            OPTIONAL MATCH (snapshot)-[:HAS_MEMBERSHIP_OBSERVATION]->(observation)
            RETURN checkpoint.processed_rows AS processed_rows,
              snapshot.binding_count AS binding_count,
              count(observation) AS observations
            """
        ).single(strict=True)
    assert dict(row) == {"processed_rows": 1, "binding_count": 0, "observations": 0}


def test_multi_company_membership_precedes_and_reuses_description_reference(
    neo4j_driver: Driver,
) -> None:
    _seed_authority(neo4j_driver)
    repository = _repository(neo4j_driver)
    payloads = (
        CrmCompanyBindingPayload("3", 0, "7", "Y"),
        CrmCompanyBindingPayload("4", 1, "8", "N"),
    )
    commit, _head = _membership_commit(payloads=payloads)
    assert repository.commit_unit(commit).decision == "committed"

    scope = _scope()
    reference = CrmCompanyReference(scope, "3", "bitrix-crm-company-3")
    observation = CrmCompanyDescriptionObservation(
        reference,
        "company-record-3",
        1,
        "company-hash-3",
        "Northwind",
        "2026-08-28T00:00:00Z",
        StandaloneCrmSourceAvailability(_AVAILABLE_AT),
    )
    description_head = CrmCompanyDescriptionHead(reference, observation)
    mutation = CrmCompanyDescriptionMutation(
        observation,
        CrmCompanyDescriptionHeadCompareAndSet(None, description_head),
    )
    expected = StandaloneCrmCheckpoint("census-a", "company", 10, None, 0, None, None, 0, 0, 1, 2)
    proposed = StandaloneCrmCheckpoint("census-a", "company", 10, None, 3, None, None, 1, 0, 1, 2)
    description_commit = build_company_description_commit(
        cast(CompanySourceChildEnvelope, _envelope("company")),
        mutation,
        expected,
        proposed,
        StandaloneCrmUnitAccountingDelta(1, 0, 0),
    )
    assert repository.commit_unit(description_commit).decision == "committed"

    with neo4j_driver.session() as session:
        row = session.run(
            """
            MATCH (reference:CrmCompanyReference)
            OPTIONAL MATCH (reference)-[:HAS_DESCRIPTION_OBSERVATION]->(description)
            WITH count(DISTINCT reference) AS references,
              count(DISTINCT description) AS descriptions
            MATCH (snapshot:CrmCompanyMembershipSnapshot)
              -[:HAS_MEMBERSHIP_OBSERVATION]->(membership)
            RETURN references, descriptions, count(membership) AS memberships
            """
        ).single(strict=True)
    assert dict(row) == {"references": 2, "descriptions": 1, "memberships": 2}


@pytest.mark.parametrize(
    "payloads",
    (
        (),
        (CrmCompanyBindingPayload("3", 0, "7", "Y"),),
    ),
)
def test_later_source_observation_preserves_unchanged_membership_history(
    neo4j_driver: Driver,
    payloads: tuple[CrmCompanyBindingPayload, ...],
) -> None:
    _seed_authority(neo4j_driver)
    repository = _repository(neo4j_driver)
    first, first_head = _membership_commit(payloads=payloads)
    second, second_head = _membership_commit(
        payloads=payloads,
        expected_head=first_head,
        version=2,
        expected_processed=1,
        expected_offset=1,
    )

    assert repository.commit_unit(first).decision == "committed"
    assert repository.commit_unit(second).decision == "committed"

    with neo4j_driver.session() as session:
        row = session.run(
            """
            MATCH (checkpoint:StandaloneCrmCensusCheckpoint {
              census_id: 'census-a', stream_kind: 'contact'
            })
            MATCH (head:CrmCompanyMembershipHead {
              source_instance_id: 'portal-a', subject_kind: 'contact', subject_id: '5'
            })
            MATCH (snapshot:CrmCompanyMembershipSnapshot {
              source_instance_id: 'portal-a', subject_kind: 'contact', subject_id: '5'
            })
            OPTIONAL MATCH (snapshot)-[:HAS_MEMBERSHIP_OBSERVATION]->(observation)
            RETURN checkpoint.processed_rows AS processed_rows,
              checkpoint.binding_offset AS binding_offset,
              head.selected_snapshot_id AS selected_snapshot_id,
              count(DISTINCT snapshot) AS snapshots,
              count(observation) AS observations
            """
        ).single(strict=True)
    assert row["processed_rows"] == 2
    assert row["binding_offset"] == 2
    assert row["selected_snapshot_id"] == second_head.snapshot_record.snapshot_id
    assert row["snapshots"] == 2
    assert row["observations"] == len(payloads) * 2


def test_stale_cas_and_immutable_conflict_leave_claim_state_unchanged(
    neo4j_driver: Driver,
) -> None:
    _seed_authority(neo4j_driver)
    repository = _repository(neo4j_driver)
    first, first_head = _membership_commit()
    assert repository.commit_unit(first).decision == "committed"

    stale, _stale_head = _membership_commit(
        payloads=(CrmCompanyBindingPayload("3", 0, None, "Y"),),
        version=2,
        expected_processed=1,
        expected_offset=1,
    )
    assert repository.commit_unit(stale).decision == "stale_or_conflict"

    conflicting, conflict_head = _membership_commit(
        payloads=(CrmCompanyBindingPayload("3", 0, None, "Y"),),
        expected_head=first_head,
        version=2,
        expected_processed=1,
        expected_offset=1,
    )
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (:CrmCompanyMembershipSnapshot {snapshot_id: $snapshot_id, "
            "snapshot_digest: 'corrupt'})",
            snapshot_id=conflict_head.snapshot_record.snapshot_id,
        ).consume()
    with pytest.raises(RuntimeError, match="immutable membership snapshot"):
        repository.commit_unit(conflicting)

    with neo4j_driver.session() as session:
        row = session.run(
            """
            MATCH (census:StandaloneCrmCensus {census_id: 'census-a'})
            MATCH (checkpoint:StandaloneCrmCensusCheckpoint {
              census_id: 'census-a', stream_kind: 'contact'
            })
            MATCH (head:CrmCompanyMembershipHead)
            RETURN census.occurrence_rows AS occurrence_rows,
              checkpoint.processed_rows AS processed_rows,
              checkpoint.binding_offset AS binding_offset,
              head.selected_snapshot_id AS selected_snapshot_id
            """
        ).single(strict=True)
    assert row["occurrence_rows"] == 1
    assert row["processed_rows"] == 1
    assert row["binding_offset"] == 1
    assert row["selected_snapshot_id"] == first_head.snapshot_record.snapshot_id


def test_same_numeric_ids_are_isolated_by_source_and_no_forbidden_topology_is_created(
    neo4j_driver: Driver,
) -> None:
    _seed_authority(neo4j_driver)
    _seed_authority(
        neo4j_driver,
        census_id="census-b",
        source_instance_id="portal-b",
        control_instance_id="control-b",
    )
    repository = _repository(neo4j_driver)
    payloads = (CrmCompanyBindingPayload("3", 0, None, "Y"),)
    first, _head_a = _membership_commit(payloads=payloads)
    second, _head_b = _membership_commit(
        payloads=payloads,
        source_instance_id="portal-b",
        census_id="census-b",
    )
    assert repository.commit_unit(first).decision == "committed"
    assert repository.commit_unit(second).decision == "committed"

    with neo4j_driver.session() as session:
        rows = session.run(
            "MATCH (reference:CrmCompanyReference {company_id: '3'}) "
            "RETURN collect(reference.source_instance_id) AS instances"
        ).single(strict=True)
        forbidden = session.run(
            """
            MATCH (node)
            WHERE any(label IN labels(node) WHERE label IN [
              'Person', 'Identifier', 'Entity', 'ReviewCase', 'Address',
              'MatchDecision', 'MergeEvent'
            ])
            RETURN count(node) AS count
            """
        ).single(strict=True)
    assert set(rows["instances"]) == {"portal-a", "portal-b"}
    assert forbidden["count"] == 0
