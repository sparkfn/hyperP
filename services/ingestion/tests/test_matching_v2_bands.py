"""Unit coverage for v2 bands, the margin gate and promotion checks (plan §2.4)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast
from unittest.mock import MagicMock

import pytest
from _txmock import summary_counter_result
from neo4j import ManagedTransaction
from src.graph import queries
from src.identity_links import MaterializationOutcome
from src.matching.engine import MatchEngine
from src.matching.heuristic import evaluate_heuristic
from src.matching.snapshot import CandidateSnapshot
from src.matching.thresholds import (
    REASSIGNMENT_AUTO_THRESHOLD,
    REASSIGNMENT_MIN_MARGIN,
    V2_ATTACH_MARGIN,
)
from src.models import (
    CandidateResult,
    EngineType,
    MatchAction,
    MatchDecision,
    MatchResult,
    NormalizedAttribute,
    NormalizedIdentifier,
    QualityFlag,
    RecordType,
    SourceRecordEnvelope,
)
from src.pipeline import IngestPipeline
from src.pipeline_writes import (
    create_review_case_if_needed,
    persist_nric_contradiction_review,
    reset_review_counters,
)
from src.review_taxonomy import case_key


class _StrictTx:
    """Fails any Cypher query; weighted scoring with a snapshot issues none."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def run(self, query: str, **params: object) -> list[dict[str, object]]:
        self.queries.append(query)
        raise AssertionError(f"unexpected query: {query}")


def _phone(value: str, *, verified: bool = True) -> NormalizedIdentifier:
    return NormalizedIdentifier(
        identifier_type="phone",
        normalized_value=value,
        is_verified=verified,
        quality_flag=QualityFlag.VALID,
    )


def _email(value: str, *, verified: bool = True) -> NormalizedIdentifier:
    return NormalizedIdentifier(
        identifier_type="email",
        normalized_value=value,
        is_verified=verified,
        quality_flag=QualityFlag.VALID,
    )


def _row(
    identifier_type: str,
    value: str,
    *,
    verified: bool = True,
    link_weight: float = 1.0,
    participant_count: int = 1,
    system_sourced: bool = True,
) -> dict[str, object]:
    return {
        "identifier_type": identifier_type,
        "normalized_value": value,
        "is_verified": verified,
        "last_confirmed_at": "2026-01-01T00:00:00Z",
        "is_system_sourced": system_sourced,
        "link_weight": link_weight,
        "participant_count": participant_count,
    }


def _snapshot(
    idents: list[dict[str, object]],
    facts: list[dict[str, object]] | None = None,
    *,
    anchored: bool = False,
) -> CandidateSnapshot:
    return CandidateSnapshot(
        idents=idents, facts=facts if facts is not None else [], addrs=[], anchored=anchored
    )


def _attach(person_id: str, confidence: float, *, anchored: bool = False) -> MatchResult:
    return MatchResult(
        decision=MatchDecision.MERGE,
        confidence=confidence,
        reasons=[f"attach {person_id}"],
        engine_type=EngineType.HEURISTIC,
        matched_person_id=person_id,
        action=MatchAction.ATTACH,
        policy_version="c4-v1",
        feature_snapshot={"anchored": anchored},
    )


