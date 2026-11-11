"""Persist-first parking: previously dropped records now persist unlinked.

A record that cannot be linked to a Person (match-only source with no usable
match, or an unresolved CRM-deal contact set) is persisted as a ``parked``
SourceRecord with no identity evidence, no MatchDecision, no ReviewCase and no
version activation. Parking the same hash again is idempotent.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import cast
from unittest.mock import MagicMock

import pytest
from _txmock import _RecordingTx
from neo4j import ManagedTransaction
from pytest import MonkeyPatch
from src.bitrix_backfill_models import GenerationRunContext
from src.bitrix_backfill_runtime import _result_disposition
from src.bitrix_ingestion_models import ExecutionContext, FenceContext
from src.config import IdentityModel
from src.graph import queries
from src.graph.queries.identity_link_revisions import APPEND_IDENTITY_LINK_REVISIONS
from src.graph.queries.source_records import (
    FIND_PARKED_SOURCE_RECORD_BY_HASH,
    SET_SOURCE_RECORD_ATTRIBUTION,
)
from src.models import (
    IngestResult,
    MatchDecision,
    MatchResult,
    RecordType,
    SourceRecordEnvelope,
)
from src.pipeline import IngestPipeline
from src.resumable import CheckpointDescriptor

_EMPTY_LOCK_STATE: dict[str, object] = {
    "source_record_pk": None,
    "source_record_version": None,
    "record_hash": None,
    "lifecycle_status": None,
    "linked_person_ids": [],
    "max_source_record_version": None,
}

_ACTIVE_LOCK_STATE: dict[str, object] = {
    "source_record_pk": "sr-active",
    "source_record_version": 1,
    "record_hash": "sha256:previous",
    "lifecycle_status": "active",
    "linked_person_ids": ["person-1"],
    "normalized_payload": "{}",
    "raw_payload": "{}",
    "max_source_record_version": 1,
}

_FORBIDDEN_PARK_QUERIES: tuple[str, ...] = (
    queries.CREATE_MATCH_DECISION,
    queries.CREATE_REVIEW_CASE,
    queries.LINK_SOURCE_RECORD_TO_PERSON,
    queries.ACTIVATE_FIRST_SOURCE_RECORD_VERSION,
    queries.ACTIVATE_SOURCE_RECORD_VERSION,
    APPEND_IDENTITY_LINK_REVISIONS,
)


class _Result:
    def __init__(
        self,
        row: dict[str, object] | None = None,
        rows: list[dict[str, object]] | None = None,
    ) -> None:
        self._row = row
        self._rows = rows or ([] if row is None else [row])

    def single(self) -> dict[str, object] | None:
        return self._row

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._rows)

    def consume(self) -> None:
        return None


class _ParkedTx(_RecordingTx):
    """Canned graph answers for the parked path; every call is recorded."""

    def __init__(
        self,
        *,
        parked_hit: str | None = None,
        lock_row: dict[str, object] | None = None,
    ) -> None:
        super().__init__()
        self._parked_hit = parked_hit
        self._lock_row = dict(_EMPTY_LOCK_STATE) if lock_row is None else dict(lock_row)

    def run(self, query: str, **kwargs: object) -> _Result:
        self._record(query, kwargs)
        if query == FIND_PARKED_SOURCE_RECORD_BY_HASH:
            if self._parked_hit is None:
                return _Result()
            return _Result({"source_record_pk": self._parked_hit})
        if query == queries.LOCK_AND_GET_SOURCE_STATE:
            return _Result(dict(self._lock_row))
        if query == queries.CREATE_SOURCE_RECORD:
            return _Result({"source_record_pk": "sr-parked"})
        return _Result()


class _Session:
    def __init__(self, tx: _RecordingTx) -> None:
        self._tx = tx

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def execute_write(self, callback: object) -> object:
        return callback(cast(ManagedTransaction, self._tx))  # type: ignore[operator]


class _Client:
    def __init__(self, tx: _RecordingTx) -> None:
        self._tx = tx

    def session(self) -> _Session:
        return _Session(self._tx)

    def execute_read(self, callback: object) -> object:
        return callback(cast(ManagedTransaction, self._tx))  # type: ignore[operator]


def _conversation_envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="whatsapp_chat",
        source_record_id="whatsapp-session-1",
        record_type=RecordType.CONVERSATION,
        observed_at="2026-05-08T09:47:25+00:00",
        record_hash="sha256:chat",
        identifiers=[{"type": "phone", "value": "+6591234567", "is_verified": True}],
        raw_payload={"session_id": "session-1"},
        extraction_confidence=0.9,
        extraction_method="llm",
    )


def _call_envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="bitrix_chat",
        source_record_id="bitrix-call-900",
        record_type=RecordType.CALL,
        observed_at="2026-05-08T09:47:25+00:00",
        record_hash="sha256:call",
        identifiers=[{"type": "phone", "value": "+6591234567", "is_verified": True}],
        raw_payload={"crm_activity_id": "900", "owner_id": "501"},
    )


def _bankruptcy_envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="sgbankruptcy",
        source_record_id="bankruptcy_case:1",
        record_type=RecordType.BANKRUPTCY,
        observed_at="2026-05-08T09:47:25+00:00",
        record_hash="sha256:abc",
        identifiers=[{"type": "nric", "value": "S1234567A", "is_verified": True}],
        attributes={"full_name": "Ada Lovelace"},
        raw_payload={"case": {"id": "1"}},
    )


def _ingest(
    envelope: SourceRecordEnvelope,
    tx: _RecordingTx,
    *,
    identity_model: IdentityModel | None = None,
    execution_context: ExecutionContext | None = None,
) -> IngestResult:
    pipeline = IngestPipeline(
        cast(object, _Client(tx)),
        identity_model=identity_model,
        execution_context=execution_context,
    )
    return pipeline.ingest(envelope, ingest_run_id="run-1")


def _creates(tx: _RecordingTx) -> list[dict[str, object]]:
    return [params for query, params in tx.calls if query == queries.CREATE_SOURCE_RECORD]


@pytest.mark.parametrize(
    "envelope_factory",
    [_conversation_envelope, _call_envelope, _bankruptcy_envelope],
    ids=["conversation", "call", "bankruptcy_no_match"],
)
def test_unmatched_record_is_persisted_parked_without_identity_writes(
    envelope_factory: Callable[[], SourceRecordEnvelope],
) -> None:
    tx = _ParkedTx()

    result = _ingest(envelope_factory(), tx)

    assert result.parked is True
    assert result.skipped_duplicate is False
    assert result.match_decision is None
    assert result.source_record_pk == "sr-parked"
    creates = _creates(tx)
    assert len(creates) == 1
    assert creates[0]["lifecycle_status"] == "parked"
    assert creates[0]["link_status"] == "unlinked"
    assert creates[0]["expected_active_source_record_pk"] is None
    attribution = [params for query, params in tx.calls if query == SET_SOURCE_RECORD_ATTRIBUTION]
    assert attribution == [
        {
            "source_record_pk": "sr-parked",
            "attribution_status": "parked",
            "reeval_pending": True,
        }
    ]
    issued = [query for query, _ in tx.calls]
    for forbidden in _FORBIDDEN_PARK_QUERIES:
        assert forbidden not in issued
    assert not any("CREATE (p:Person" in query for query in issued)


def test_reparking_the_same_hash_is_a_duplicate_without_writes() -> None:
    tx = _ParkedTx(parked_hit="sr-existing-parked")

    result = _ingest(_bankruptcy_envelope(), tx)

    assert result.skipped_duplicate is True
    assert result.parked is True
    assert result.source_record_pk == "sr-existing-parked"
    assert _creates(tx) == []


def test_parked_write_never_supersedes_an_active_version() -> None:
    tx = _ParkedTx(lock_row=_ACTIVE_LOCK_STATE)
    pipeline = IngestPipeline(cast(object, _Client(tx)))
    pipeline._match_engine = MagicMock(
        evaluate=MagicMock(return_value=MatchResult(decision=MatchDecision.NO_MATCH))
    )

    result = pipeline.ingest(_bankruptcy_envelope(), ingest_run_id="run-1")

    assert result.parked is True
    creates = _creates(tx)
    assert len(creates) == 1
    assert creates[0]["expected_active_source_record_pk"] is None
    issued = [query for query, _ in tx.calls]
    assert queries.ACTIVATE_FIRST_SOURCE_RECORD_VERSION not in issued
    assert queries.ACTIVATE_SOURCE_RECORD_VERSION not in issued
    assert queries.RETIRE_IDENTITY_PROJECTIONS not in issued


def _execution_context() -> ExecutionContext:
    return ExecutionContext(
        worker_task_id="task-1",
        fence_context=FenceContext(
            logical_run_id="lr-1",
            ingest_run_id="ir-1",
            source_key="bitrix_chat",
            stream_key="crm_deals",
            stream_generation=1,
            fencing_token=1,
            attempt_generation=1,
        ),
        checkpoint=CheckpointDescriptor(
            phase="test",
            cursor={},
            source_window={},
            last_committed_record_id=None,
            connector_version="v1",
            schema_version=1,
            replay_boundary="boundary-1",
        ),
        generation_context=GenerationRunContext(
            generation_id="gen-1",
            boundary_digest="bd-1",
            configuration_digest="cd-1",
        ),
    )


def _deal_envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="bitrix_chat",
        source_record_id="bitrix-crm-deal-501",
        record_type=RecordType.CRM_DEAL,
        entity_key="eko",
        observed_at="2026-05-08T09:47:25+00:00",
        record_hash="sha256:deal",
        identifiers=[{"type": "phone", "value": "+6591234567", "is_verified": True}],
        raw_payload={
            "category_id": "1",
            "crm_contact_resolution_required": True,
            "primary_contact_id": None,
        },
    )


def test_crm_deal_with_unresolved_contacts_is_parked_without_scope_link(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.pipeline.assert_active_bitrix_fence", lambda *_a, **_kw: None)
    monkeypatch.setattr("src.pipeline.resolve_canonical_crm_contact", lambda *_a, **_kw: None)
    monkeypatch.setattr("src.pipeline.record_terminal_unit", lambda *_a, **_kw: None)
    observations: list[object] = []
    monkeypatch.setattr(
        "src.pipeline.record_scope_batch_in_transaction",
        lambda _tx, rows, **_kw: observations.extend(rows),
    )
    tx = _ParkedTx()

    result = _ingest(_deal_envelope(), tx, execution_context=_execution_context())

    assert result.parked is True
    assert result.person_id is None
    creates = _creates(tx)
    assert len(creates) == 1
    assert creates[0]["lifecycle_status"] == "parked"
    assert creates[0]["link_status"] == "unlinked"
    assert creates[0]["entity_key"] == "eko"
    issued = [query for query, _ in tx.calls]
    for forbidden in _FORBIDDEN_PARK_QUERIES:
        assert forbidden not in issued
    assert len(observations) == 1
    observation = observations[0]
    assert observation.source_record_pk is None


def test_shadow_assertions_are_flag_gated(monkeypatch: MonkeyPatch) -> None:
    recorded: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "src.pipeline_writes.write_assertions",
        lambda _tx, pk, normalized: recorded.append((pk, normalized)),
    )

    v1_result = _ingest(_bankruptcy_envelope(), _ParkedTx(), identity_model="v1")
    assert v1_result.parked is True
    assert recorded == []

    shadow_result = _ingest(_bankruptcy_envelope(), _ParkedTx(), identity_model="v2_shadow")

    assert shadow_result.parked is True
    assert len(recorded) == 1
    pk, normalized = recorded[0]
    assert pk == "sr-parked"
    identifiers = normalized["identifiers"]
    assert isinstance(identifiers, list)
    assert identifiers[0]["identifier_type"] == "nric"


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (IngestResult(source_record_id="r", parked=True), "conflict"),
        (IngestResult(source_record_id="r", parked=True, skipped_duplicate=True), "conflict"),
        (IngestResult(source_record_id="r", excluded=True), "conflict"),
        (IngestResult(source_record_id="r", skipped_duplicate=True), "existing_same_hash"),
        (IngestResult(source_record_id="r"), "created"),
    ],
)
def test_parked_units_keep_the_v1_conflict_coverage_disposition(
    result: IngestResult, expected: str
) -> None:
    assert _result_disposition(result) == expected
