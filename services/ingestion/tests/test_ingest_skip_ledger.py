"""Unit tests for SkipLedger, SkipRecorder, and related skip tracking."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest
from src.bitrix_ingestion_models import ExecutionContext
from src.connectors.base import SourceConnector
from src.connectors.bitrix.connector import (
    BitrixChatConnector,
)
from src.connectors.bitrix.connector import (
    _ChatBundle as BitrixChatBundle,
)
from src.connectors.chat_helpers import ExtractionResult
from src.connectors.whatsapp.connector import (
    _ChatBundle as WhatsAppChatBundle,
)
from src.connectors.whatsapp.connector import (
    _build_envelopes as build_whatsapp_envelopes,
)
from src.exclusions import ExclusionContext
from src.graph.client import Neo4jClient
from src.graph.queries.ingest_skips import (
    RECONCILE_INGEST_RUN_ACCOUNTING,
    WRITE_INGEST_ATTEMPT,
    WRITE_INGEST_SKIPS,
)
from src.ingest_skips import (
    UPSTREAM_QUERY_FILTERS_RECORDED_AS_SKIPS,
    ConnectorSkip,
    SkipLedger,
    SkipRecorder,
    skip_row,
)
from src.main import _ingest_all_records
from src.models import (
    IngestResult,
    JsonValue,
)
from src.pipeline import IngestPipeline
from src.watermark_runner import _process_page_records


class _FakeTx:
    def __init__(self, skip_count: int = 0) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.reconciliation: dict[str, object] = {
            "emitted_count": 0,
            "persisted_count": 0,
            "duplicate_count": 0,
            "pipeline_skip_count": 0,
            "connector_skip_count": 0,
            "record_error_count": 0,
            "stored_skips": skip_count,
            "expected_skips": 0,
            "reconciled": skip_count == 0,
        }

    def run(self, query: str, **kwargs: object) -> _FakeTx:
        self.calls.append((query, kwargs))
        return self

    def single(self) -> dict[str, object] | None:
        return self.reconciliation


class _FakeSession:
    def __init__(self, tx: _FakeTx) -> None:
        self.tx = tx
        self.write_calls = 0

    def execute_write(self, fn: object) -> object:
        from collections.abc import Callable

        assert isinstance(fn, Callable)
        self.write_calls += 1
        return fn(self.tx)

    def execute_read(self, fn: object) -> object:
        from collections.abc import Callable

        assert isinstance(fn, Callable)
        return fn(self.tx)

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *args: object) -> None:
        pass


class _FakeClient:
    def __init__(self, tx: _FakeTx | None = None) -> None:
        self.tx = tx or _FakeTx()
        self._session = _FakeSession(self.tx)

    def session(self) -> _FakeSession:
        return self._session


class _DummyConnector(SourceConnector):
    def __init__(self, records: list[dict[str, JsonValue]]) -> None:
        self.records = records

    def get_source_key(self) -> str:
        return "fundbox"

    def fetch_records(self) -> list[dict[str, JsonValue]]:
        return list(self.records)


def test_upstream_query_filters_are_not_recorded_as_skips() -> None:
    assert UPSTREAM_QUERY_FILTERS_RECORDED_AS_SKIPS is False


def test_skip_recorder_mixin() -> None:
    recorder = SkipRecorder()
    assert recorder.drain_skips() == []

    recorder.record_skip("employee_customer", source_record_id="rec-1", locator="loc-1")
    recorder.record_skip(
        ConnectorSkip(reason="empty_content", source_record_id=None, locator="loc-2")
    )

    skips = recorder.drain_skips()
    assert len(skips) == 2
    assert skips[0].reason == "employee_customer"
    assert skips[0].source_record_id == "rec-1"
    assert skips[1].reason == "empty_content"
    assert recorder.drain_skips() == []


def test_ledger_skip_ids_are_deterministic_within_an_attempt() -> None:
    tx = _FakeTx()
    client = _FakeClient(tx)
    ledger1 = SkipLedger(client, "run-123", "fundbox", attempt_key="attempt-1")
    ledger1.add_pipeline_skip("excluded_identity", source_record_id="rec-1")
    ledger1.flush()

    assert len(tx.calls) == 2
    assert tx.calls[0][0] == WRITE_INGEST_SKIPS
    assert tx.calls[1][0] == WRITE_INGEST_ATTEMPT
    assert client._session.write_calls == 1
    _query, kwargs = tx.calls[0]
    rows1 = kwargs["rows"]
    skip_id1 = rows1[0]["skip_id"]

    tx2 = _FakeTx()
    client2 = _FakeClient(tx2)
    ledger2 = SkipLedger(client2, "run-123", "fundbox", attempt_key="attempt-1")
    ledger2.add_pipeline_skip("excluded_identity", source_record_id="rec-1")
    ledger2.flush()

    query2, kwargs2 = tx2.calls[0]
    rows2 = kwargs2["rows"]
    skip_id2 = rows2[0]["skip_id"]

    assert skip_id1 == skip_id2


def test_attempt_keys_make_skip_ids_distinct() -> None:
    first = skip_row(
        "run-123",
        "connector",
        "fundbox",
        "missing_id",
        None,
        "users#missing_id",
        attempt_key="one",
    )
    second = skip_row(
        "run-123",
        "connector",
        "fundbox",
        "missing_id",
        None,
        "users#missing_id",
        attempt_key="two",
    )
    assert first["skip_id"] != second["skip_id"]


def test_ledger_run_counters_satisfy_invariant() -> None:
    tx = _FakeTx(skip_count=3)
    client = _FakeClient(tx)
    ledger = SkipLedger(client, "run-invariant", "fundbox")

    pipeline_mock = MagicMock(spec=IngestPipeline)

    results = [
        IngestResult(source_record_id="rec-1", person_id="p-1"),
        IngestResult(source_record_id="rec-2", skipped_duplicate=True),
        IngestResult(source_record_id="rec-3", errors=["fail"]),
        IngestResult(source_record_id="rec-4", parked=True),
        IngestResult(source_record_id="rec-5", excluded=True, skip_reason="test_excluded"),
    ]
    pipeline_mock.ingest.side_effect = results

    raw_records = [
        {
            "source_record_id": f"rec-{i}",
            "observed_at": "2026-01-01T00:00:00Z",
            "record_hash": f"hash-{i}",
            "identifiers": [],
            "attributes": {},
            "raw_payload": {},
        }
        for i in range(1, 7)
    ]
    raw_records[5]["source_record_id"] = "rec-excl-id"

    exclusion_ctx = ExclusionContext(source_ids=frozenset({"rec-excl-id"}))
    connector = _DummyConnector(raw_records)
    connector.record_skip("connector_skip_reason", source_record_id="c-1", locator="loc-c1")

    success, errors, skipped = _ingest_all_records(
        client=client,
        pipeline=pipeline_mock,
        connector=connector,
        ingest_run_id="run-invariant",
        exclusion_context=exclusion_ctx,
        ledger=ledger,
    )

    assert ledger.emitted == 6
    assert ledger.persisted == 2  # rec-1 (normal) + rec-4 (parked)
    assert ledger.duplicates == 1  # rec-2
    assert ledger.record_errors == 1  # rec-3
    assert (
        ledger.pipeline_skips == 2
    )  # rec-5 (pipeline excluded) + rec-excl-id (pre-pipeline excluded)
    assert ledger.connector_skips == 1

    assert ledger.emitted == (
        ledger.persisted + ledger.duplicates + ledger.pipeline_skips + ledger.record_errors
    )
    tx.reconciliation = {
        "emitted_count": 6,
        "persisted_count": 2,
        "duplicate_count": 1,
        "pipeline_skip_count": 2,
        "connector_skip_count": 1,
        "record_error_count": 1,
        "stored_skips": 3,
        "expected_skips": 3,
        "reconciled": True,
    }

    reconciled = ledger.reconcile()
    assert reconciled is True
    assert ledger.reconciled is True


def _raw_record(index: int) -> dict[str, JsonValue]:
    return {
        "source_record_id": f"rec-{index}",
        "observed_at": "2026-01-01T00:00:00Z",
        "record_hash": f"hash-{index}",
        "identifiers": [],
        "attributes": {},
        "raw_payload": {},
    }


def _durable_attempt(tx: _FakeTx) -> dict[str, object]:
    """The counters the failure path durably wrote for the attempt."""
    [params] = [kwargs for query, kwargs in tx.calls if query == WRITE_INGEST_ATTEMPT]
    return params


def _assert_attempt_is_balanced(params: dict[str, object], *, emitted: int) -> None:
    assert params["emitted_count"] == emitted
    assert params["emitted_count"] == (
        cast(int, params["persisted_count"])
        + cast(int, params["duplicate_count"])
        + cast(int, params["pipeline_skip_count"])
        + cast(int, params["record_error_count"])
    )


class _MidIterationFailureConnector(SourceConnector):
    """Yields one record, then buffers a skip and fails before the next record."""

    def get_source_key(self) -> str:
        return "fundbox"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        yield _raw_record(1)
        self.record_skip("parse_drop", source_record_id=None, locator="row-2")
        raise RuntimeError("mid-iteration failure")


def test_connector_skip_is_drained_when_fetch_fails_before_the_next_record() -> None:
    pipeline = MagicMock(spec=IngestPipeline)
    pipeline.ingest.return_value = IngestResult(source_record_id="rec-1", person_id="p-1")

    tx = _FakeTx()
    client = _FakeClient(tx)
    ledger = SkipLedger(client, "run-mid-failure", "fundbox")

    with pytest.raises(RuntimeError, match="mid-iteration failure"):
        _ingest_all_records(
            client=client,
            pipeline=pipeline,
            connector=_MidIterationFailureConnector(),
            ingest_run_id="run-mid-failure",
            exclusion_context=ExclusionContext(),
            ledger=ledger,
        )

    assert ledger.connector_skips == 1

    ledger.flush_quietly()

    skip_writes = [kwargs for query, kwargs in tx.calls if query == WRITE_INGEST_SKIPS]
    assert len(skip_writes) == 1
    rows = cast(list[dict[str, object]], skip_writes[0]["rows"])
    assert len(rows) == 1
    assert rows[0]["stage"] == "connector"
    assert rows[0]["reason"] == "parse_drop"
    assert rows[0]["source_record_id"] is None
    assert rows[0]["locator"] == "row-2"

    _assert_attempt_is_balanced(_durable_attempt(tx), emitted=1)


def _excluded_extraction() -> ExtractionResult:
    return ExtractionResult(
        persons=[
            {"name": "Excluded One", "phone": "+6588889999", "email": None},
            {"name": "Excluded Two", "phone": "+6598765432", "email": None},
        ],
        possible_persons=[],
        transactions=[],
        chat_members=[],
        inquiries=[],
        strong_identifiers=[],
        weak_identifiers=[],
        summary=None,
        customer_sentiment=None,
        tone=None,
        purpose=None,
        outcome=None,
        difficulty=None,
        confidence=0.8,
    )


def _whatsapp_bundle() -> WhatsAppChatBundle:
    return WhatsAppChatBundle(
        chat_id="chat-1",
        chat_name="Customer One",
        session_id="session-1",
        whatsapp_user_id="6568505434@c.us",
        tenant="speedzone",
        msg_text="conversation",
        observed_at="2026-05-06T00:00:00",
        participants=[],
        message_endpoints=[],
        session_phone="+6568505434",
    )


def _bitrix_bundle() -> BitrixChatBundle:
    return BitrixChatBundle(
        chat_id=1,
        deal_id=2,
        bitrix_chat_id="chat-1",
        category_name="Speedzone",
        last_message_at=datetime(2026, 5, 6),
        created_at=datetime(2026, 5, 6),
        entity="speedzone",
        conv_text="conversation",
        deal=None,
        agents=[],
    )


_EXCLUDED_PHONES = ["+6588889999", "+6598765432"]


def test_whatsapp_records_one_skip_per_excluded_person() -> None:
    skips: list[ConnectorSkip] = []

    envelopes = build_whatsapp_envelopes(
        bundle=_whatsapp_bundle(),
        extraction=_excluded_extraction(),
        company_mobile_numbers=_EXCLUDED_PHONES,
        on_skip=skips.append,
    )

    assert envelopes == []
    assert [skip.locator for skip in skips] == [
        "whatsapp-chat-chat-1#excluded-person-1",
        "whatsapp-chat-chat-1#excluded-person-2",
    ]
    assert {skip.reason for skip in skips} == {"person_excluded"}


def test_bitrix_records_one_skip_per_excluded_person() -> None:
    skips: list[ConnectorSkip] = []

    envelopes = BitrixChatConnector()._build_envelopes(
        bundle=_bitrix_bundle(),
        extraction=_excluded_extraction(),
        company_mobile_numbers=_EXCLUDED_PHONES,
        on_skip=skips.append,
    )

    assert envelopes == []
    assert [skip.locator for skip in skips] == [
        "bitrix-chat-1#excluded-person-1",
        "bitrix-chat-1#excluded-person-2",
    ]
    assert {skip.reason for skip in skips} == {"person_excluded"}


def _run_until_failure(
    pipeline: MagicMock,
    records: list[dict[str, JsonValue]],
    *,
    max_rows: int | None = None,
) -> tuple[SkipLedger, _FakeTx, BaseException]:
    tx = _FakeTx()
    client = _FakeClient(tx)
    ledger = SkipLedger(client, "run-abort", "fundbox")
    execution = SimpleNamespace(max_rows=max_rows, deadline_monotonic=None, fence_context=None)
    with pytest.raises(Exception) as raised:
        _ingest_all_records(
            client=client,
            pipeline=pipeline,
            connector=_DummyConnector(records),
            ingest_run_id="run-abort",
            exclusion_context=ExclusionContext(),
            execution_context=cast(ExecutionContext, execution) if max_rows else None,
            ledger=ledger,
        )
    ledger.flush_quietly()  # what the run owner's failure path does
    return ledger, tx, raised.value


def test_row_ceiling_stop_leaves_a_balanced_durable_attempt() -> None:
    pipeline = MagicMock(spec=IngestPipeline)
    pipeline.ingest.return_value = IngestResult(source_record_id="rec-1", person_id="p-1")

    ledger, tx, error = _run_until_failure(pipeline, [_raw_record(1), _raw_record(2)], max_rows=1)

    assert "row ceiling" in str(error)
    assert ledger.emitted == ledger.persisted == 1
    _assert_attempt_is_balanced(_durable_attempt(tx), emitted=1)


def test_failure_mid_record_does_not_count_the_unclassified_record() -> None:
    pipeline = MagicMock(spec=IngestPipeline)
    pipeline.ingest.side_effect = [
        IngestResult(source_record_id="rec-1", person_id="p-1"),
        RuntimeError("write failed"),
    ]

    ledger, tx, _ = _run_until_failure(pipeline, [_raw_record(1), _raw_record(2)])

    assert ledger.emitted == ledger.persisted == 1
    _assert_attempt_is_balanced(_durable_attempt(tx), emitted=1)


def test_invalid_envelope_does_not_count_the_unclassified_record() -> None:
    pipeline = MagicMock(spec=IngestPipeline)
    pipeline.ingest.return_value = IngestResult(source_record_id="rec-1", person_id="p-1")
    invalid: dict[str, JsonValue] = {"source_record_id": "rec-2"}

    ledger, tx, _ = _run_until_failure(pipeline, [_raw_record(1), invalid])

    assert ledger.emitted == ledger.persisted == 1
    _assert_attempt_is_balanced(_durable_attempt(tx), emitted=1)


def test_watermark_page_failure_does_not_count_the_unclassified_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = iter([IngestResult(source_record_id="rec-1", person_id="p-1")])

    def _process(*_args: object, **_kwargs: object) -> IngestResult:
        try:
            return next(results)
        except StopIteration:
            raise RuntimeError("write failed") from None

    monkeypatch.setattr("src.main._process_record", _process)
    tx = _FakeTx()
    client = _FakeClient(tx)
    ledger = SkipLedger(client, "run-page", "fundbox")

    with pytest.raises(RuntimeError, match="write failed"):
        _process_page_records(
            cast(Neo4jClient, client),
            MagicMock(spec=IngestPipeline),
            "fundbox",
            (_raw_record(1), _raw_record(2)),
            "run-page",
            ExclusionContext(),
            "control-1",
            ledger=ledger,
        )
    ledger.flush_quietly()

    assert ledger.emitted == ledger.persisted == 1
    _assert_attempt_is_balanced(_durable_attempt(tx), emitted=1)


_SKIP_FLUSH_THRESHOLD = 500  # buffered skip rows that trigger an immediate ledger flush
_ATTEMPT_COUNTERS = (
    "emitted_count",
    "persisted_count",
    "duplicate_count",
    "pipeline_skip_count",
    "connector_skip_count",
    "record_error_count",
)


class _FlakySession(_FakeSession):
    def __init__(self, tx: _FakeTx, owner: _FlakyClient) -> None:
        super().__init__(tx)
        self._owner = owner

    def execute_write(self, fn: object) -> object:
        if self._owner.failures_left > 0:
            self._owner.failures_left -= 1
            raise ConnectionError("neo4j write dropped")
        return super().execute_write(fn)


class _FlakyClient(_FakeClient):
    """Fails its first ``fail_writes`` write transactions, as a dropped connection would."""

    def __init__(self, tx: _FakeTx, *, fail_writes: int) -> None:
        super().__init__(tx)
        self.failures_left = fail_writes

    def session(self) -> _FlakySession:
        return _FlakySession(self.tx, self)


def _drive_skipped_records(
    runner: str,
    mode: str,
    client: _FakeClient,
    ledger: SkipLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feed one more skipped record than the flush threshold through a record loop."""
    records = [_raw_record(index) for index in range(1, _SKIP_FLUSH_THRESHOLD + 2)]
    policy = ExclusionContext(
        source_ids=frozenset(str(record["source_record_id"]) for record in records)
    )
    excluded_by_pipeline = IngestResult(source_record_id="rec", excluded=True, skip_reason="policy")
    exclusion = policy if mode == "pre_pipeline_exclusion" else ExclusionContext()
    if runner == "run_loop":
        pipeline = MagicMock(spec=IngestPipeline)
        pipeline.ingest.return_value = excluded_by_pipeline
        _ingest_all_records(
            client=client,
            pipeline=pipeline,
            connector=_DummyConnector(records),
            ingest_run_id="run-500",
            exclusion_context=exclusion,
            ledger=ledger,
        )
        return
    monkeypatch.setattr("src.main._process_record", lambda *_a, **_k: excluded_by_pipeline)
    _process_page_records(
        cast(Neo4jClient, client),
        MagicMock(spec=IngestPipeline),
        "fundbox",
        tuple(records),
        "run-500",
        exclusion,
        "control-1",
        ledger=ledger,
    )


