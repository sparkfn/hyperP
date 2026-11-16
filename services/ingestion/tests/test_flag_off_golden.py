"""The identity-model flag must not change any v1 write.

Every record path is executed twice — ``v1`` and ``v2_shadow`` — against a
recording transaction. The only permitted difference is the shadow assertion
writer, which is stubbed here so the recorded Cypher stream stays comparable.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import cast
from unittest.mock import MagicMock

import pytest
from _txmock import _RecordingTx, _SummaryCounterResult, summary_counter_result
from neo4j import ManagedTransaction
from pytest import MonkeyPatch
from src.config import IdentityModel
from src.graph import queries
from src.models import (
    CandidateResult,
    MatchDecision,
    MatchResult,
    RecordType,
    SourceRecordEnvelope,
)
from src.pipeline import IngestPipeline

#: ``create_review_case_if_needed`` stamps the SLA deadline from the wall clock.
_CLOCK_PARAM_KEYS = frozenset({"sla_due_at"})


class _Result:
    def __init__(self, row: dict[str, object] | None = None) -> None:
        self._row = row

    def single(self) -> dict[str, object] | None:
        return self._row

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(())

    def consume(self) -> None:
        return None


class _StreamTx(_RecordingTx):
    """Records every query/param pair and answers only the asserting writes."""

    _ROWS: dict[str, dict[str, object]] = {
        queries.CREATE_SOURCE_RECORD: {"source_record_pk": "sr-new"},
        queries.PROMOTE_STAGED_SOURCE_RECORD: {"source_record_pk": "sr-new"},
        queries.CREATE_PERSON: {"person_id": "person-created"},
        queries.CREATE_MATCH_DECISION: {"match_decision_id": "md-1"},
        queries.CREATE_REVIEW_CASE: {"review_case_id": "rc-1"},
        queries.CREATE_MERGE_EVENT_AUTO_MERGE: {"merge_event_id": "me-1"},
        queries.ACTIVATE_FIRST_SOURCE_RECORD_VERSION: {"source_record_pk": "sr-new"},
        queries.ACTIVATE_SOURCE_RECORD_VERSION: {"source_record_pk": "sr-new"},
    }

    def run(self, query: str, **kwargs: object) -> _Result | _SummaryCounterResult:
        counter = summary_counter_result(query)
        if counter is not None:
            return counter
        self._record(query, kwargs)
        return _Result(self._ROWS.get(query))


@dataclass(frozen=True)
class _Scenario:
    name: str
    envelope: SourceRecordEnvelope
    match_result: MatchResult
    candidates: tuple[CandidateResult, ...] = ()


def _identity_envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="fundbox",
        source_record_id="fundbox-user-1",
        record_type=RecordType.IDENTITY,
        observed_at="2026-05-08T09:47:25+00:00",
        record_hash="sha256:identity",
        identifiers=[{"type": "phone", "value": "+6591234567", "is_verified": True}],
        attributes={"full_name": "Ada Lovelace"},
        raw_payload={"user_id": "1"},
    )


def _deal_envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="bitrix_chat",
        source_record_id="bitrix-crm-deal-501",
        record_type=RecordType.CRM_DEAL,
        observed_at="2026-05-08T09:47:25+00:00",
        record_hash="sha256:deal",
        raw_payload={"category_id": "1"},
    )


_REVIEW_NO_ENGINE_PICK = _Scenario(
    name="review_no_engine_pick",
    envelope=_identity_envelope(),
    match_result=MatchResult(
        decision=MatchDecision.REVIEW,
        confidence=0.7,
        reasons=["ambiguous_candidate"],
    ),
    candidates=(CandidateResult(person_id="person-a", source="identifier"),),
)

_SCENARIOS: tuple[_Scenario, ...] = (
    _Scenario(
        name="matched_merge",
        envelope=_identity_envelope(),
        match_result=MatchResult(
            decision=MatchDecision.MERGE,
            confidence=1.0,
            matched_person_id="person-a",
            reasons=["exact_phone_match"],
        ),
        candidates=(CandidateResult(person_id="person-a", source="identifier"),),
    ),
    _Scenario(
        name="review",
        envelope=_identity_envelope(),
        match_result=MatchResult(
            decision=MatchDecision.REVIEW,
            confidence=0.7,
            matched_person_id="person-a",
            reasons=["partial_name_match"],
        ),
        candidates=(CandidateResult(person_id="person-a", source="identifier"),),
    ),
    _Scenario(
        name="new_person",
        envelope=_identity_envelope(),
        match_result=MatchResult(
            decision=MatchDecision.MERGE,
            confidence=1.0,
            reasons=["no_candidates"],
        ),
    ),
    _Scenario(
        name="quarantine",
        envelope=_deal_envelope(),
        match_result=MatchResult(
            decision=MatchDecision.REVIEW,
            confidence=1.0,
            reasons=["crm_deal_quarantine"],
            feature_snapshot={"crm_deal_quarantine": True},
        ),
    ),
    _REVIEW_NO_ENGINE_PICK,
)


def _stream(tx: _RecordingTx) -> list[tuple[str, dict[str, object]]]:
    return [
        (query, {key: value for key, value in params.items() if key not in _CLOCK_PARAM_KEYS})
        for query, params in tx.calls
    ]


def _run(
    scenario: _Scenario,
    identity_model: IdentityModel,
    monkeypatch: MonkeyPatch,
) -> tuple[list[tuple[str, dict[str, object]]], list[tuple[str, dict[str, object]]]]:
    tx = _StreamTx()
    pipeline = IngestPipeline(cast(object, MagicMock()), identity_model=identity_model)
    pipeline._match_engine = MagicMock(evaluate=MagicMock(return_value=scenario.match_result))
    monkeypatch.setattr(
        "src.pipeline.find_candidates",
        lambda *_args, **_kwargs: list(scenario.candidates),
    )
    monkeypatch.setattr(
        "src.pipeline.find_identity_candidates",
        lambda *_args, **_kwargs: (list(scenario.candidates), []),
    )
    assertions: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "src.pipeline_writes.write_assertions",
        lambda _tx, pk, normalized: assertions.append((pk, normalized)),
    )
    envelope = scenario.envelope.model_copy(update={"source_record_version": "1"})

    pipeline._execute_ingest(cast(ManagedTransaction, tx), envelope, [], [], [])

    return _stream(tx), assertions


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=[scenario.name for scenario in _SCENARIOS])
def test_shadow_flag_only_adds_assertion_writes(
    scenario: _Scenario,
    monkeypatch: MonkeyPatch,
) -> None:
    v1_stream, v1_assertions = _run(scenario, "v1", monkeypatch)
    shadow_stream, shadow_assertions = _run(scenario, "v2_shadow", monkeypatch)

    assert v1_assertions == []
    assert len(shadow_assertions) == 1
    assert shadow_assertions[0][0] == "sr-new"
    v1_queries = [query for query, _ in v1_stream]
    assert queries.CREATE_SOURCE_RECORD in v1_queries
    assert queries.PROMOTE_STAGED_SOURCE_RECORD in v1_queries
    # Shadow mode may add only the C3 projection reads; the legacy stream is
    # otherwise identical and the legacy outcome is preserved.
    projection_queries = {
        queries.SOURCE_RECORD_LINKED_TO_PERSON,
        queries.FETCH_RECORD_IDENTIFIER_KEYS,
    }
    assert [call for call in shadow_stream if call[0] not in projection_queries] == v1_stream


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=[scenario.name for scenario in _SCENARIOS])
def test_v2_never_uses_the_legacy_person_fallback(
    scenario: _Scenario,
    monkeypatch: MonkeyPatch,
) -> None:
    v2_stream, _assertions = _run(scenario, "v2", monkeypatch)

    v2_queries = [query for query, _ in v2_stream]
    assert queries.CREATE_PERSON not in v2_queries
    assert queries.MERGE_PERSON_BY_NRIC not in v2_queries


def test_v2_review_with_candidate_links_provisionally_and_opens_a_review_case(
    monkeypatch: MonkeyPatch,
) -> None:
    stream, _assertions = _run(_REVIEW_NO_ENGINE_PICK, "v2", monkeypatch)

    issued = [query for query, _ in stream]
    assert queries.CREATE_MATCH_DECISION in issued
    assert queries.CREATE_REVIEW_CASE in issued
    assert queries.PROMOTE_STAGED_SOURCE_RECORD in issued
    # No evidence rows were answered by the fake, so the provisional record's
    # identifiers cannot reach HAS_IDENTIFIER in this REVIEW outcome.
    assert queries.MERGE_PARTICIPANT_HAS_IDENTIFIER_LINKS not in issued


def test_v2_parked_outcome_persists_match_decision_without_review_case(
    monkeypatch: MonkeyPatch,
) -> None:
    parked = next(scenario for scenario in _SCENARIOS if scenario.name == "new_person")
    stream, _assertions = _run(parked, "v2", monkeypatch)

    issued = [query for query, _ in stream]
    assert queries.CREATE_MATCH_DECISION in issued
    assert queries.CREATE_REVIEW_CASE not in issued
    assert queries.PROMOTE_STAGED_SOURCE_RECORD not in issued
