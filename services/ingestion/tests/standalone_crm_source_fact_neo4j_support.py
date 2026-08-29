# ruff: noqa: E501 -- Cypher fixture literals retain executable graph shape.
"""Cohesive disposable-Neo4j support for #302 source-fact acceptance scenarios."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from neo4j import Driver, ManagedTransaction
from src.connectors.bitrix_openlines.models import CrmContact
from src.graph.queries.standalone_crm_source_facts import CLAIM_PAGE, FINALIZE_PAGE
from src.models import IngestResult
from src.record_lifecycle import PlannedVersion
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_census_requests import (
    SourceSyncAuthority,
    SourceSyncCensusRequest,
    StandaloneCrmBudget,
    canonical_request_payload,
)
from src.standalone_crm_child_contracts import StandaloneCrmSourceChildScope
from src.standalone_crm_source_fact_mapper import map_source_fact_page
from src.standalone_crm_source_fact_models import (
    StandaloneCrmSourceFactMutation,
    StandaloneCrmSourceFactPage,
    build_source_fact_commit,
)
from src.standalone_crm_unit_repository import StandaloneCrmAtomicUnitCommit
from tests._standalone_crm_lane_a_fakes import lead_envelope


def reset_disposable_data(driver: Driver) -> None:
    """Clear every data node from the explicitly disposable Lane A database.

    Neo4j schema constraints and indexes are database metadata, so this removes
    production-pipeline artifacts without weakening or dropping the Lane A schema.
    The #302 hook is the final Lane A case and each scenario also resets in finally.
    """
    with driver.session() as session:
        session.run("MATCH (node) DETACH DELETE node").consume()


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
        "proposed_binding_subject": None,
        "proposed_binding_offset": None,
        "processed_delta": 1,
        "skipped_delta": 0,
        "failed_delta": 0,
        "source_receipts_json": json.dumps(
            [
                {
                    "lead_company_id": None,
                    "observed_at": "2020-01-01T00:00:00Z",
                    "record_hash": "sha256:" + "d" * 64,
                    "row_id": 6,
                    "source_record_pk": "source-record-6",
                    "source_record_version": 1,
                }
            ],
            sort_keys=True,
            separators=(",", ":"),
        ),
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


def assert_raw_claim_finalize_replay(driver: Driver) -> None:
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
              k.processed_rows AS processed, r.status AS receipt_status,
              r.source_receipts_json AS source_receipts_json
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
        "source_receipts_json": p["source_receipts_json"],
    }


def assert_raw_authority_conflict(driver: Driver) -> None:
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


class DriverClient:
    def __init__(self, driver: Driver) -> None:
        self._driver = driver

    def execute_write(self, work: Callable[[ManagedTransaction], object]) -> object:
        with self._driver.session() as session:
            return session.execute_write(work)

    def execute_read(self, work: Callable[[ManagedTransaction], object]) -> object:
        with self._driver.session() as session:
            return session.execute_read(work)


class SentinelAdapter:
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


def repository_request(
    *,
    census_id: str = "census-a",
    source_instance_id: str = "portal-a",
    control_instance_id: str = "control-a",
    call_intent_id: str = "call-a",
) -> StandaloneCrmAtomicUnitCommit[StandaloneCrmSourceFactMutation]:
    base = lead_envelope()
    task_id = f"{census_id}-lead-task"
    unit = replace(base.unit, census_id=census_id, task_id=task_id)
    budget = replace(
        base.budget_authorization,
        census_id=census_id,
        task_id=task_id,
        attempt_deadline="2099-01-01T00:00:00Z",
        occurrence_deadline="2099-01-02T00:00:00Z",
    )
    envelope = replace(
        base,
        scope=StandaloneCrmSourceChildScope("bitrix_chat", source_instance_id, control_instance_id),
        unit=unit,
        budget_authorization=budget,
    )
    checkpoint = StandaloneCrmCheckpoint(census_id, "lead", 10, None, 5, None, None, 0, 0, 1, 2)
    page = StandaloneCrmSourceFactPage(
        envelope,
        call_intent_id,
        5,
        checkpoint,
        (CrmContact("6", "Ada", kind="lead", observed_at=datetime(2020, 1, 1, tzinfo=UTC)),),
    )
    return build_source_fact_commit(map_source_fact_page(page), skipped_rows=0)


def _repository_request_json(
    request: StandaloneCrmAtomicUnitCommit[StandaloneCrmSourceFactMutation],
) -> str:
    envelope = request.envelope
    budget = envelope.budget_authorization
    stored = SourceSyncCensusRequest(
        envelope.scope.source_key,
        envelope.scope.source_instance_id,
        envelope.scope.control_instance_id,
        f"occurrence-{envelope.unit.census_id}",
        ("lead",),
        StandaloneCrmBudget(
            budget.max_calls_per_attempt,
            budget.max_rows_per_attempt,
            3600,
            budget.max_calls_per_occurrence,
            budget.max_rows_per_occurrence,
            2,
            budget.occurrence_deadline,
        ),
        "policy-a",
        "association-a",
        "configuration-a",
        SourceSyncAuthority("mapping", "mapping-digest", "projection", "projection-digest"),
    )
    return canonical_request_payload(stored)


_REPOSITORY_CASE_SEED_QUERY = """
    MERGE (source:SourceSystem {source_key: $source_key})
    SET source.is_active = true
    MERGE (instance:BitrixSourceInstance {
      source_key: $source_key, source_instance_id: $source_instance_id
    })
    SET instance.status = 'active'
    MERGE (instance)-[:INSTANCE_OF]->(source)
    MERGE (:BitrixExecutionSourceBinding {
      source_key: $source_key, source_instance_id: $source_instance_id,
      control_instance_id: $control_instance_id
    })
    CREATE (:StandaloneCrmCensus {
      census_id: $census_id, generation: $generation, source_key: $source_key,
      source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
      census_kind: 'source_sync', request_json: $request_json, status: 'running',
      cancel_requested: false, created_at: datetime($available_at), occurrence_rows: 0
    })
    CREATE (:StandaloneCrmCensusAttempt {
      census_id: $census_id, generation: $generation, fence_token: $fence_token,
      status: 'running', task_id: $parent_task_id, attempt_deadline: datetime($attempt_deadline),
      row_count: 0
    })
    CREATE (:StandaloneCrmCensusUnit {
      census_id: $census_id, generation: $generation, stream_kind: 'lead', state: 'running',
      frozen_upper_id: $frozen_upper_id
    })
    CREATE (:StandaloneCrmCensusFence {
      census_id: $census_id, generation: $generation, stream_kind: 'lead', token: $fence_token,
      owner_id: $fence_owner_id, status: 'active', lease_until: datetime($attempt_deadline)
    })
    CREATE (:StandaloneCrmChildPublication {
      census_id: $census_id, generation: $generation, stream_kind: 'lead', task_name: $task_name,
      task_id: $task_id, payload_digest: $payload_digest, status: 'published'
    })
    CREATE (:StandaloneCrmHttpCallReservation {
      intent_id: $call_intent_id, census_id: $census_id, generation: $generation,
      fence_token: $fence_token, stream_kind: 'lead', call_kind: 'page', cursor: $expected_cursor,
      task_id: $task_id, status: 'succeeded'
    })
    CREATE (:StandaloneCrmCensusCheckpoint {
      census_id: $census_id, stream_kind: 'lead', last_committed_id: $expected_cursor,
      processed_rows: $expected_processed, skipped_rows: $expected_skipped,
      generation: $generation, fence_token: $fence_token, frozen_upper_id: $frozen_upper_id,
      revision_id: null
    })
