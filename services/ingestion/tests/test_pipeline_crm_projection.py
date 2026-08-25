"""Regression coverage for Bitrix CRM activity projection v2."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

from neo4j import ManagedTransaction
from pytest import MonkeyPatch
from src.bitrix_ingestion_models import CrmActivityProjection
from src.graph import queries
from src.graph.client import Neo4jClient
from src.models import RecordType, SourceRecordEnvelope, SourceRecordParentRef
from src.pipeline_crm import ingest_crm_history_record


class _Result:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def single(self) -> dict[str, object] | None:
        return self._row

    def consume(self) -> None:
        return None


class _Tx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **kwargs: object) -> _Result:
        self.calls.append((query, kwargs))
        if query == queries.FIND_ANY_SOURCE_RECORD:
            return _Result(
                {
                    "source_record_pk": "history-pk",
                    "record_hash": "sha256:unchanged",
                    "history_family": "crm_activity",
                    "history_kind": "call",
                    "history_source": "bitrix_crm_activity",
                    "event_at": "2026-08-07T00:00:00Z",
                    "projection_version": 2,
                    "projection_source": "bitrix_crm_activity_v1",
                }
            )
        if query == queries.REMATERIALIZE_CRM_HISTORY_PROJECTION:
            return _Result(None)
        raise AssertionError("unexpected query")


class _Session:
    def __init__(self, tx: _Tx) -> None:
        self._tx = tx

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute_write(self, work: Callable[[ManagedTransaction], object]) -> object:
        return work(cast(ManagedTransaction, self._tx))


class _Client:
    def __init__(self) -> None:
        self.tx = _Tx()

    def session(self) -> _Session:
        return _Session(self.tx)


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
        record_hash="sha256:unchanged",
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


def test_bitrix_activity_projection_defaults_to_canonical_v2() -> None:
    projection = CrmActivityProjection(history_kind="call", event_at=None)

    assert projection.history_family == "activity"
    assert projection.projection_version == 2
    assert projection.projection_source == "bitrix_crm_activity_v2"


def test_same_hash_legacy_activity_rematerializes_as_v2(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr("src.pipeline_crm.load_locked_source_state", lambda *_args: None)
    client = _Client()

    result = ingest_crm_history_record(
        cast(Neo4jClient, client),
        _activity_envelope(),
        ingest_run_id="run-1",
    )

    assert result.source_record_pk == "history-pk"
    assert result.skipped_duplicate is True
    assert client.tx.calls[0] == (
        queries.FIND_ANY_SOURCE_RECORD,
        {
            "source_system": "bitrix_chat",
            "source_instance_id": "legacy-default",
            "source_record_id": "bitrix-crm-history-900",
        },
    )
    query, params = client.tx.calls[1]
    assert query == queries.REMATERIALIZE_CRM_HISTORY_PROJECTION
    assert params == {
        "source_record_pk": "history-pk",
        "record_hash": "sha256:unchanged",
        "history_family": "activity",
        "history_kind": "call",
        "history_source": "bitrix_crm_activity",
        "event_at": "2026-08-07T00:00:00Z",
        "projection_version": 2,
        "projection_source": "bitrix_crm_activity_v2",
    }
    assert "history.history_family = 'crm_activity'" in query
    assert "history.history_source = 'bitrix_crm_activity'" in query
    assert "history.projection_source = 'bitrix_crm_activity_v1'" in query
    assert "coalesce(history.projection_version, 0) <= $projection_version" in query
    assert "SET history.record_hash" not in query
    assert "SET history.raw_payload" not in query
    assert "SET history.source_record_id" not in query
