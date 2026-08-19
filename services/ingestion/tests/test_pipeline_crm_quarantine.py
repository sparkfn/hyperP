"""Quarantine behavior for activities with unresolved owners.

Issue #221: instead of raising UnresolvedActivityOwnerError and killing the
entire ingestion run, activities whose owner deal scope is indeterminate or
missing are quarantined with the ``quarantined_owner_unresolved`` disposition.
The cursor advances past them so the run continues.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

from neo4j import ManagedTransaction
from pytest import MonkeyPatch
from src.bitrix_backfill_models import GenerationRunContext
from src.bitrix_ingestion_models import (
    CrmActivityProjection,
    ExecutionContext,
    FenceContext,
)
from src.graph.client import Neo4jClient
from src.graph.queries.bitrix_backfill import RECORD_BITRIX_ACTIVITY_OWNER_RETRY
from src.graph.queries.bitrix_deal_scope import GET_CURRENT_DEAL_SCOPE_BATCH
from src.models import IngestResult, RecordType, SourceRecordEnvelope, SourceRecordParentRef
from src.pipeline_crm import (
    _owner_scope_or_retry,
    _require_activity_owner_scope,
    ingest_call_record,
    ingest_crm_history_record,
)
from src.resumable import CheckpointDescriptor

# -- helpers -----------------------------------------------------------------


class _Result:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def single(self) -> dict[str, object] | None:
        return self._row

    def consume(self) -> None:
        return None


class _Session:
    def __init__(self, tx: object) -> None:
        self._tx = tx

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute_write(self, work: Callable[[ManagedTransaction], object]) -> object:
        return work(cast(ManagedTransaction, self._tx))


class _Client:
    def __init__(self, tx: object | None = None) -> None:
        self.tx = tx if tx is not None else _NoopTx()

    def session(self) -> _Session:
        return _Session(self.tx)


class _NoopTx:
    def run(self, query: str, **kwargs: object) -> _Result:
        return _Result(None)

    def consume(self) -> None:
        return None


def _fence() -> FenceContext:
    return FenceContext(
        logical_run_id="lr-1",
        ingest_run_id="ir-1",
        source_key="bitrix_chat",
        stream_key="crm_activities",
        stream_generation=1,
        fencing_token=1,
        attempt_generation=1,
    )


def _checkpoint() -> CheckpointDescriptor:
    return CheckpointDescriptor(
        phase="test",
        cursor={},
        source_window={},
        last_committed_record_id=None,
        connector_version="v1",
        schema_version=1,
        replay_boundary="boundary-1",
    )


def _execution_context() -> ExecutionContext:
    return ExecutionContext(
        worker_task_id="task-1",
        fence_context=_fence(),
        checkpoint=_checkpoint(),
        generation_context=GenerationRunContext(
            generation_id="gen-1",
            boundary_digest="bd-1",
            configuration_digest="cd-1",
        ),
    )


def _activity_envelope() -> SourceRecordEnvelope:
    projection = CrmActivityProjection(
        history_kind="call",
        event_at=datetime(2026, 8, 7, tzinfo=UTC),
    )
    return SourceRecordEnvelope(
        source_system="bitrix_chat",
        source_record_id="bitrix-crm-history-900",
        record_type=RecordType.CRM_HISTORY,
        observed_at="2026-08-07T00:00:00Z",
        record_hash="sha256:abc",
        raw_payload={"crm_activity_id": "900"},
        parent_ref=SourceRecordParentRef(
            parent_source_system="bitrix_chat",
            parent_source_record_id="bitrix-crm-deal-501",
            parent_record_type=RecordType.CRM_DEAL,
        ),
        history_family=projection.history_family,
        history_kind=projection.history_kind,
        history_source=projection.history_source,
        event_at=projection.event_at_iso,
        projection_version=projection.projection_version,
        projection_source=projection.projection_source,
    )


def _call_envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="bitrix_chat",
        source_record_id="bitrix-call-900",
        record_type=RecordType.CALL,
        observed_at="2026-08-07T00:00:00Z",
        record_hash="sha256:call-abc",
        raw_payload={"crm_activity_id": "900", "owner_id": "501"},
        parent_ref=SourceRecordParentRef(
            parent_source_system="bitrix_chat",
            parent_source_record_id="bitrix-crm-history-900",
            parent_record_type=RecordType.CRM_HISTORY,
        ),
    )


# -- tests -------------------------------------------------------------------


def test_call_scope_uses_raw_owner_deal_not_history_parent_suffix() -> None:
    requested_deal_ids: list[list[str]] = []

    class _ScopeTx:
        def run(self, query: str, **kwargs: object) -> _Result:
            assert query == GET_CURRENT_DEAL_SCOPE_BATCH
            deal_ids = kwargs["deal_ids"]
            assert isinstance(deal_ids, list)
            assert all(isinstance(deal_id, str) for deal_id in deal_ids)
            requested_deal_ids.append(cast(list[str], deal_ids))
            return _Result({"scope_state": "in_scope"})

    result = _require_activity_owner_scope(
        cast(ManagedTransaction, _ScopeTx()),
        _call_envelope(),
    )

    assert result == "in_scope"
    assert requested_deal_ids == [["501"]]


def test_call_owner_retry_records_raw_owner_deal_id() -> None:
    scope_deal_ids: list[list[str]] = []
    retry_owner_ids: list[str] = []

    class _RetryTx:
        def run(self, query: str, **kwargs: object) -> _Result:
            if query == GET_CURRENT_DEAL_SCOPE_BATCH:
                deal_ids = kwargs["deal_ids"]
                assert isinstance(deal_ids, list)
                assert all(isinstance(deal_id, str) for deal_id in deal_ids)
                scope_deal_ids.append(cast(list[str], deal_ids))
                return _Result(None)
            assert query == RECORD_BITRIX_ACTIVITY_OWNER_RETRY
            owner_deal_id = kwargs["owner_deal_id"]
            assert isinstance(owner_deal_id, str)
            retry_owner_ids.append(owner_deal_id)
            return _Result({"status": "retryable"})

    result = _owner_scope_or_retry(
        cast(ManagedTransaction, _RetryTx()),
        _call_envelope(),
        _execution_context(),
    )

    assert result is None
    assert scope_deal_ids == [["501"]]
    assert retry_owner_ids == ["501"]


def test_unresolved_owner_quarantines_crm_history_not_raises(
    monkeypatch: MonkeyPatch,
) -> None:
    """A crm_history record with an unresolved owner is quarantined, not fatal."""
    monkeypatch.setattr("src.pipeline_crm.assert_active_bitrix_fence", lambda *_a, **_kw: None)
    monkeypatch.setattr("src.pipeline_crm._owner_scope_or_retry", lambda *_a, **_kw: None)
    monkeypatch.setattr("src.pipeline_crm.load_locked_source_state", lambda *_a: None)
    monkeypatch.setattr("src.pipeline_crm._record_activity_unit", lambda *_a, **_kw: None)

    client = _Client()
    result = ingest_crm_history_record(
        cast(Neo4jClient, client),
        _activity_envelope(),
        ingest_run_id="run-1",
        execution_context=_execution_context(),
    )

    assert result.dropped is True
    assert result.retry_pending is False
    assert result.source_record_pk is None


def test_unresolved_owner_quarantines_call_not_raises(
    monkeypatch: MonkeyPatch,
) -> None:
    """A call record with an unresolved owner is quarantined, not fatal."""
    monkeypatch.setattr("src.pipeline_crm.assert_active_bitrix_fence", lambda *_a, **_kw: None)
    monkeypatch.setattr("src.pipeline_crm._owner_scope_or_retry", lambda *_a, **_kw: None)
    monkeypatch.setattr("src.pipeline_crm.load_locked_source_state", lambda *_a: None)
    monkeypatch.setattr("src.pipeline_crm._record_activity_unit", lambda *_a, **_kw: None)

    client = _Client()
    result = ingest_call_record(
        cast(Neo4jClient, client),
        _call_envelope(),
        ingest_run_id="run-1",
        execution_context=_execution_context(),
    )

    assert result.dropped is True
    assert result.retry_pending is False
    assert result.source_record_pk is None


def test_quarantine_records_coverage_with_correct_disposition(
    monkeypatch: MonkeyPatch,
) -> None:
    """The quarantine path records coverage with quarantined_owner_unresolved."""
    recorded: list[tuple[str, str]] = []

    def _capture(
        tx: ManagedTransaction,
        context: ExecutionContext | None,
        envelope: SourceRecordEnvelope,
        result: object,
        *,
        disposition: str,
        scope_state: str,
    ) -> None:
        recorded.append((disposition, scope_state))

    monkeypatch.setattr("src.pipeline_crm.assert_active_bitrix_fence", lambda *_a, **_kw: None)
    monkeypatch.setattr("src.pipeline_crm._owner_scope_or_retry", lambda *_a, **_kw: None)
    monkeypatch.setattr("src.pipeline_crm.load_locked_source_state", lambda *_a: None)
    monkeypatch.setattr("src.pipeline_crm._record_activity_unit", _capture)

    client = _Client()
    result = ingest_crm_history_record(
        cast(Neo4jClient, client),
        _activity_envelope(),
        ingest_run_id="run-1",
        execution_context=_execution_context(),
    )

    assert result.dropped is True
    assert recorded == [("quarantined_owner_unresolved", "indeterminate")]


def test_quarantine_records_coverage_for_call_record(
    monkeypatch: MonkeyPatch,
) -> None:
    """The call quarantine path also records the correct disposition."""
    recorded: list[tuple[str, str]] = []

    def _capture(
        tx: ManagedTransaction,
        context: ExecutionContext | None,
        envelope: SourceRecordEnvelope,
        result: object,
        *,
        disposition: str,
        scope_state: str,
    ) -> None:
        recorded.append((disposition, scope_state))

    monkeypatch.setattr("src.pipeline_crm.assert_active_bitrix_fence", lambda *_a, **_kw: None)
    monkeypatch.setattr("src.pipeline_crm._owner_scope_or_retry", lambda *_a, **_kw: None)
    monkeypatch.setattr("src.pipeline_crm.load_locked_source_state", lambda *_a: None)
    monkeypatch.setattr("src.pipeline_crm._record_activity_unit", _capture)

    client = _Client()
    result = ingest_call_record(
        cast(Neo4jClient, client),
        _call_envelope(),
        ingest_run_id="run-1",
        execution_context=_execution_context(),
    )

    assert result.dropped is True
    assert recorded == [("quarantined_owner_unresolved", "indeterminate")]


def test_record_activity_unit_resolves_retry_as_quarantined(
    monkeypatch: MonkeyPatch,
) -> None:
    """_record_activity_unit resolves the retry as quarantined_owner_unresolved."""
    from src.graph.queries.bitrix_backfill import RESOLVE_BITRIX_ACTIVITY_OWNER_RETRY
    from src.pipeline_crm import _record_activity_unit

    resolve_calls: list[dict[str, object]] = []

    class _CaptureTx:
        def run(self, query: str, **kwargs: object) -> _Result:
            if query == RESOLVE_BITRIX_ACTIVITY_OWNER_RETRY:
                resolve_calls.append(kwargs)
                return _Result({"source_identity": "test"})
            return _Result(None)

        def consume(self) -> None:
            return None

    context = _execution_context()
    envelope = _call_envelope()

    monkeypatch.setattr("src.pipeline_crm.record_terminal_unit", lambda *_a, **_kw: None)

    _record_activity_unit(
        cast(ManagedTransaction, _CaptureTx()),
        context,
        envelope,
        IngestResult(source_record_id="bitrix-call-900", dropped=True),
        disposition="quarantined_owner_unresolved",
        scope_state="indeterminate",
    )

    assert len(resolve_calls) == 1
    assert resolve_calls[0]["resolution"] == "quarantined_owner_unresolved"


def test_record_activity_unit_resolves_retry_as_reviewed_excluded_for_out_of_scope(
    monkeypatch: MonkeyPatch,
) -> None:
    """Out-of-scope activities still resolve the retry as reviewed_excluded."""
    from src.graph.queries.bitrix_backfill import RESOLVE_BITRIX_ACTIVITY_OWNER_RETRY
    from src.pipeline_crm import _record_activity_unit

    resolve_calls: list[dict[str, object]] = []

    class _CaptureTx:
        def run(self, query: str, **kwargs: object) -> _Result:
            if query == RESOLVE_BITRIX_ACTIVITY_OWNER_RETRY:
                resolve_calls.append(kwargs)
                return _Result({"source_identity": "test"})
            return _Result(None)

        def consume(self) -> None:
            return None

    context = _execution_context()
    envelope = _call_envelope()

    monkeypatch.setattr("src.pipeline_crm.record_terminal_unit", lambda *_a, **_kw: None)

    _record_activity_unit(
        cast(ManagedTransaction, _CaptureTx()),
        context,
        envelope,
        IngestResult(source_record_id="bitrix-call-900", dropped=True),
        disposition="excluded_out_of_scope",
        scope_state="out_of_scope",
    )

    assert len(resolve_calls) == 1
    assert resolve_calls[0]["resolution"] == "reviewed_excluded"


def test_record_activity_unit_resolves_retry_as_resolved_in_scope_for_normal(
    monkeypatch: MonkeyPatch,
) -> None:
    """Normally ingested activities resolve the retry as resolved_in_scope."""
    from src.graph.queries.bitrix_backfill import RESOLVE_BITRIX_ACTIVITY_OWNER_RETRY
    from src.pipeline_crm import _record_activity_unit

    resolve_calls: list[dict[str, object]] = []

    class _CaptureTx:
        def run(self, query: str, **kwargs: object) -> _Result:
            if query == RESOLVE_BITRIX_ACTIVITY_OWNER_RETRY:
                resolve_calls.append(kwargs)
                return _Result({"source_identity": "test"})
            return _Result(None)

        def consume(self) -> None:
            return None

    context = _execution_context()
    envelope = _call_envelope()

    monkeypatch.setattr("src.pipeline_crm.record_terminal_unit", lambda *_a, **_kw: None)

    _record_activity_unit(
        cast(ManagedTransaction, _CaptureTx()),
        context,
        envelope,
        IngestResult(source_record_id="bitrix-call-900", source_record_pk="pk-1"),
        disposition="created",
        scope_state="in_scope",
    )

    assert len(resolve_calls) == 1
    assert resolve_calls[0]["resolution"] == "resolved_in_scope"


def test_point_in_time_safety_observed_at_preserved() -> None:
    """The activity envelope carries observed_at for point-in-time filtering."""
    envelope = _activity_envelope()
    assert envelope.observed_at == "2026-08-07T00:00:00Z"
    # The raw_payload preserves the activity id for later feature extraction
    assert envelope.raw_payload.get("crm_activity_id") == "900"


def test_quarantined_owner_disposition_is_excluded_in_counter_deltas() -> None:
    """The quarantined_owner_unresolved disposition is counted as excluded, not failed."""
    from src.bitrix_backfill_runtime import _counter_deltas

    deltas = _counter_deltas("quarantined_owner_unresolved")
    assert deltas["excluded_delta"] == 1
    assert deltas["committed_delta"] == 0
    assert deltas["retry_delta"] == 0
    assert deltas["duplicate_delta"] == 0


def test_no_retry_pending_after_quarantine(monkeypatch: MonkeyPatch) -> None:
    """After quarantine, the result does not signal retry_pending (run continues)."""
    monkeypatch.setattr("src.pipeline_crm.assert_active_bitrix_fence", lambda *_a, **_kw: None)
    monkeypatch.setattr("src.pipeline_crm._owner_scope_or_retry", lambda *_a, **_kw: None)
    monkeypatch.setattr("src.pipeline_crm.load_locked_source_state", lambda *_a: None)
    monkeypatch.setattr("src.pipeline_crm._record_activity_unit", lambda *_a, **_kw: None)

    client = _Client()
    result = ingest_crm_history_record(
        cast(Neo4jClient, client),
        _activity_envelope(),
        ingest_run_id="run-1",
        execution_context=_execution_context(),
    )

    # The key invariant: no retry_pending means the caller loop won't
    # raise UnresolvedActivityOwnerError and the batch continues.
    assert result.retry_pending is False
    assert result.dropped is True
    assert result.errors == []