"""


def _repository_seed_parameters(
    request: StandaloneCrmAtomicUnitCommit[StandaloneCrmSourceFactMutation],
) -> dict[str, object]:
    envelope = request.envelope
    checkpoint = request.expected_checkpoint
    return {
        "source_key": envelope.scope.source_key,
        "source_instance_id": envelope.scope.source_instance_id,
        "control_instance_id": envelope.scope.control_instance_id,
        "census_id": envelope.unit.census_id,
        "generation": envelope.unit.generation,
        "fence_token": envelope.unit.fence_token,
        "fence_owner_id": envelope.unit.fence_owner_id,
        "task_name": envelope.unit.task_name,
        "task_id": envelope.unit.task_id,
        "payload_digest": envelope.unit.payload_digest,
        "available_at": envelope.availability.available_at,
        "attempt_deadline": envelope.budget_authorization.attempt_deadline,
        "frozen_upper_id": envelope.frozen_upper_id,
        "parent_task_id": f"standalone-crm-parent:{envelope.unit.census_id}:{envelope.unit.generation}",
        "call_intent_id": request.mutation.page.call_intent_id,
        "expected_cursor": checkpoint.last_committed_id,
        "expected_processed": checkpoint.processed_rows,
        "expected_skipped": checkpoint.skipped_rows,
        "request_json": _repository_request_json(request),
    }


def _execute_repository_seed(driver: Driver, parameters: dict[str, object]) -> None:
    with driver.session() as session:
        session.run(_REPOSITORY_CASE_SEED_QUERY, **parameters).consume()


def _assert_repository_seed_clock(
    driver: Driver,
    parameters: dict[str, object],
) -> None:
    with driver.session() as session:
        clock_matches = session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "RETURN census.created_at = datetime($available_at) AS clock_matches",
            **parameters,
        ).single(strict=True)["clock_matches"]
    assert clock_matches is True


def seed_repository_case(
    driver: Driver,
    request: StandaloneCrmAtomicUnitCommit[StandaloneCrmSourceFactMutation] | None = None,
) -> StandaloneCrmAtomicUnitCommit[StandaloneCrmSourceFactMutation]:
    seeded = request or repository_request()
    parameters = _repository_seed_parameters(seeded)
    _execute_repository_seed(driver, parameters)
    _assert_repository_seed_clock(driver, parameters)
    return seeded


def source_fact_counts(driver: Driver, census_id: str = "census-a") -> dict[str, int]:
    with driver.session() as session:
        row = session.run(
            """
            MATCH (checkpoint:StandaloneCrmCensusCheckpoint {census_id: $census_id, stream_kind: 'lead'})
            OPTIONAL MATCH (record:SourceRecord)
            OPTIONAL MATCH (receipt:StandaloneCrmSourceFactPageReceipt)
            RETURN count(DISTINCT record) AS records, count(DISTINCT receipt) AS receipts,
              checkpoint.last_committed_id AS cursor, checkpoint.processed_rows AS processed
            """,
            census_id=census_id,
        ).single(strict=True)
    return dict(row)


def retire_current_authority(driver: Driver) -> None:
    """Make current-generation control state unusable after an immutable commit."""
    with driver.session() as session:
        session.run(
            """
            MATCH (census:StandaloneCrmCensus {census_id: 'census-a'})
            MATCH (attempt:StandaloneCrmCensusAttempt {census_id: 'census-a'})
            MATCH (unit:StandaloneCrmCensusUnit {census_id: 'census-a', stream_kind: 'lead'})
            MATCH (fence:StandaloneCrmCensusFence {census_id: 'census-a', stream_kind: 'lead'})
            SET census.status = 'completed', census.generation = 2,
              attempt.status = 'completed', unit.state = 'completed',
              fence.status = 'retired', fence.lease_until = datetime('2000-01-01T00:00:00Z')
            """
        ).consume()