def _engine_with(
    monkeypatch: pytest.MonkeyPatch, canned: dict[str, MatchResult | None]
) -> MatchEngine:
    engine = MatchEngine()
    monkeypatch.setattr(
        "src.matching.engine.evaluate_deterministic", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr("src.matching.engine.prefetch_no_match_lock_owners", lambda *_a, **_k: {})
    monkeypatch.setattr(
        "src.matching.engine.evaluate_heuristic",
        lambda _tx, person_id, *_args, **_kwargs: canned[person_id],
    )
    return engine


def test_unverified_phone_only_parks_without_case() -> None:
    snapshot = _snapshot([_row("phone", "+6591234567", verified=False, link_weight=0.6)])
    result = evaluate_heuristic(
        cast(ManagedTransaction, _StrictTx()),
        "person-1",
        [_phone("+6591234567", verified=False)],
        None,
        [],
        RecordType.IDENTITY,
        snapshot=snapshot,
        weighted=True,
    )

    assert result.decision is MatchDecision.NO_MATCH
    assert result.action is MatchAction.PARK
    assert round(result.confidence, 6) == 0.12
    assert (
        create_review_case_if_needed(
            cast(ManagedTransaction, _StrictTx()), result, "md-1", exception_only=True
        )
        is None
    )


def test_margin_gate_anchored_ambiguity_and_stable_case_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine_with(
        monkeypatch,
        {
            "person-a": _attach("person-a", 0.79, anchored=True),
            "person-b": _attach("person-b", 0.79, anchored=True),
        },
    )
    result = engine.evaluate(
        cast(ManagedTransaction, _StrictTx()),
        [CandidateResult(person_id="person-a"), CandidateResult(person_id="person-b")],
        [],
        None,
        [],
        v2_evidence=True,
    )

    assert result.decision is MatchDecision.REVIEW
    assert result.review_reason == "anchored_ambiguity"
    assert result.review_candidate_person_ids == ["person-a", "person-b"]
    assert result.matched_person_id == "person-a"
    assert case_key("anchored_ambiguity", "record-1", ["person-b", "person-a"]) == case_key(
        "anchored_ambiguity", "record-2", ["person-a", "person-b"]
    )


def test_attach_margin_is_ten_points() -> None:
    assert V2_ATTACH_MARGIN == 0.10


def test_margin_below_threshold_parks_without_merge_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine_with(
        monkeypatch,
        {"person-a": _attach("person-a", 0.79), "person-b": _attach("person-b", 0.75)},
    )

    def _boom(merges: list[MatchResult]) -> MatchResult:
        raise AssertionError(f"_resolve_merges must not run: {merges}")

    monkeypatch.setattr(MatchEngine, "_resolve_merges", _boom)
    result = engine.evaluate(
        cast(ManagedTransaction, _StrictTx()),
        [CandidateResult(person_id="person-a"), CandidateResult(person_id="person-b")],
        [],
        None,
        [],
        v2_evidence=True,
    )

    assert result.decision is MatchDecision.NO_MATCH
    assert result.action is MatchAction.PARK


def test_margin_ok_links_primary_and_keeps_other_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine_with(
        monkeypatch,
        {"person-a": _attach("person-a", 0.85), "person-b": _attach("person-b", 0.70)},
    )
    result = engine.evaluate(
        cast(ManagedTransaction, _StrictTx()),
        [CandidateResult(person_id="person-a"), CandidateResult(person_id="person-b")],
        [],
        None,
        [],
        v2_evidence=True,
    )

    assert result.decision is MatchDecision.MERGE
    assert result.matched_person_id == "person-a"
    assert result.additional_linked_person_ids == ["person-b"]


def test_veto_blocks_attach_on_identity_record() -> None:
    snapshot = _snapshot(
        [
            _row("phone", "+6591234567"),
            _row("email", "user@example.com"),
        ],
        [
            {"attribute_name": "full_name", "attribute_value": "Ada Lovelace"},
            {"attribute_name": "dob", "attribute_value": "1990-01-01"},
        ],
    )
    attributes = [
        NormalizedAttribute(attribute_name="full_name", attribute_value="Ada Lovelace"),
        NormalizedAttribute(attribute_name="dob", attribute_value="2000-01-01"),
    ]
    result = evaluate_heuristic(
        cast(ManagedTransaction, _StrictTx()),
        "person-1",
        [_phone("+6591234567"), _email("user@example.com")],
        None,
        attributes,
        RecordType.IDENTITY,
        snapshot=snapshot,
        weighted=True,
    )

    assert result.decision is MatchDecision.NO_MATCH
    assert result.action is MatchAction.PARK
    assert (
        create_review_case_if_needed(
            cast(ManagedTransaction, _StrictTx()), result, "md-1", exception_only=True
        )
        is None
    )


def _conversation_fixtures() -> tuple[
    list[NormalizedIdentifier], CandidateSnapshot, list[NormalizedAttribute]
]:
    identifiers = [_phone("+6591234567"), _email("user@example.com")]
    snapshot = _snapshot(
        [
            _row("phone", "+6591234567"),
            _row("email", "user@example.com"),
        ],
        [{"attribute_name": "full_name", "attribute_value": "Ada Lovelace"}],
    )
    attributes = [NormalizedAttribute(attribute_name="full_name", attribute_value="Ada Lovelace")]
    return identifiers, snapshot, attributes


def test_conversation_promotion_attaches_with_two_families() -> None:
    identifiers, snapshot, attributes = _conversation_fixtures()
    result = evaluate_heuristic(
        cast(ManagedTransaction, _StrictTx()),
        "person-1",
        identifiers,
        None,
        attributes,
        RecordType.CONVERSATION,
        snapshot=snapshot,
        weighted=True,
    )

    assert result.confidence == 0.91
    assert result.decision is MatchDecision.MERGE
    assert result.action is MatchAction.ATTACH


def test_conversation_promotion_vetoed_by_dob_conflict() -> None:
    identifiers, snapshot, attributes = _conversation_fixtures()
    snapshot.facts.append({"attribute_name": "dob", "attribute_value": "1990-01-01"})
    attributes = [
        *attributes,
        NormalizedAttribute(attribute_name="dob", attribute_value="2000-01-01"),
    ]
    result = evaluate_heuristic(
        cast(ManagedTransaction, _StrictTx()),
        "person-1",
        identifiers,
        None,
        attributes,
        RecordType.CONVERSATION,
        snapshot=snapshot,
        weighted=True,
    )

    assert result.decision is MatchDecision.NO_MATCH
    assert result.action is MatchAction.PARK
    assert (
        create_review_case_if_needed(
            cast(ManagedTransaction, _StrictTx()), result, "md-1", exception_only=True
        )
        is None
    )


def test_hysteresis_margins_unchanged() -> None:
    assert REASSIGNMENT_AUTO_THRESHOLD == 0.90
    assert REASSIGNMENT_MIN_MARGIN == 0.10


class _PipeTx:
    """Recording fake answering the pipeline's persisted-write queries."""

    _ROWS: dict[str, dict[str, object]] = {
        queries.CREATE_SOURCE_RECORD: {"source_record_pk": "sr-new"},
        queries.PROMOTE_STAGED_SOURCE_RECORD: {"source_record_pk": "sr-new"},
        queries.CREATE_MATCH_DECISION: {"match_decision_id": "md-1"},
    }

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **kwargs: object) -> object:
        counter = summary_counter_result(query)
        if counter is not None:
            return counter
        self.calls.append((query, dict(kwargs)))
        row = self._ROWS.get(query)
        return _Single(row)


