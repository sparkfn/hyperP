"""Neo4j control query contracts and transaction-retry idempotence proofs."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar, cast

from _bounded_ingestion_fixture import context, unit
from neo4j import ManagedTransaction
from src.bounded_ingestion_models import AttemptContext, BoundedUnit, UnitApplyResult
from src.graph.bounded_ingestion_control import BoundedIngestionControl
from src.graph.bounded_ingestion_schema import CREATE_BOUNDED_INGESTION_SCHEMA
from src.graph.client import Neo4jClient
from src.graph.queries.bounded_ingestion_control import (
    CLAIM_BOUNDED_ATTEMPT,
    CLAIM_BOUNDED_RECEIPT,
    ENSURE_BOUNDED_LOGICAL_RUN,
    FINALIZE_BOUNDED_RUN,
    FINALIZE_BOUNDED_UNIT,
    GET_BOUNDED_STATUS,
    PAUSE_BOUNDED_RUN,
    PERSIST_BOUNDED_RETRY,
    REQUEST_MANUAL_PAUSE,
    RESERVE_BOUNDED_USAGE,
)

T = TypeVar("T")


class _Result:
    def __init__(self, record: dict[str, object] | None) -> None:
        self._record = record

    def single(self) -> dict[str, object] | None:
        return self._record


class _RetryTransaction:
    def __init__(self) -> None:
        self.pending_identities: list[str] = []

    def run(self, query: str, **_parameters: object) -> _Result:
        if query == CLAIM_BOUNDED_RECEIPT:
            return _Result({"created": True, "status": "pending", "dispositions_json": None})
        if query == FINALIZE_BOUNDED_UNIT:
            return _Result({"logical_run_id": "logical-fixture"})
        if query == PERSIST_BOUNDED_RETRY:
            return _Result(None)
        raise AssertionError("unexpected bounded control query")


class _RetryingClient:
    def __init__(self) -> None:
        self.active_versions: list[str] = []
        self.write_attempts = 0

    def execute_write(
        self,
        work: Callable[[ManagedTransaction], T],
        *,
        transaction_timeout_seconds: float | None = None,
    ) -> T:
        assert transaction_timeout_seconds == 30.0
        self.write_attempts += 1
        abandoned = _RetryTransaction()
        work(cast(ManagedTransaction, abandoned))
        self.write_attempts += 1
        committed = _RetryTransaction()
        result = work(cast(ManagedTransaction, committed))
        self.active_versions.extend(committed.pending_identities)
        return result


class _TransactionalWriter:
    def __init__(self) -> None:
        self.calls = 0

    def apply(
        self,
        tx: ManagedTransaction,
        _context: AttemptContext,
        bounded_unit: BoundedUnit,
    ) -> UnitApplyResult:
        assert isinstance(tx, _RetryTransaction)
        self.calls += 1
        for record in bounded_unit.unit.records:
            identity = record.get("id")
            assert isinstance(identity, str)
            tx.pending_identities.append(identity)
        return UnitApplyResult(tuple("committed" for _ in bounded_unit.unit.records))


def test_transaction_callback_retry_rolls_back_first_writer_invocation_and_commits_once() -> None:
    client = _RetryingClient()
    writer = _TransactionalWriter()
    control = BoundedIngestionControl(cast(Neo4jClient, client))
    attempt = context()
    bounded_unit = unit(0, (("identity-1", "v1"),), terminal=True)

    result = control.commit_unit(attempt, bounded_unit, writer)

    assert result == UnitApplyResult(("committed",))
    assert client.write_attempts == 2
    assert writer.calls == 2
    assert client.active_versions == ["identity-1"]


def test_control_schema_and_queries_persist_receipts_retries_and_exact_fences() -> None:
    schema = "\n".join(CREATE_BOUNDED_INGESTION_SCHEMA)
    for fragment in (
        "IngestionResetGeneration",
        "BoundedIngestionScope",
        "bounded_logical_key",
        "BoundedIngestionReceipt",
        "(receipt.logical_run_id, receipt.replay_id) IS UNIQUE",
        "BoundedIngestionRetry",
        "(retry.logical_run_id, retry.replay_id, retry.source_record_id) IS UNIQUE",
    ):
        assert fragment in schema

    for fragment in (
        "is_active: true",
        "generation: $reset_generation",
        "status: 'active'",
        "scope.configuration_fingerprint = $configuration_fingerprint",
        "scope.connector_version = $connector_version",
        "scope.checkpoint_schema_version = $checkpoint_schema_version",
        "logical.occurrence_starts_at",
        "logical.drain_starts_at",
        "logical.cutoff_at",
        "logical.next_eligible_at",
    ):
        assert fragment in ENSURE_BOUNDED_LOGICAL_RUN

    for fragment in (
        "active_generation: $attempt_generation",
        "bounded_fencing_token: $fencing_token",
        "worker_task_id: $worker_task_id",
        "lease_token: $lease_token",
        "checkpoint.generation = generation",
        "checkpoint.status = 'active'",
    ):
        assert fragment in CLAIM_BOUNDED_ATTEMPT

    assert "creation_token" in CLAIM_BOUNDED_RECEIPT
    assert "receipt.status = 'pending'" in FINALIZE_BOUNDED_UNIT
    assert "receipt.status = 'committed'" in FINALIZE_BOUNDED_UNIT
    assert "checkpoint.cursor_json = $cursor_after_json" in FINALIZE_BOUNDED_UNIT
    assert "retry.status = 'pending'" in PERSIST_BOUNDED_RETRY
    assert "coalesce(logical.retry_backlog, 0) = 0" in FINALIZE_BOUNDED_RUN
    assert "reserved_source_requests" in RESERVE_BOUNDED_USAGE


def test_bounded_status_projection_is_redacted_to_allowlisted_progress_fields() -> None:
    forbidden = (
        "cursor_json",
        "source_window_json",
        "replay_boundary",
        "lease_token",
        "raw_payload",
    )
    required = (
        "occurrence_timezone AS timezone",
        "usage_records",
        "retry_backlog",
        "failure_category",
        "LIMIT 1",
    )

    for field in forbidden:
        assert field not in GET_BOUNDED_STATUS
    for field in required:
        assert field in GET_BOUNDED_STATUS


def test_manual_and_cooperative_pause_queries_never_finalize_a_terminal_run() -> None:
    for query in (PAUSE_BOUNDED_RUN, REQUEST_MANUAL_PAUSE):
        assert "paused_with_checkpoint" in query
        assert "completed" not in query
        assert "FINISHED" not in query
