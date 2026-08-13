"""Atomic Bitrix stream-fence regression coverage."""

from collections.abc import Callable
from typing import TypeVar, cast

from neo4j import ManagedTransaction, Record
from src.bitrix_ingestion_models import FenceContext
from src.graph.client import Neo4jClient
from src.graph.ingestion_control import verify_rejected_bitrix_fence_rollback
from src.graph.queries.ingestion_control import (
    FIND_BITRIX_FENCE_ROLLBACK_PROBE,
    LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE,
    PROBE_REJECTED_BITRIX_FENCE_ROLLBACK,
    SET_FENCED_BITRIX_STREAM_STATUS,
)

T = TypeVar("T")


class _Result:
    def __init__(self, record: Record | None) -> None:
        self._record = record

    def single(self) -> Record | None:
        return self._record


class _ProbeTransaction:
    def __init__(self, *, stale: bool, stream_exists: bool = True) -> None:
        self.stale = stale
        self.stream_exists = stream_exists
        self.probe_token: str | None = None

    def run(self, query: str, **parameters: object) -> _Result:
        if query == PROBE_REJECTED_BITRIX_FENCE_ROLLBACK:
            if not self.stream_exists:
                return _Result(None)
            self.probe_token = cast(str, parameters["probe_token"])
            return _Result(
                cast(
                    Record,
                    {
                        "fence_accepted": not self.stale,
                        "rollback_probe_token": self.probe_token,
                    },
                )
            )
        assert query == FIND_BITRIX_FENCE_ROLLBACK_PROBE
        persisted = int(parameters["probe_token"] == self.probe_token)
        return _Result(cast(Record, {"persisted_probe_count": persisted}))


class _RollbackClient:
    def __init__(self, *, stale: bool, stream_exists: bool = True) -> None:
        self.stale = stale
        self.stream_exists = stream_exists
        self.committed_probe_token: str | None = None

    def execute_write(self, work: Callable[[ManagedTransaction], T]) -> T:
        transaction = _ProbeTransaction(stale=self.stale, stream_exists=self.stream_exists)
        try:
            result = work(cast(ManagedTransaction, transaction))
        except Exception:
            self.committed_probe_token = None
            raise
        self.committed_probe_token = transaction.probe_token
        return result

    def execute_read(self, work: Callable[[ManagedTransaction], T]) -> T:
        transaction = _ProbeTransaction(stale=self.stale, stream_exists=self.stream_exists)
        transaction.probe_token = self.committed_probe_token
        return work(cast(ManagedTransaction, transaction))


def _fence() -> FenceContext:
    return FenceContext(
        logical_run_id="logical-1",
        ingest_run_id="ingest-1",
        source_key="bitrix_chat",
        stream_key="crm_deals",
        stream_generation=1,
        fencing_token=1,
        attempt_generation=1,
    )


def test_fence_takes_stream_write_lock_before_identity_recheck() -> None:
    lock = LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE.index("SET stream.fence_lock_version")
    identity_recheck = LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE.index("WHERE stream.logical_run_id")

    assert lock < identity_recheck
    assert "stream.ingest_run_id = $ingest_run_id" in LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE
    assert "stream.attempt_generation = $attempt_generation" in (
        LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE
    )
    assert "stream.stream_generation = $stream_generation" in (LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE)
    assert "stream.fencing_token = $fencing_token" in LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE
    assert "stream.status = 'active'" in LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE


def test_terminal_stream_transition_rechecks_the_complete_fence() -> None:
    for field in (
        "source_key",
        "stream_key",
        "logical_run_id",
        "ingest_run_id",
        "attempt_generation",
        "stream_generation",
        "fencing_token",
    ):
        assert f"{field}: ${field}" in SET_FENCED_BITRIX_STREAM_STATUS
    assert "stream.status = 'active'" in SET_FENCED_BITRIX_STREAM_STATUS
    assert "'completed'" in SET_FENCED_BITRIX_STREAM_STATUS
    assert "'terminated'" in SET_FENCED_BITRIX_STREAM_STATUS


def test_rollback_probe_uses_a_unique_marker_instead_of_racy_counter_reads() -> None:
    assert "rollback_probe_token = $probe_token" in PROBE_REJECTED_BITRIX_FENCE_ROLLBACK
    assert "fence_lock_version" in PROBE_REJECTED_BITRIX_FENCE_ROLLBACK
    assert "AS fence_accepted" in PROBE_REJECTED_BITRIX_FENCE_ROLLBACK
    assert "rollback_probe_token: $probe_token" in FIND_BITRIX_FENCE_ROLLBACK_PROBE
    assert "persisted_probe_count" in FIND_BITRIX_FENCE_ROLLBACK_PROBE


def test_rollback_probe_proves_a_rejected_write_did_not_persist() -> None:
    client = _RollbackClient(stale=True)

    assert verify_rejected_bitrix_fence_rollback(cast(Neo4jClient, client), _fence()) is True
    assert client.committed_probe_token is None


def test_rollback_probe_rejects_an_active_fence() -> None:
    client = _RollbackClient(stale=False)

    assert verify_rejected_bitrix_fence_rollback(cast(Neo4jClient, client), _fence()) is False
    assert client.committed_probe_token is None


def test_rollback_probe_does_not_pass_when_the_target_stream_is_missing() -> None:
    client = _RollbackClient(stale=True, stream_exists=False)

    assert verify_rejected_bitrix_fence_rollback(cast(Neo4jClient, client), _fence()) is False