@pytest.mark.parametrize("runner", ["run_loop", "watermark_page"])
@pytest.mark.parametrize("mode", ["pre_pipeline_exclusion", "pipeline_excluded_result"])
def test_failed_flush_on_the_500th_skip_leaves_balanced_attempts_across_a_retry(
    runner: str,
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tx = _FakeTx()
    client = _FlakyClient(tx, fail_writes=1)  # the flush the 500th skipped row triggers
    first = SkipLedger(client, "run-500", "fundbox", attempt_key="attempt-1")

    with pytest.raises(ConnectionError):
        _drive_skipped_records(runner, mode, client, first, monkeypatch)
    first.flush_quietly()  # the run owner's failure path, once the connection is back

    retry = SkipLedger(client, "run-500", "fundbox", attempt_key="attempt-2")
    _drive_skipped_records(runner, mode, client, retry, monkeypatch)
    retry.flush()

    snapshots = [kwargs for query, kwargs in tx.calls if query == WRITE_INGEST_ATTEMPT]
    # The retry flushed at the 500th skip too, so there are transient snapshots to check.
    assert len(snapshots) >= 3
    for snapshot in snapshots:
        _assert_attempt_is_balanced(snapshot, emitted=cast(int, snapshot["emitted_count"]))

    latest = {str(item["attempt_key"]): item for item in snapshots}
    assert set(latest) == {"attempt-1", "attempt-2"}
    # Attempt 1 classified exactly the 500 records before the failed flush; attempt 2 all 501.
    assert latest["attempt-1"]["emitted_count"] == _SKIP_FLUSH_THRESHOLD
    assert latest["attempt-1"]["pipeline_skip_count"] == _SKIP_FLUSH_THRESHOLD
    assert latest["attempt-2"]["emitted_count"] == _SKIP_FLUSH_THRESHOLD + 1
    # What RECONCILE_INGEST_RUN_ACCOUNTING sums across the run's attempts is balanced too.
    totals = {
        name: sum(cast(int, item[name]) for item in latest.values()) for name in _ATTEMPT_COUNTERS
    }
    _assert_attempt_is_balanced(totals, emitted=totals["emitted_count"])
    assert totals["emitted_count"] == 2 * _SKIP_FLUSH_THRESHOLD + 1


def test_checkpoint_flushes_after_500_emitted_records() -> None:
    tx = _FakeTx()
    client = _FakeClient(tx)
    ledger = SkipLedger(client, "run-checkpoint", "fundbox")

    ledger.emitted = 499
    ledger.checkpoint()
    assert client._session.write_calls == 0

    ledger.emitted = 500
    ledger.checkpoint()
    assert client._session.write_calls == 1
    assert [query for query, _params in tx.calls] == [WRITE_INGEST_ATTEMPT]


def test_reconcile_mismatch_logs_and_returns_false(caplog: pytest.LogCaptureFixture) -> None:
    tx = _FakeTx(skip_count=999)  # DB count does not match expected
    client = _FakeClient(tx)
    ledger = SkipLedger(client, "run-mismatch", "fundbox")
    ledger.emitted = 1
    ledger.persisted = 1

    with caplog.at_level(logging.ERROR):
        reconciled = ledger.reconcile()

    assert reconciled is False
    assert ledger.reconciled is False
    assert "Skip ledger reconciliation failed for run run-mismatch" in caplog.text


def test_identical_skips_keep_one_distinct_row_each() -> None:
    tx = _FakeTx(skip_count=3)
    ledger = SkipLedger(_FakeClient(tx), "run-dup", "onediver", attempt_key="attempt-dup")
    skip = ConnectorSkip(reason="missing_id", source_record_id=None, locator="profiles#missing_id")

    ledger.add_connector_skips([skip, skip, skip])
    ledger.flush()

    skip_query, params = tx.calls[0]
    assert skip_query == WRITE_INGEST_SKIPS
    rows = cast(list[dict[str, str]], params["rows"])
    assert len({row["skip_id"] for row in rows}) == 3
    first = skip_row(
        "run-dup",
        "connector",
        "onediver",
        "missing_id",
        None,
        "profiles#missing_id",
        attempt_key="attempt-dup",
    )
    assert rows[0]["skip_id"] == first["skip_id"]
    tx.reconciliation = {
        "emitted_count": 0,
        "persisted_count": 0,
        "duplicate_count": 0,
        "pipeline_skip_count": 0,
        "connector_skip_count": 3,
        "record_error_count": 0,
        "stored_skips": 3,
        "expected_skips": 3,
        "reconciled": True,
    }
    assert ledger.reconcile() is True
    assert tx.calls[-1][0] == RECONCILE_INGEST_RUN_ACCOUNTING


class _FailingSession:
    def execute_write(self, fn: object) -> object:
        raise RuntimeError("neo4j unavailable")

    def __enter__(self) -> _FailingSession:
        return self

    def __exit__(self, *args: object) -> None:
        pass


class _FailingClient:
    def session(self) -> _FailingSession:
        return _FailingSession()


def test_failed_flush_keeps_rows_and_quiet_flush_never_raises() -> None:
    ledger = SkipLedger(_FailingClient(), "run-fail", "fundbox")  # type: ignore[arg-type]
    ledger.add_pipeline_skip("excluded_identity", source_record_id="rec-1")

    with pytest.raises(RuntimeError):
        ledger.flush()
    ledger.flush_quietly()

    assert len(ledger._buffer) == 1
    assert ledger.reconcile() is False


def test_run_ingestion_fails_closed_when_reconcile_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.main as main_module

    class _ClientStub(_FakeClient):
        def verify_connectivity(self) -> None:
            return None

        def close(self) -> None:
            return None

    tx = _FakeTx(skip_count=999)
    client = _ClientStub(tx)
    failed_runs: list[str] = []
    finalized_runs: list[str] = []

    monkeypatch.setattr(main_module, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(main_module, "Neo4jClient", lambda _settings: client)
    monkeypatch.setattr(main_module, "IngestPipeline", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr(main_module, "_create_ingest_run", lambda *_a, **_kw: "run-reconcile-fail")
    monkeypatch.setattr(main_module, "get_connector", lambda *_a, **_kw: _DummyConnector([]))
    monkeypatch.setattr(main_module, "_load_exclusion_context", ExclusionContext)
    monkeypatch.setattr(main_module, "drain_pending_customer_sales", lambda *_a, **_kw: 0)
    monkeypatch.setattr(
        main_module, "propose_vehicle_matches_for_pending_sales", lambda *_a, **_kw: 0
    )
    monkeypatch.setattr(
        main_module,
        "_mark_run_failed",
        lambda _client, run_id, *_a, **_kw: failed_runs.append(run_id),
    )
    monkeypatch.setattr(
        main_module,
        "finalize_ingest_run",
        lambda _client, run_id, *_a, **_kw: finalized_runs.append(run_id),
    )

    with pytest.raises(
        RuntimeError, match="Skip ledger reconciliation failed for run run-reconcile-fail"
    ):
        main_module.run_ingestion("fundbox", mode="batch", initialize_graph=False)

    assert failed_runs == ["run-reconcile-fail"]
    assert finalized_runs == []


def test_connector_drain_over_500_skips_persists_tail_after_failed_flush_and_recovery() -> None:
    """When a connector drain exceeds 500 skips and the triggered flush fails, all rows

    remain buffered through finally draining an empty list, and persist on recovery.
    """
    tx = _FakeTx(skip_count=505)
    client = _FlakyClient(tx, fail_writes=1)
    ledger = SkipLedger(client, "run-drain-505", "fundbox", attempt_key="attempt-1")
    pipeline = MagicMock(spec=IngestPipeline)

    connector = _DummyConnector([_raw_record(1)])
    for i in range(503):
        connector.record_skip("bad_phone", source_record_id=f"rec-{i}", locator=f"row-{i}")
    # Record duplicate skips with identical locator to test repeat occurrence numbering
    connector.record_skip("duplicate_phone", locator="row-repeat")
    connector.record_skip("duplicate_phone", locator="row-repeat")

    with pytest.raises(ConnectionError, match="neo4j write dropped"):
        _ingest_all_records(
            client=client,
            pipeline=pipeline,
            connector=connector,
            ingest_run_id="run-drain-505",
            exclusion_context=ExclusionContext(),
            ledger=ledger,
        )

    # All 505 skips were buffered before flush failed; finally block's drain of []
    # preserved the buffer, did not retry prematurely, and preserved the exception.
    assert ledger.connector_skips == 505
    assert len(ledger._buffer) == 505
    assert len(tx.calls) == 0

    # Draining an empty list directly is a safe no-op that does not trigger flush
    ledger.add_connector_skips([])
    assert len(ledger._buffer) == 505

    # Run owner's failure teardown recovers and flushes all 505 skips
    ledger.flush_quietly()

    assert len(ledger._buffer) == 0
    assert ledger.connector_skips == 505

    skip_writes = [kwargs for query, kwargs in tx.calls if query == WRITE_INGEST_SKIPS]
    assert len(skip_writes) == 1
    rows = cast(list[dict[str, object]], skip_writes[0]["rows"])
    assert len(rows) == 505

    # Each skip is persisted once with a unique skip_id
    skip_ids = {cast(str, row["skip_id"]) for row in rows}
    assert len(skip_ids) == 505

    # Occurrence numbering for repeats was preserved across failure and recovery
    repeat_rows = [r for r in rows if r["locator"] == "row-repeat"]
    assert len(repeat_rows) == 2
    assert repeat_rows[0]["skip_id"] != repeat_rows[1]["skip_id"]

    attempt_writes = [kwargs for query, kwargs in tx.calls if query == WRITE_INGEST_ATTEMPT]
    assert len(attempt_writes) == 1
    assert attempt_writes[0]["connector_skip_count"] == 505
    assert attempt_writes[0]["emitted_count"] == 0

    tx.reconciliation = {
        "emitted_count": 0,
        "persisted_count": 0,
        "duplicate_count": 0,
        "pipeline_skip_count": 0,
        "connector_skip_count": 505,
        "record_error_count": 0,
        "stored_skips": 505,
        "expected_skips": 505,
        "reconciled": True,
    }
    assert ledger.reconcile() is True
    assert ledger.reconciled is True
