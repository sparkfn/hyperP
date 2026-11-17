"""Unit coverage for the v2 exception-only review taxonomy (plan §2.5)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from neo4j import ManagedTransaction
from src.graph import queries
from src.models import EngineType, MatchAction, MatchDecision, MatchResult
from src.pipeline_writes import (
    REVIEW_CASES_CREATED_COUNTER,
    REVIEW_DECISIONS_SUPPRESSED_COUNTER,
    create_review_case_if_needed,
    reset_review_counters,
)
from src.review_taxonomy import case_key, exception_reason


def _result(
    *,
    decision: MatchDecision = MatchDecision.REVIEW,
    review_reason: str | None = None,
    reasons: list[str] | None = None,
    candidates: list[str] | None = None,
    matched: str | None = None,
) -> MatchResult:
    return MatchResult(
        decision=decision,
        confidence=0.7,
        reasons=reasons if reasons is not None else [],
        engine_type=EngineType.HEURISTIC,
        matched_person_id=matched,
        review_candidate_person_ids=candidates if candidates is not None else [],
        review_reason=review_reason,
        action=MatchAction.REVIEW if decision is MatchDecision.REVIEW else MatchAction.PARK,
        policy_version="c4-v1",
    )


def test_exception_reason_prefers_the_explicit_reason() -> None:
    result = _result(review_reason="anchored_ambiguity", candidates=["person-a"])
    assert exception_reason(result) == "anchored_ambiguity"
    nric = _result(review_reason="nric_contradiction")
    assert exception_reason(nric) == "nric_contradiction"


def test_exception_reason_maps_classification_through_the_taxonomy() -> None:
    crm = _result(reasons=["ambiguous CRM deal contacts"], candidates=["person-a", "person-b"])
    assert exception_reason(crm) == "crm_ambiguity"
    continuity = _result(
        reasons=["Changed record requires review before reassigning its prior person"],
        matched="person-a",
    )
    assert exception_reason(continuity) == "continuity_reassignment"


def test_exception_reason_suppresses_non_taxonomy_reviews() -> None:
    weak = _result(reasons=["Phone match (unverified: +0.20)"])
    assert exception_reason(weak) is None
    similar = _result(reasons=["High name similarity (0.85: +0.20)"])
    assert exception_reason(similar) is None
    other = _result(reasons=["something unrecognized"])
    assert exception_reason(other) is None


def test_case_key_is_deterministic_and_dedupes_across_records() -> None:
    first = case_key("anchored_ambiguity", "record-1", ["person-b", "person-a"])
    second = case_key("anchored_ambiguity", "record-2", ["person-a", "person-b"])
    assert first == second
    assert len(first) == 40
    assert case_key("anchored_ambiguity", "record-1", ["person-a", "person-c"]) != first


def test_case_key_shapes_per_reason() -> None:
    nric = case_key("nric_contradiction", None, ["profile-1", "person-holder"])
    assert nric == case_key("nric_contradiction", "ignored", ["profile-1", "person-holder"])
    crm_one = case_key("crm_ambiguity", "deal-1", ["person-a"])
    crm_two = case_key("crm_ambiguity", "deal-2", ["person-a"])
    assert crm_one != crm_two
    assert case_key("crm_ambiguity", "deal-1", ["person-a"]) == crm_one


class _KeyTx:
    """Answers the keyed review-case write with a canned case id."""

    def __init__(self, review_case_id: str = "rc-1") -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._review_case_id = review_case_id

    def run(self, query: str, **params: object) -> _Row:
        self.calls.append((query, dict(params)))
        if query == queries.CREATE_REVIEW_CASE_BY_KEY:
            return _Row({"review_case_id": self._review_case_id})
        if query == queries.SUPPRESS_MATCH_DECISION_REVIEW:
            return _Row({"match_decision_id": params.get("match_decision_id")})
        return _Row(None)


class _Row:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def single(self) -> dict[str, object] | None:
        return self._row


def test_exception_path_opens_keyed_case_with_telemetry() -> None:
    reset_review_counters()
    tx = _KeyTx()
    result = _result(
        review_reason="anchored_ambiguity", candidates=["person-a", "person-b"], matched="person-a"
    )
    review_case_id = create_review_case_if_needed(
        cast(ManagedTransaction, tx),
        result,
        "md-1",
        source_system="onediver",
        exception_only=True,
        record_identity="record-1",
    )

    assert review_case_id == "rc-1"
    by_key = [params for query, params in tx.calls if query == queries.CREATE_REVIEW_CASE_BY_KEY]
    assert len(by_key) == 1
    assert by_key[0]["reason_code"] == "anchored_ambiguity"
    assert by_key[0]["case_key"] == case_key(
        "anchored_ambiguity", "record-1", ["person-a", "person-b"]
    )
    day_utc = datetime.now(UTC).date().isoformat()
    assert (
        REVIEW_CASES_CREATED_COUNTER[("heuristic", "anchored_ambiguity", "onediver", day_utc)] == 1
    )


def test_non_exception_review_suppresses_as_not_exception() -> None:
    reset_review_counters()
    tx = _KeyTx()
    result = _result(reasons=["Phone match (unverified: +0.20)"])
    review_case_id = create_review_case_if_needed(
        cast(ManagedTransaction, tx),
        result,
        "md-1",
        source_system="onediver",
        exception_only=True,
    )

    assert review_case_id is None
    suppressed = [
        params for query, params in tx.calls if query == queries.SUPPRESS_MATCH_DECISION_REVIEW
    ]
    assert len(suppressed) == 1
    assert suppressed[0]["suppressed_reason"] == "not_exception"
    assert queries.CREATE_REVIEW_CASE_BY_KEY not in [query for query, _ in tx.calls]
    day_utc = datetime.now(UTC).date().isoformat()
    assert REVIEW_DECISIONS_SUPPRESSED_COUNTER[("not_exception", "onediver", day_utc)] == 1
    assert REVIEW_CASES_CREATED_COUNTER == {}


def test_non_review_decisions_open_no_case() -> None:
    tx = _KeyTx()
    parked = _result(decision=MatchDecision.NO_MATCH)
    assert (
        create_review_case_if_needed(
            cast(ManagedTransaction, tx), parked, "md-1", exception_only=True
        )
        is None
    )
    assert tx.calls == []