class _Single:
    """Fake result supporting the driver's read shapes (single/iterate/consume)."""

    def __init__(self, row: dict[str, object] | None = None) -> None:
        self._row = row

    def single(self) -> dict[str, object] | None:
        return self._row

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(())

    def consume(self) -> None:
        return None


def _envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="fundbox",
        source_record_id="fundbox-user-1",
        record_type=RecordType.IDENTITY,
        observed_at="2026-05-08T09:47:25+00:00",
        record_hash="sha256:identity",
        identifiers=[{"type": "phone", "value": "+6591234567", "is_verified": True}],
        raw_payload={"user_id": "1"},
    )


def _run_v2_no_match(
    monkeypatch: pytest.MonkeyPatch, match_result: MatchResult
) -> tuple[_PipeTx, int]:
    tx = _PipeTx()
    pipeline = IngestPipeline(cast(object, MagicMock()), identity_model="v2")
    pipeline._match_engine = MagicMock(evaluate=MagicMock(return_value=match_result))
    monkeypatch.setattr("src.pipeline.find_identity_candidates", lambda *_args, **_kwargs: ([], []))
    materialize_calls: list[str] = []
    monkeypatch.setattr(
        "src.pipeline.materialize_profile_for_record",
        lambda _tx, pk, **_kwargs: (
            materialize_calls.append(pk) or MaterializationOutcome(status="parked", profile_id=None)
        ),
    )
    envelope = _envelope().model_copy(update={"source_record_version": "1"})
    pipeline._execute_ingest(
        cast(ManagedTransaction, tx),
        envelope,
        [_phone("+6591234567")],
        [],
        [],
    )
    return tx, len(materialize_calls)


def _non_attach_result(action: MatchAction, confidence: float) -> MatchResult:
    return MatchResult(
        decision=MatchDecision.NO_MATCH,
        confidence=confidence,
        reasons=["weighted v2 band"],
        engine_type=EngineType.HEURISTIC,
        action=action,
        policy_version="c4-v1",
    )


@pytest.mark.parametrize(
    ("action", "confidence"), [(MatchAction.UNION, 0.6), (MatchAction.PARK, 0.3)]
)
def test_union_and_park_materialize_exactly_once(
    monkeypatch: pytest.MonkeyPatch, action: MatchAction, confidence: float
) -> None:
    tx, materialize_count = _run_v2_no_match(monkeypatch, _non_attach_result(action, confidence))

    assert materialize_count == 1
    issued = [query for query, _ in tx.calls]
    assert queries.CREATE_REVIEW_CASE not in issued
    assert queries.CREATE_REVIEW_CASE_BY_KEY not in issued


def test_nric_contradiction_rejection_opens_one_keyed_case() -> None:
    class _ReviewTx:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def run(self, query: str, **params: object) -> _Single:
            self.calls.append((query, dict(params)))
            if query == queries.CREATE_MATCH_DECISION:
                return _Single({"match_decision_id": "md-1"})
            if query == queries.CREATE_REVIEW_CASE_BY_KEY:
                return _Single({"review_case_id": "rc-1"})
            return _Single(None)

    reset_review_counters()
    tx = _ReviewTx()
    review_case_id = persist_nric_contradiction_review(
        cast(ManagedTransaction, tx),
        source_record_pk="sr-incoming",
        profile_id="profile-1",
        holder_person_id="person-holder",
        source_system="onediver",
    )

    assert review_case_id == "rc-1"
    by_key = [params for query, params in tx.calls if query == queries.CREATE_REVIEW_CASE_BY_KEY]
    assert len(by_key) == 1
    assert by_key[0]["case_key"] == case_key(
        "nric_contradiction", None, ["profile-1", "person-holder"]
    )
    assert by_key[0]["reason_code"] == "nric_contradiction"
