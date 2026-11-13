"""Persist-first parking: previously dropped records now persist unlinked.

A record that cannot be linked to a Person (match-only source with no usable
match, or an unresolved CRM-deal contact set) is persisted as a ``parked``
SourceRecord with no identity evidence, no MatchDecision, no ReviewCase and no
version activation. Parking the same hash again is idempotent.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import cast
from unittest.mock import MagicMock

import pytest
from _txmock import _RecordingTx, _SummaryCounterResult, summary_counter_result
from neo4j import ManagedTransaction
from pytest import MonkeyPatch
from src.bitrix_backfill_models import GenerationRunContext
from src.bitrix_backfill_runtime import _result_disposition
from src.bitrix_ingestion_models import ExecutionContext, FenceContext
from src.config import IdentityModel
from src.graph import queries
from src.graph.queries.identity_assertions import (
    ASSERT_FACTS,
    ASSERT_IDENTIFIERS,
    MERGE_ASSERTION_IDENTIFIERS_BATCH,
    MERGE_PROFILE_FACTS_BATCH,
)
from src.graph.queries.identity_link_revisions import APPEND_IDENTITY_LINK_REVISIONS
from src.graph.queries.source_records import (
    FIND_PARKED_SOURCE_RECORD_BY_HASH,
    PROMOTE_STAGED_SOURCE_RECORD,
    SET_SOURCE_RECORD_ATTRIBUTION,
)
from src.identity_assertions import assertion_rows
from src.models import (
    IngestResult,
    MatchDecision,
    MatchResult,
    RecordType,
    SourceRecordEnvelope,
    SourceRecordParentRef,
)
from src.pipeline import IngestPipeline
from src.pipeline_addresses import ingest_address_record
from src.pipeline_crm_identity import plan_crm_deal_identity, projected_identifiers
from src.pipeline_normalization import normalize_envelope_identifiers
from src.pipeline_references import ingest_reference_record
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

    def run(self, query: str, **kwargs: object) -> _Result | _SummaryCounterResult:
        counter = summary_counter_result(query)
        if counter is not None:
            return counter
        self._record(query, kwargs)
        if query == FIND_PARKED_SOURCE_RECORD_BY_HASH:
            if self._parked_hit is None:
                return _Result()
            return _Result({"source_record_pk": self._parked_hit})
        if query == queries.LOCK_AND_GET_SOURCE_STATE:
            return _Result(dict(self._lock_row))
        if query in (
            queries.CREATE_SOURCE_RECORD,
            queries.ACTIVATE_FIRST_SOURCE_RECORD_VERSION,
            queries.ACTIVATE_SOURCE_RECORD_VERSION,
        ):
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
        parent_ref=SourceRecordParentRef(
            parent_source_system="bitrix_chat",
            parent_source_record_id="bitrix-crm-history-900",
            parent_record_type=RecordType.CRM_HISTORY,
        ),
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


def test_find_parked_query_contains_latest_version_guard() -> None:
    assert "$latest_version" in FIND_PARKED_SOURCE_RECORD_BY_HASH
    assert (
        "toInteger(sr.source_record_version) = $latest_version" in FIND_PARKED_SOURCE_RECORD_BY_HASH
    )


def test_reparking_the_same_hash_is_a_duplicate_without_writes() -> None:
    tx = _ParkedTx(parked_hit="sr-existing-parked")

    result = _ingest(_bankruptcy_envelope(), tx)

    assert result.skipped_duplicate is True
    assert result.parked is True
    assert result.source_record_pk == "sr-existing-parked"
    assert _creates(tx) == []
    find_calls = [params for q, params in tx.calls if q == FIND_PARKED_SOURCE_RECORD_BY_HASH]
    assert len(find_calls) == 1
    assert find_calls[0]["latest_version"] == 0


def test_parked_revert_after_newer_version_creates_new_parked_row_instead_of_reusing_older() -> (
    None
):
    lock_row = {
        "source_record_pk": "sr-v2-active",
        "source_record_version": 2,
        "record_hash": "sha256:hash-b",
        "lifecycle_status": "active",
        "linked_person_ids": ["person-1"],
        "normalized_payload": "{}",
        "raw_payload": "{}",
        "max_source_record_version": 2,
    }
    tx = _ParkedTx(lock_row=lock_row)
    pipeline = IngestPipeline(cast(object, _Client(tx)))
    pipeline._match_engine = MagicMock(
        evaluate=MagicMock(return_value=MatchResult(decision=MatchDecision.NO_MATCH))
    )

    env = _bankruptcy_envelope().model_copy(update={"record_hash": "sha256:hash-a"})
    result = pipeline.ingest(env, ingest_run_id="run-1")

    assert result.parked is True
    assert result.skipped_duplicate is False

    find_calls = [params for q, params in tx.calls if q == FIND_PARKED_SOURCE_RECORD_BY_HASH]
    assert len(find_calls) == 1
    assert find_calls[0]["latest_version"] == 2

    creates = _creates(tx)
    assert len(creates) == 1
    assert creates[0]["source_record_version"] == "3"
    assert creates[0]["lifecycle_status"] == "parked"


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


@pytest.mark.parametrize(
    "envelope_factory",
    [_conversation_envelope, _call_envelope, _bankruptcy_envelope],
    ids=["conversation", "call", "bankruptcy_no_match"],
)
def test_shadow_assertions_are_flag_gated(
    envelope_factory: Callable[[], SourceRecordEnvelope],
    monkeypatch: MonkeyPatch,
) -> None:
    recorded: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "src.pipeline_writes.write_assertions",
        lambda _tx, pk, normalized: recorded.append((pk, normalized)),
    )

    v1_result = _ingest(envelope_factory(), _ParkedTx(), identity_model="v1")
    assert v1_result.parked is True
    assert recorded == []

    shadow_result = _ingest(envelope_factory(), _ParkedTx(), identity_model="v2_shadow")

    assert shadow_result.parked is True
    assert len(recorded) == 1
    pk, normalized = recorded[0]
    assert pk == "sr-parked"
    identifiers = normalized["identifiers"]
    assert isinstance(identifiers, list)
    assert identifiers[0]["identifier_type"] in ("phone", "nric")


def test_v2_shadow_parked_ingest_executes_real_assertion_queries() -> None:
    tx = _ParkedTx()
    result = _ingest(_bankruptcy_envelope(), tx, identity_model="v2_shadow")

    assert result.parked is True
    assert result.source_record_pk == "sr-parked"

    creates = _creates(tx)
    assert len(creates) == 1
    raw_normalized = creates[0]["normalized_payload"]
    assert isinstance(raw_normalized, str)
    stored_normalized = json.loads(raw_normalized)

    expected_rows = assertion_rows("sr-parked", stored_normalized)
    assert len(expected_rows.identifiers) == 1
    assert len(expected_rows.facts) == 1

    nric_row = expected_rows.identifiers[0]
    assert nric_row["hashed_value"] == nric_row["normalized_value"]
    assert nric_row["nric_masked"] == "S••••567A"
    assert nric_row["checksum_valid"] is False
    assert nric_row["nric_key_version"] == "v1"
    assert nric_row["nric_encrypted"] is None

    upsert_ident_calls = [
        params for query, params in tx.calls if query == MERGE_ASSERTION_IDENTIFIERS_BATCH
    ]
    assert len(upsert_ident_calls) == 1
    assert upsert_ident_calls[0]["identifiers"] == expected_rows.identifiers

    merge_fact_calls = [params for query, params in tx.calls if query == MERGE_PROFILE_FACTS_BATCH]
    assert len(merge_fact_calls) == 1
    assert merge_fact_calls[0]["facts"] == expected_rows.facts

    assert_ident_calls = [params for query, params in tx.calls if query == ASSERT_IDENTIFIERS]
    assert len(assert_ident_calls) == 1
    assert assert_ident_calls[0]["assertions"] == expected_rows.identifiers
    assert (
        assert_ident_calls[0]["assertions"][0]["assertion_id"]
        == expected_rows.identifiers[0]["assertion_id"]
    )

    assert_fact_calls = [params for query, params in tx.calls if query == ASSERT_FACTS]
    assert len(assert_fact_calls) == 1
    assert assert_fact_calls[0]["assertions"] == expected_rows.facts
    assert (
        assert_fact_calls[0]["assertions"][0]["assertion_id"]
        == expected_rows.facts[0]["assertion_id"]
    )


def _address_envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="sgrentalflats",
        source_record_id="rental:1",
        record_type=RecordType.RENTAL_FLAT,
        observed_at="2026-05-08T09:47:25+00:00",
        record_hash="sha256:rental",
        addresses=[
            {
                "country_code": "SG",
                "postal_code": "123456",
                "street_name": "Bedok Reservoir Road",
                "street_number": "123",
                "unit_number": "#01-01",
                "raw_address": "123 Bedok Reservoir Road #01-01 Singapore 123456",
            }
        ],
        raw_payload={"block": "123", "street": "Bedok Reservoir Road"},
    )


def _company_reference_envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="bitrix_chat",
        source_instance_id="bitrix-primary",
        source_record_id="bitrix-crm-company-303",
        record_type=RecordType.CRM_COMPANY,
        observed_at="2026-05-08T09:47:25+00:00",
        record_hash="sha256:company",
        attributes={"full_name": "Acme Corp"},
        raw_payload={
            "company_reference": {"type": "crm_company_id", "value": "303"},
            "reference_metadata": {
                "identity_policy_version": "crm_company_reference_v1",
                "source_instance_id": "bitrix-primary",
                "crm_company_id": "303",
                "person_matching_prohibited": True,
            },
        },
    )


def test_ingest_address_record_writes_assertions_under_v2_shadow_and_not_under_v1(
    monkeypatch: MonkeyPatch,
) -> None:
    recorded: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "src.pipeline_writes.write_assertions",
        lambda _tx, pk, normalized: recorded.append((pk, normalized)),
    )

    monkeypatch.setattr(
        "src.pipeline_writes.get_settings",
        lambda: MagicMock(identity_model="v1"),
    )
    tx_v1 = _ParkedTx()
    result_v1 = ingest_address_record(cast(object, _Client(tx_v1)), _address_envelope())
    assert result_v1.source_record_pk == "sr-parked"
    assert recorded == []

    monkeypatch.setattr(
        "src.pipeline_writes.get_settings",
        lambda: MagicMock(identity_model="v2_shadow"),
    )
    tx_shadow = _ParkedTx()
    result_shadow = ingest_address_record(cast(object, _Client(tx_shadow)), _address_envelope())
    assert result_shadow.source_record_pk == "sr-parked"
    assert len(recorded) == 1
    assert recorded[0][0] == "sr-parked"


def test_ingest_reference_record_writes_assertions_under_v2_shadow_and_not_under_v1(
    monkeypatch: MonkeyPatch,
) -> None:
    recorded: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "src.pipeline_writes.write_assertions",
        lambda _tx, pk, normalized: recorded.append((pk, normalized)),
    )

    monkeypatch.setattr(
        "src.pipeline_writes.get_settings",
        lambda: MagicMock(identity_model="v1"),
    )
    tx_v1 = _ParkedTx()
    result_v1 = ingest_reference_record(cast(object, _Client(tx_v1)), _company_reference_envelope())
    assert result_v1.source_record_pk == "sr-parked"
    assert recorded == []

    monkeypatch.setattr(
        "src.pipeline_writes.get_settings",
        lambda: MagicMock(identity_model="v2_shadow"),
    )
    tx_shadow = _ParkedTx()
    result_shadow = ingest_reference_record(
        cast(object, _Client(tx_shadow)), _company_reference_envelope()
    )
    assert result_shadow.source_record_pk == "sr-parked"
    assert len(recorded) == 1
    assert recorded[0][0] == "sr-parked"


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


def test_crm_deal_projected_identifiers_equality() -> None:
    deal = _deal_envelope()
    identifiers = normalize_envelope_identifiers(deal)
    direct = projected_identifiers(deal, identifiers)
    dummy_match = MatchResult(
        decision=MatchDecision.NO_MATCH,
        reasons=["test"],
    )
    plan = plan_crm_deal_identity(deal, identifiers, dummy_match)
    assert direct == list(plan.projected_identifiers)


class _FullOrderTx(_RecordingTx):
    """Answers graph calls for all pipeline branches so query ordering can be audited."""

    def __init__(
        self,
        *,
        candidates: list[dict[str, object]] | None = None,
    ) -> None:
        super().__init__()
        self._candidates = candidates or []

    def run(self, query: str, **kwargs: object) -> _Result | _SummaryCounterResult:
        counter = summary_counter_result(query)
        if counter is not None:
            return counter
        self._record(query, kwargs)
        if query == queries.LOCK_AND_GET_SOURCE_STATE:
            return _Result(dict(_EMPTY_LOCK_STATE))
        if query == FIND_PARKED_SOURCE_RECORD_BY_HASH:
            return _Result()
        if query in (
            queries.CREATE_SOURCE_RECORD,
            queries.ACTIVATE_FIRST_SOURCE_RECORD_VERSION,
            queries.ACTIVATE_SOURCE_RECORD_VERSION,
            PROMOTE_STAGED_SOURCE_RECORD,
        ):
            return _Result({"source_record_pk": "sr-staged"})
        if query == queries.CREATE_PERSON:
            return _Result({"person_id": "person-created"})
        if query == queries.CREATE_MATCH_DECISION:
            return _Result({"match_decision_id": "md-created"})
        if query == queries.CREATE_MERGE_EVENT_AUTO_MERGE:
            return _Result({"merge_event_id": "me-created"})
        if query == queries.CREATE_REVIEW_CASE:
            return _Result({"review_case_id": "rc-created"})
        if query == queries.FIND_CANDIDATES_BY_IDENTIFIERS_BATCH:
            return _Result(rows=self._candidates)
        return _Result()


def _identity_order_envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="fundbox",
        source_record_id="fb-101",
        record_type=RecordType.IDENTITY,
        observed_at="2026-05-08T09:47:25+00:00",
        record_hash="sha256:fb101",
        identifiers=[{"type": "phone", "value": "+6591234567", "is_verified": True}],
        attributes={"full_name": "Ada Lovelace"},
        raw_payload={"user_id": "101"},
    )


def test_persist_first_order_in_new_person_path() -> None:
    tx = _FullOrderTx()
    pipeline = IngestPipeline(cast(object, _Client(tx)))
    result = pipeline.ingest(_identity_order_envelope())

    assert result.parked is False
    assert result.is_new_person is True
    queries_issued = [q for q, _ in tx.calls]

    create_sr_idx = queries_issued.index(queries.CREATE_SOURCE_RECORD)
    find_cand_idx = queries_issued.index(queries.FIND_CANDIDATES_BY_IDENTIFIERS_BATCH)
    create_person_idx = queries_issued.index(queries.CREATE_PERSON)
    promote_idx = queries_issued.index(PROMOTE_STAGED_SOURCE_RECORD)
    create_decision_idx = queries_issued.index(queries.CREATE_MATCH_DECISION)

    assert create_sr_idx < find_cand_idx
    assert create_sr_idx < create_person_idx
    assert create_sr_idx < create_decision_idx
    assert promote_idx < create_decision_idx

    promote_calls = [params for q, params in tx.calls if q == PROMOTE_STAGED_SOURCE_RECORD]
    assert len(promote_calls) == 1
    assert promote_calls[0]["link_status"] == "linked"


def test_promotion_carries_the_settling_run_so_reused_rows_join_its_lineage() -> None:
    tx = _FullOrderTx()
    pipeline = IngestPipeline(cast(object, _Client(tx)))
    pipeline.ingest(_identity_order_envelope(), ingest_run_id="run-2")

    [promote] = [params for q, params in tx.calls if q == PROMOTE_STAGED_SOURCE_RECORD]
    assert promote["ingest_run_id"] == "run-2"
    assert promote["control_instance_id"]
    # Idempotent: a row staged in this same run keeps exactly one PART_OF_RUN edge.
    assert "MERGE (sr)-[:PART_OF_RUN]->(run)" in PROMOTE_STAGED_SOURCE_RECORD


def test_persist_first_order_in_matched_person_path() -> None:
    candidates = [
        {
            "input_index": 0,
            "identifier_type": "phone",
            "normalized_value": "+6591234567",
            "fanout": 1,
            "person_ids": ["person-matched"],
        }
    ]
    tx = _FullOrderTx(candidates=candidates)
    pipeline = IngestPipeline(cast(object, _Client(tx)))
    pipeline._match_engine = MagicMock(
        evaluate=MagicMock(
            return_value=MatchResult(
                decision=MatchDecision.MERGE,
                confidence=1.0,
                matched_person_id="person-matched",
            )
        )
    )
    result = pipeline.ingest(_identity_order_envelope())

    assert result.parked is False
    assert result.person_id == "person-matched"
    queries_issued = [q for q, _ in tx.calls]

    create_sr_idx = queries_issued.index(queries.CREATE_SOURCE_RECORD)
    find_cand_idx = queries_issued.index(queries.FIND_CANDIDATES_BY_IDENTIFIERS_BATCH)
    promote_idx = queries_issued.index(PROMOTE_STAGED_SOURCE_RECORD)
    create_decision_idx = queries_issued.index(queries.CREATE_MATCH_DECISION)

    assert create_sr_idx < find_cand_idx
    assert create_sr_idx < create_decision_idx
    assert promote_idx < create_decision_idx
    assert queries.CREATE_PERSON not in queries_issued

    promote_calls = [params for q, params in tx.calls if q == PROMOTE_STAGED_SOURCE_RECORD]
    assert len(promote_calls) == 1
    assert promote_calls[0]["link_status"] == "linked"


def test_persist_first_order_in_review_path() -> None:
    candidates = [
        {
            "input_index": 0,
            "identifier_type": "phone",
            "normalized_value": "+6591234567",
            "fanout": 1,
            "person_ids": ["person-review"],
        }
    ]
    tx = _FullOrderTx(candidates=candidates)
    pipeline = IngestPipeline(cast(object, _Client(tx)))
    pipeline._match_engine = MagicMock(
        evaluate=MagicMock(
            return_value=MatchResult(
                decision=MatchDecision.REVIEW,
                confidence=0.7,
                matched_person_id="person-review",
            )
        )
    )
    result = pipeline.ingest(_identity_order_envelope())

    assert result.parked is False
    assert result.match_decision is MatchDecision.REVIEW
    queries_issued = [q for q, _ in tx.calls]

    create_sr_idx = queries_issued.index(queries.CREATE_SOURCE_RECORD)
    find_cand_idx = queries_issued.index(queries.FIND_CANDIDATES_BY_IDENTIFIERS_BATCH)
    promote_idx = queries_issued.index(PROMOTE_STAGED_SOURCE_RECORD)
    create_decision_idx = queries_issued.index(queries.CREATE_MATCH_DECISION)

    assert create_sr_idx < find_cand_idx
    assert create_sr_idx < create_decision_idx
    assert promote_idx < create_decision_idx

    promote_calls = [params for q, params in tx.calls if q == PROMOTE_STAGED_SOURCE_RECORD]
    assert len(promote_calls) == 1
    assert promote_calls[0]["link_status"] == "pending_review"


def test_persist_first_order_in_quarantine_path(monkeypatch: MonkeyPatch) -> None:
    quarantine = MatchResult(
        decision=MatchDecision.REVIEW,
        confidence=1.0,
        reasons=["canonical_crm_contact_owner_blocked_by_no_match_lock"],
        feature_snapshot={
            "crm_deal_quarantine": True,
            "blocked_canonical_crm_contact_candidate_ids": ["person-a"],
        },
    )
    monkeypatch.setattr("src.pipeline.resolve_canonical_crm_contact", lambda *_a, **_kw: quarantine)
    tx = _FullOrderTx()
    pipeline = IngestPipeline(cast(object, _Client(tx)))
    result = pipeline.ingest(_deal_envelope())

    assert result.parked is False
    assert result.person_id is None
    queries_issued = [q for q, _ in tx.calls]

    create_sr_idx = queries_issued.index(queries.CREATE_SOURCE_RECORD)
    promote_idx = queries_issued.index(PROMOTE_STAGED_SOURCE_RECORD)
    create_decision_idx = queries_issued.index(queries.CREATE_MATCH_DECISION)

    assert create_sr_idx < promote_idx
    assert create_sr_idx < create_decision_idx
    assert promote_idx < create_decision_idx

    promote_calls = [params for q, params in tx.calls if q == PROMOTE_STAGED_SOURCE_RECORD]
    assert len(promote_calls) == 1
    assert promote_calls[0]["link_status"] == "blocked"


def test_persist_first_order_in_parked_path() -> None:
    tx = _FullOrderTx()
    pipeline = IngestPipeline(cast(object, _Client(tx)))
    result = pipeline.ingest(_bankruptcy_envelope())

    assert result.parked is True
    queries_issued = [q for q, _ in tx.calls]

    assert queries.CREATE_SOURCE_RECORD in queries_issued
    assert queries.FIND_CANDIDATES_BY_IDENTIFIERS_BATCH in queries_issued
    assert queries.CREATE_PERSON not in queries_issued
    assert queries.CREATE_MATCH_DECISION not in queries_issued
    assert PROMOTE_STAGED_SOURCE_RECORD not in queries_issued

    create_sr_idx = queries_issued.index(queries.CREATE_SOURCE_RECORD)
    find_cand_idx = queries_issued.index(queries.FIND_CANDIDATES_BY_IDENTIFIERS_BATCH)
    assert create_sr_idx < find_cand_idx
