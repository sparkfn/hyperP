from __future__ import annotations

from src.matching.machine_unit_heuristic import (
    MACHINE_UNIT_BOUGHT_CONFIDENCE,
    MACHINE_UNIT_OWNS_CONFIDENCE,
    MachineUnitCandidate,
    build_machine_unit_match_result,
    select_best_machine_unit_candidate,
)
from src.models import EngineType, MatchDecision


def _c(**overrides: object) -> MachineUnitCandidate:
    base: dict[str, object] = {
        "person_id": "person-1",
        "machine_unit_id": "unit-1",
        "rel_type": "OWNS_UNIT",
        "is_active": True,
        "conflict_flag": False,
        "last_confirmed_at": "2026-06-01T00:00:00+00:00",
    }
    base.update(overrides)
    return MachineUnitCandidate(**base)  # type: ignore[arg-type]


def test_select_best_returns_none_for_empty_list() -> None:
    assert select_best_machine_unit_candidate([]) is None


def test_select_best_prefers_active_owns_no_conflict() -> None:
    owns = _c(person_id="p-owns", rel_type="OWNS_UNIT", is_active=True, conflict_flag=False)
    bought = _c(person_id="p-bought", rel_type="BOUGHT_UNIT", is_active=False, conflict_flag=False)
    assert select_best_machine_unit_candidate([bought, owns]) == owns


def test_select_best_conflicted_owns_treated_as_lower_tier() -> None:
    conflicted_owns = _c(
        person_id="p-conflict",
        rel_type="OWNS_UNIT",
        is_active=True,
        conflict_flag=True,
        last_confirmed_at="2026-06-10T00:00:00+00:00",
    )
    bought = _c(
        person_id="p-bought",
        rel_type="BOUGHT_UNIT",
        is_active=False,
        conflict_flag=False,
        last_confirmed_at="2026-06-01T00:00:00+00:00",
    )
    # Both are tier 0 (non-best-tier); most-recent last_confirmed_at wins.
    assert select_best_machine_unit_candidate([bought, conflicted_owns]) == conflicted_owns


def test_select_best_tie_breaks_by_most_recent_last_confirmed_at() -> None:
    older = _c(person_id="p-older", last_confirmed_at="2026-05-01T00:00:00+00:00")
    newer = _c(person_id="p-newer", last_confirmed_at="2026-06-10T00:00:00+00:00")
    assert select_best_machine_unit_candidate([older, newer]) == newer


def test_select_best_final_tie_break_by_person_id() -> None:
    a = _c(person_id="person-aaa", last_confirmed_at="2026-06-01T00:00:00+00:00")
    b = _c(person_id="person-bbb", last_confirmed_at="2026-06-01T00:00:00+00:00")
    assert select_best_machine_unit_candidate([b, a]) == a


def test_select_best_none_last_confirmed_at_sorts_last() -> None:
    has_date = _c(person_id="p-date", last_confirmed_at="2026-01-01T00:00:00+00:00")
    no_date = _c(person_id="p-none", last_confirmed_at=None)
    # has_date has score (1, "2026-01-01...") vs (1, "") — higher score wins.
    assert select_best_machine_unit_candidate([has_date, no_date]) == has_date


def test_build_match_result_owns_unit_active_no_conflict() -> None:
    candidate = _c(person_id="person-1", machine_unit_id="unit-1")

    result = build_machine_unit_match_result(candidate)

    assert result.decision == MatchDecision.REVIEW
    assert result.confidence == MACHINE_UNIT_OWNS_CONFIDENCE
    assert result.engine_type == EngineType.HEURISTIC
    assert result.matched_person_id == "person-1"
    assert result.reasons == [
        "same_machine_unit_owner_claim (OWNS_UNIT, person person-1, unit unit-1)"
    ]
    assert result.feature_snapshot == {
        "candidate_person_id": "person-1",
        "machine_unit_id": "unit-1",
        "rel_type": "OWNS_UNIT",
        "conflict_flag": False,
        "signal_source": "machine_unit",
    }


def test_build_match_result_bought_unit() -> None:
    candidate = _c(
        person_id="person-2",
        machine_unit_id="unit-2",
        rel_type="BOUGHT_UNIT",
        is_active=False,
    )
    result = build_machine_unit_match_result(candidate)
    assert result.confidence == MACHINE_UNIT_BOUGHT_CONFIDENCE
    assert result.reasons == [
        "same_machine_unit_purchase (BOUGHT_UNIT, person person-2, unit unit-2)"
    ]


def test_build_match_result_appends_conflict_note() -> None:
    candidate = _c(
        person_id="person-3",
        machine_unit_id="unit-3",
        rel_type="BOUGHT_UNIT",
        is_active=False,
        conflict_flag=True,
    )
    result = build_machine_unit_match_result(candidate)
    assert result.reasons == [
        "same_machine_unit_purchase (BOUGHT_UNIT, person person-3, unit unit-3)"
        "; unit has conflicting ownership claims"
    ]
    assert result.feature_snapshot["conflict_flag"] is True


def test_confidence_constants_inside_review_band() -> None:
    # Both constants must satisfy: CONFIDENCE_REVIEW (0.60) <= x < CONFIDENCE_AUTO_MERGE (0.90)
    assert 0.60 <= MACHINE_UNIT_BOUGHT_CONFIDENCE < MACHINE_UNIT_OWNS_CONFIDENCE < 0.90
