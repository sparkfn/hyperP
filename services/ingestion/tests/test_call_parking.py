"""An in-scope call with no person context is parked, never dropped, and creates no Person."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

from neo4j import ManagedTransaction
from pytest import MonkeyPatch
from src.graph import queries
from src.graph.client import Neo4jClient
from src.models import IngestResult, RecordType, SourceRecordEnvelope, SourceRecordParentRef
from src.pipeline_crm import ingest_call_record


class _Result:
    def __init__(self, row: dict[str, object] | None = None) -> None:
        self._row = row

    def single(self) -> dict[str, object] | None:
        return self._row

    def consume(self) -> None:
        return None


class _Tx:
    """Records every query; only the parked lookup can return a row."""

    def __init__(self, parked_pk: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._parked_pk = parked_pk

    def run(self, query: str, **kwargs: object) -> _Result:
        self.calls.append((query, kwargs))
        if query == queries.FIND_PARKED_SOURCE_RECORD_BY_HASH and self._parked_pk is not None:
            return _Result({"source_record_pk": self._parked_pk})
        if query == queries.CREATE_SOURCE_RECORD:
            return _Result({"source_record_pk": "sr-parked-call"})
        return _Result(None)


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
    def __init__(self, tx: _Tx) -> None:
        self._tx = tx

    def session(self) -> _Session:
        return _Session(self._tx)


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


def _ingest(
    monkeypatch: MonkeyPatch,
    tx: _Tx,
    *,
    next_version: int = 1,
) -> tuple[IngestResult, list[dict[str, object]]]:
    """Run the call route with no history parent, so call creation returns no row."""
    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(
        "src.pipeline_crm.load_locked_source_state",
        lambda *_a: SimpleNamespace(next_version=next_version),
    )
    monkeypatch.setattr(
        "src.pipeline_crm._record_activity_unit",
        lambda *_a, **kwargs: recorded.append(dict(kwargs)),
    )
    result = ingest_call_record(
        cast(Neo4jClient, _Client(tx)),
        _call_envelope(),
        ingest_run_id="run-1",
    )
    return result, recorded


def _queries(tx: _Tx) -> list[str]:
    return [query for query, _ in tx.calls]


def test_call_without_person_context_is_parked_without_any_person(
    monkeypatch: MonkeyPatch,
) -> None:
    tx = _Tx()

    result, recorded = _ingest(monkeypatch, tx)

    assert result.parked is True
    assert result.excluded is False
    assert result.skipped_duplicate is False
    assert result.source_record_pk == "sr-parked-call"
    [create] = [params for query, params in tx.calls if query == queries.CREATE_SOURCE_RECORD]
    assert create["record_type"] == "call"
    assert create["lifecycle_status"] == "parked"
    assert create["link_status"] == "unlinked"
    assert create["expected_active_source_record_pk"] is None
    assert create["source_record_version"] == "1"
    issued = _queries(tx)
    for forbidden in (
        queries.CREATE_PERSON,
        queries.LINK_SOURCE_RECORD_TO_PERSON,
        queries.CREATE_MATCH_DECISION,
        queries.ACTIVATE_FIRST_SOURCE_RECORD_VERSION,
    ):
        assert forbidden not in issued
    assert [item["disposition"] for item in recorded] == ["conflict"]
    assert result.skip_reason is None


def test_rerun_with_the_same_hash_reuses_the_latest_parked_call(monkeypatch: MonkeyPatch) -> None:
    tx = _Tx(parked_pk="sr-existing-parked")

    result, _ = _ingest(monkeypatch, tx, next_version=2)

    assert result.parked is True
    assert result.skipped_duplicate is True
    assert result.source_record_pk == "sr-existing-parked"
    assert queries.CREATE_SOURCE_RECORD not in _queries(tx)
    [lookup] = [
        params for query, params in tx.calls if query == queries.FIND_PARKED_SOURCE_RECORD_BY_HASH
    ]
    assert lookup["latest_version"] == 1


def test_parked_call_never_blocks_the_real_call(monkeypatch: MonkeyPatch) -> None:
    # The real call is created at the next version, so its version key cannot collide with the
    # parked stub (which already holds version 1).
    tx = _Tx()

    _ingest(monkeypatch, tx, next_version=2)

    [create_call] = [
        params for query, params in tx.calls if query == queries.CREATE_CALL_FROM_HISTORY
    ]
    assert create_call["source_record_version"] == "2"
    assert "sv" in str(create_call["source_version_key"])
    [stub] = [params for query, params in tx.calls if query == queries.CREATE_SOURCE_RECORD]
    assert stub["source_record_version"] == "2"
    assert stub["source_version_key"] == create_call["source_version_key"]


def test_parked_rows_are_invisible_to_the_existing_check_and_version_is_parameterised() -> None:
    assert "<> 'parked'" in queries.FIND_ANY_SOURCE_RECORD
    assert "source_record_version: $source_record_version" in queries.CREATE_CALL_FROM_HISTORY
    assert "source_record_version: '1'" not in queries.CREATE_CALL_FROM_HISTORY


def test_real_call_clears_reeval_pending_on_prior_parked_stubs_for_the_same_identity() -> None:
    query = queries.CREATE_CALL_FROM_HISTORY

    assert (
        "OPTIONAL MATCH (parked:SourceRecord {\n"
        "    source_instance_id: $source_instance_id,\n"
        "    source_record_id: $source_record_id,\n"
        "    lifecycle_status: 'parked'\n"
        "})-[:FROM_SOURCE]->(ss)"
    ) in query
    assert "parked.reeval_pending = true" in query
    assert "SET stub.reeval_pending = false" in query
    assert "stub.attribution_eval_at = datetime()" in query
