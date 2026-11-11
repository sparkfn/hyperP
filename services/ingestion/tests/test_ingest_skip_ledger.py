"""Unit tests for SkipLedger, SkipRecorder, and related skip tracking."""

from __future__ import annotations

import logging
from typing import cast
from unittest.mock import MagicMock

import pytest
from src.connectors.base import SourceConnector
from src.exclusions import ExclusionContext
from src.graph.queries.ingest_skips import (
    RECONCILE_INGEST_RUN_ACCOUNTING,
    WRITE_INGEST_ATTEMPT,
    WRITE_INGEST_SKIPS,
)
from src.ingest_skips import ConnectorSkip, SkipLedger, SkipRecorder, skip_row
from src.main import _ingest_all_records
from src.models import (
    IngestResult,
    JsonValue,
)
from src.pipeline import IngestPipeline


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
