from __future__ import annotations

from unittest.mock import patch

import pytest
from src.matching.engine import MatchEngine, ambiguous_prior_owners_result
from src.models import CandidateResult, EngineType, MatchDecision, MatchResult


def _merge(person_id: str, confidence: float) -> MatchResult:
    return MatchResult(
        decision=MatchDecision.MERGE,
        confidence=confidence,
        engine_type=EngineType.HEURISTIC,
        matched_person_id=person_id,
    )


@pytest.mark.parametrize(
    ("continuity_score", "expected"),
    [(0.81, MatchDecision.REVIEW), (0.80, MatchDecision.MERGE), (0.90, MatchDecision.REVIEW)],
)
def test_reassignment_requires_inclusive_tenth_margin(
    continuity_score: float, expected: MatchDecision
) -> None:
    engine = MatchEngine()
    scores = {"prior": _merge("prior", continuity_score), "new": _merge("new", 0.90)}

    with patch.object(engine, "_evaluate_one", side_effect=lambda _tx, pid, *_args: scores[pid]):
        result = engine.evaluate(
            object(),  # type: ignore[arg-type]
            [CandidateResult(person_id="new")],
            [],
            None,
            [],
            continuity_person_id="prior",
        )

    assert result.decision is expected
    assert result.matched_person_id == "new" if expected is MatchDecision.MERGE else "prior"


def test_sensitive_id_conflict_with_continuity_blocks_reassignment() -> None:
    engine = MatchEngine()
    scores = {"new": _merge("new", 1.0), "prior": None}

    with patch.object(engine, "_evaluate_one", side_effect=lambda _tx, pid, *_args: scores[pid]):
        result = engine.evaluate(
            object(),  # type: ignore[arg-type]
            [CandidateResult(person_id="new")],
            [],
            None,
            [],
            continuity_person_id="prior",
        )

    assert result.decision is MatchDecision.REVIEW
    assert result.matched_person_id == "prior"


def test_multiple_prior_owners_force_explainable_review() -> None:
    result = ambiguous_prior_owners_result(("person-b", "person-a"))

    assert result.decision is MatchDecision.REVIEW
    assert result.matched_person_id == "person-a"
    assert "ambiguous_prior_owners" in result.reasons


def test_reassignment_review_names_proposed_destination() -> None:
    result = MatchEngine._apply_reassignment_policy(_merge("new", 0.90), "prior", None)

    assert result.matched_person_id == "prior"
    assert result.proposed_person_id == "new"
    assert result.feature_snapshot["continuity_person_id"] == "prior"


def test_continuity_with_no_alternative_forces_review_not_new_person() -> None:
    engine = MatchEngine()
    with patch.object(engine, "_evaluate_one", return_value=None):
        result = engine.evaluate(
            object(),  # type: ignore[arg-type]
            [],
            [],
            None,
            [],
            continuity_person_id="prior",
        )
    assert result.decision is MatchDecision.REVIEW
    assert result.matched_person_id == "prior"
    assert result.is_new_person is False


def test_continuity_with_additional_destination_forces_review() -> None:
    destination = _merge("prior", 1.0).model_copy(update={"additional_linked_person_ids": ["new"]})
    engine = MatchEngine()
    scores = {"prior": destination, "new": _merge("new", 1.0)}
    with patch.object(engine, "_evaluate_one", side_effect=lambda _tx, pid, *_a: scores[pid]):
        result = engine.evaluate(
            object(),  # type: ignore[arg-type]
            [CandidateResult(person_id="new")],
            [],
            None,
            [],
            continuity_person_id="prior",
        )
    assert result.decision is MatchDecision.REVIEW
    assert result.additional_linked_person_ids == []
