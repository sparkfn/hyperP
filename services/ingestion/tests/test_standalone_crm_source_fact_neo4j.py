# ruff: noqa: E501 -- Cypher fixture literals retain executable graph shape.
"""Disposable Neo4j execution coverage for #302 fenced page CAS queries."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import cast
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, ManagedTransaction
from neo4j.exceptions import ServiceUnavailable
from src.connectors.bitrix_openlines.models import CrmContact
from src.graph.client import Neo4jClient
from src.graph.queries.standalone_crm_source_facts import CLAIM_PAGE, FINALIZE_PAGE
from src.graph.standalone_crm_source_fact_repository import StandaloneCrmSourceFactRepository
from src.models import IngestResult
from src.record_lifecycle import PlannedVersion
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_census_requests import (
    SourceSyncAuthority,
    SourceSyncCensusRequest,
    StandaloneCrmBudget,
    canonical_request_payload,
)
from src.standalone_crm_source_fact_mapper import map_source_fact_page
from src.standalone_crm_source_fact_models import (
    StandaloneCrmSourceFactMutation,
    StandaloneCrmSourceFactPage,
    build_source_fact_commit,
)
from src.standalone_crm_unit_repository import StandaloneCrmAtomicUnitCommit
from tests._standalone_crm_lane_a_fakes import lead_envelope


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
        pytest.fail("#302 tests require an explicitly disposable Neo4j host")
    driver = GraphDatabase.driver(uri, auth=("neo4j", password))
    try:
        for _ in range(15):
            try:
                driver.verify_connectivity()
                break
            except ServiceUnavailable:
                time.sleep(1)
        else:
            pytest.fail("disposable Neo4j did not become ready")
        _reset(driver)
        yield driver
    finally:
        _reset(driver)
        driver.close()


def _reset(driver: Driver) -> None:
    labels = [
        "StandaloneCrmCensus",
        "StandaloneCrmCensusAttempt",
        "StandaloneCrmCensusUnit",
        "StandaloneCrmCensusFence",
        "StandaloneCrmChildPublication",
        "StandaloneCrmHttpCallReservation",
        "StandaloneCrmCensusCheckpoint",
        "StandaloneCrmSourceFactPageReceipt",
        "BitrixSourceInstance",
        "BitrixExecutionSourceBinding",
        "SourceSystem",
        "SourceRecord",
    ]
    with driver.session() as session:
        session.run(
            "MATCH (node) WHERE any(label IN labels(node) WHERE label IN $labels) DETACH DELETE node",
            labels=labels,
        ).consume()


def _parameters() -> dict[str, object]:
    return {
        "census_id": "census-a",
        "request_json": "{}",
        "authorization_id": "authorization-a",
        "authorization_digest": "sha256:" + "b" * 64,
        "generation": 1,
        "fence_token": 2,
        "fence_owner_id": "worker-a",
        "source_key": "bitrix_chat",
        "source_instance_id": "portal-a",
        "control_instance_id": "control-a",
        "stream_kind": "lead",
        "frozen_upper_id": 10,
        "task_name": "source.child",
        "task_id": "lead-task",
        "parent_task_id": "standalone-crm-parent:census-a:1",
        "payload_digest": "sha256:" + "a" * 64,
        "available_at": "2026-08-29T00:00:00Z",
        "availability_contract_version": "standalone-crm-source-availability-v1",
        "attempt_deadline": "2099-01-01T00:00:00Z",
        "occurrence_deadline": "2099-01-02T00:00:00Z",
        "call_intent_id": "call-a",
        "receipt_key": "census-a:1:lead:call-a",
        "content_digest": "sha256:" + "c" * 64,
        "checkpoint_absent": False,
        "expected_cursor": 5,
        "expected_processed": 0,
        "expected_skipped": 0,
        "proposed_cursor": 6,
        "proposed_processed": 1,
        "proposed_skipped": 0,
        "processed_delta": 1,
        "skipped_delta": 0,
        "failed_delta": 0,
        "attempt_call_limit": 2,
        "occurrence_call_limit": 4,
        "attempt_row_limit": 10,
        "occurrence_row_limit": 20,
    }


def _seed(driver: Driver, *, available_at: str | None = None) -> None:
    p = _parameters()
    if available_at is not None:
        p["available_at"] = available_at
    with driver.session() as session:
        session.run(
            """
            CREATE (:SourceSystem {source_key: $source_key, is_active: true})<-[:INSTANCE_OF]-
              (:BitrixSourceInstance {source_key: $source_key, source_instance_id: $source_instance_id, status: 'active'})
            CREATE (:BitrixExecutionSourceBinding {source_key: $source_key, source_instance_id: $source_instance_id, control_instance_id: $control_instance_id})
            CREATE (:StandaloneCrmCensus {census_id: $census_id, generation: $generation, source_key: $source_key,
              source_instance_id: $source_instance_id, control_instance_id: $control_instance_id, census_kind: 'source_sync',
              request_json: $request_json, status: 'running', cancel_requested: false, created_at: datetime($available_at), occurrence_rows: 0})
            CREATE (:StandaloneCrmCensusAttempt {census_id: $census_id, generation: $generation, fence_token: $fence_token,
              status: 'running', task_id: $parent_task_id, attempt_deadline: datetime($attempt_deadline), row_count: 0})
            CREATE (:StandaloneCrmCensusUnit {census_id: $census_id, generation: $generation, stream_kind: $stream_kind,
              state: 'running', frozen_upper_id: $frozen_upper_id})
            CREATE (:StandaloneCrmCensusFence {census_id: $census_id, generation: $generation, stream_kind: $stream_kind,
              token: $fence_token, owner_id: $fence_owner_id, status: 'active', lease_until: datetime($attempt_deadline)})
            CREATE (:StandaloneCrmChildPublication {census_id: $census_id, generation: $generation, stream_kind: $stream_kind,
              task_name: $task_name, task_id: $task_id, payload_digest: $payload_digest, status: 'published'})
            CREATE (:StandaloneCrmHttpCallReservation {intent_id: $call_intent_id, census_id: $census_id, generation: $generation,
              fence_token: $fence_token, stream_kind: $stream_kind, call_kind: 'page', cursor: $expected_cursor,
              task_id: $task_id, status: 'succeeded'})
            CREATE (:StandaloneCrmCensusCheckpoint {census_id: $census_id, stream_kind: $stream_kind, last_committed_id: $expected_cursor,
              processed_rows: $expected_processed, skipped_rows: $expected_skipped, generation: $generation,
              fence_token: $fence_token, frozen_upper_id: $frozen_upper_id, revision_id: null})
        """,
            **p,
        ).consume()


def _assert_raw_claim_finalize_replay(driver: Driver) -> None:
    _seed(driver)
    p = _parameters()
    with driver.session() as session:
        assert session.run(CLAIM_PAGE, **p).single(strict=True)["decision"] == "apply"
        assert (
            session.run(FINALIZE_PAGE, **p).single(strict=True)["receipt_key"] == p["receipt_key"]
        )
        replay = session.run(CLAIM_PAGE, **p).single(strict=True)
        state = session.run(
            """
            MATCH (c:StandaloneCrmCensus {census_id: $census_id})
            MATCH (a:StandaloneCrmCensusAttempt {census_id: $census_id})
            MATCH (k:StandaloneCrmCensusCheckpoint {census_id: $census_id, stream_kind: $stream_kind})
            MATCH (r:StandaloneCrmSourceFactPageReceipt {receipt_key: $receipt_key})
            RETURN c.occurrence_rows AS census_rows, a.row_count AS attempt_rows, k.last_committed_id AS cursor,
              k.processed_rows AS processed, r.status AS receipt_status
        """,
            **p,
        ).single(strict=True)
    assert replay["decision"] == "replayed"
    assert dict(state) == {
        "census_rows": 1,
        "attempt_rows": 1,
        "cursor": 6,
        "processed": 1,
        "receipt_status": "committed",
    }


def _assert_raw_authority_conflict(driver: Driver) -> None:
    _seed(driver)
    p = _parameters()
    p["task_id"] = "wrong-task"
    with driver.session() as session:
        assert session.run(CLAIM_PAGE, **p).single() is None
        row = session.run("""
            MATCH (k:StandaloneCrmCensusCheckpoint {census_id: 'census-a', stream_kind: 'lead'})
            OPTIONAL MATCH (r:StandaloneCrmSourceFactPageReceipt)
            RETURN k.last_committed_id AS cursor, count(r) AS receipts
        """).single(strict=True)
    assert dict(row) == {"cursor": 5, "receipts": 0}


# The following cases intentionally use the repository and a transaction-scoped
# sentinel adapter.  They execute no Bitrix call, but prove that the same managed
# Neo4j transaction owns domain writes, receipt, checkpoint and accounting.


class _DriverClient:
    def __init__(self, driver: Driver) -> None:
        self._driver = driver

    def execute_write(self, work: Callable[[ManagedTransaction], object]) -> object:
        with self._driver.session() as session:
            return session.execute_write(work)


class _SentinelAdapter:
    def __init__(self) -> None:
        self.plan_calls = 0
        self.persist_calls = 0

    def plan(self, tx: ManagedTransaction, row: object) -> PlannedVersion:
        del tx, row
        self.plan_calls += 1
        return PlannedVersion(1, None, (), None)

    def persist(self, tx: ManagedTransaction, row: object, plan: PlannedVersion) -> IngestResult:
        del row, plan
        self.persist_calls += 1
        pk = f"sentinel-{self.persist_calls}"
        tx.run("CREATE (:SourceRecord {source_record_pk: $pk})", pk=pk).consume()
        return IngestResult(source_record_id=pk, source_record_pk=pk)


def _repository_request() -> StandaloneCrmAtomicUnitCommit[StandaloneCrmSourceFactMutation]:
    envelope = lead_envelope()
    budget = replace(
        envelope.budget_authorization,
        attempt_deadline="2099-01-01T00:00:00Z",
        occurrence_deadline="2099-01-02T00:00:00Z",
    )
    envelope = replace(envelope, budget_authorization=budget)
    checkpoint = StandaloneCrmCheckpoint("census-a", "lead", 10, None, 5, None, None, 0, 0, 1, 2)
    page = StandaloneCrmSourceFactPage(
        envelope,
        "call-a",
        5,
        checkpoint,
        (CrmContact("6", "Ada", kind="lead"),),
    )
    return build_source_fact_commit(map_source_fact_page(page), skipped_rows=0)


def _repository_request_json() -> str:
    request = SourceSyncCensusRequest(
        "bitrix_chat",
        "portal-a",
        "control-a",
        "occurrence-a",
        ("lead",),
        StandaloneCrmBudget(2, 10, 3600, 4, 20, 2, "2099-01-02T00:00:00Z"),
        "policy-a",
        "association-a",
        "configuration-a",
        SourceSyncAuthority("mapping", "mapping-digest", "projection", "projection-digest"),
    )
    return canonical_request_payload(request)


def _seed_repository_case(
    driver: Driver,
) -> StandaloneCrmAtomicUnitCommit[StandaloneCrmSourceFactMutation]:
    request = _repository_request()
    _seed(driver, available_at=request.envelope.availability.available_at)
    with driver.session() as session:
        session.run(
            "MATCH (c:StandaloneCrmCensus {census_id: 'census-a'}) SET c.request_json = $request_json",
            request_json=_repository_request_json(),
        ).consume()
    with driver.session() as session:
        clock_matches = session.run(
            """
            MATCH (census:StandaloneCrmCensus {census_id: 'census-a'})
            RETURN census.created_at = datetime($available_at) AS clock_matches
            """,
            available_at=request.envelope.availability.available_at,
        ).single(strict=True)["clock_matches"]
    assert clock_matches is True
    return request


def _counts(driver: Driver) -> dict[str, int]:
    with driver.session() as session:
        row = session.run(
            """
            MATCH (checkpoint:StandaloneCrmCensusCheckpoint {census_id: 'census-a', stream_kind: 'lead'})
            OPTIONAL MATCH (record:SourceRecord)
            OPTIONAL MATCH (receipt:StandaloneCrmSourceFactPageReceipt)
            RETURN count(DISTINCT record) AS records, count(DISTINCT receipt) AS receipts,
              checkpoint.last_committed_id AS cursor, checkpoint.processed_rows AS processed
            """
        ).single(strict=True)
    return cast(dict[str, int], dict(row))


def _assert_repository_success_replay_conflict(driver: Driver) -> None:
    request = _seed_repository_case(driver)
    adapter = _SentinelAdapter()
    repository = StandaloneCrmSourceFactRepository(
        cast(Neo4jClient, _DriverClient(driver)), adapter=adapter
    )
    assert repository.commit_unit(request).decision == "committed"
    assert _counts(driver) == {"records": 1, "receipts": 1, "cursor": 6, "processed": 1}
    assert repository.commit_unit(request).decision == "replayed"
    assert adapter.plan_calls == 1 and adapter.persist_calls == 1

    changed_budget = replace(
        request.envelope.budget_authorization,
        authorization_digest="sha256:" + "d" * 64,
    )
    conflict_page = replace(
        request.mutation.page,
        envelope=replace(request.envelope, budget_authorization=changed_budget),
    )
    conflict = build_source_fact_commit(
        replace(request.mutation, page=conflict_page), skipped_rows=0
    )
    assert repository.commit_unit(conflict).decision == "conflict"
    assert _counts(driver) == {"records": 1, "receipts": 1, "cursor": 6, "processed": 1}


def _assert_repository_failpoint_rollback(driver: Driver) -> None:
    request = _seed_repository_case(driver)
    adapter = _SentinelAdapter()

    def fail(name: str) -> None:
        if name == "after_domain_writes":
            raise RuntimeError("rollback")

    repository = StandaloneCrmSourceFactRepository(
        cast(Neo4jClient, _DriverClient(driver)), adapter=adapter, failpoint=fail
    )
    with pytest.raises(RuntimeError, match="rollback"):
        repository.commit_unit(request)
    assert _counts(driver) == {"records": 0, "receipts": 0, "cursor": 5, "processed": 0}


def _run_isolated(driver: Driver, scenario: Callable[[Driver], None]) -> None:
    """Run one #302 graph scenario without leaking its disposable graph state."""
    _reset(driver)
    try:
        scenario(driver)
    finally:
        _reset(driver)


def test_claim_finalize_replay_and_conflict_execute_atomically(neo4j_driver: Driver) -> None:
    _run_isolated(neo4j_driver, _assert_raw_claim_finalize_replay)


def test_authority_conflict_creates_no_receipt_or_checkpoint_change(neo4j_driver: Driver) -> None:
    _run_isolated(neo4j_driver, _assert_raw_authority_conflict)


def test_repository_success_replay_conflict_and_accounting_are_atomic(neo4j_driver: Driver) -> None:
    _run_isolated(neo4j_driver, _assert_repository_success_replay_conflict)


def test_repository_failpoint_rolls_back_sentinel_receipt_checkpoint_and_accounting(
    neo4j_driver: Driver,
) -> None:
    _run_isolated(neo4j_driver, _assert_repository_failpoint_rollback)


def run_source_fact_neo4j_cases(driver: Driver) -> None:
    """Execute every #302 real-Neo4j acceptance scenario from the Lane A hook."""
    for scenario in (
        _assert_raw_claim_finalize_replay,
        _assert_raw_authority_conflict,
        _assert_repository_success_replay_conflict,
        _assert_repository_failpoint_rollback,
    ):
        _run_isolated(driver, scenario)
