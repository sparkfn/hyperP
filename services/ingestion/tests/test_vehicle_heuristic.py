from __future__ import annotations

from src.matching.vehicle_heuristic import (
    VEHICLE_MATCH_AUTO,
    VEHICLE_MATCH_REVIEW,
    VehicleCandidate,
    build_vehicle_match_result,
    build_vehicle_no_match_result,
    build_vehicle_review_result,
    select_best_vehicle_candidate,
)
from src.models import EngineType, MatchDecision


def _c(**overrides: object) -> VehicleCandidate:
    base: dict[str, object] = {
        "person_id": "person-1",
        "vehicle_id": "vehicle-1",
        "rel_type": "OWNS_VEHICLE",
        "is_active": True,
        "conflict_flag": False,
        "last_confirmed_at": "2026-06-01T00:00:00+00:00",
        "contact_channels": ["email"],
        "nric_blocked": False,
    }
    base.update(overrides)
    return VehicleCandidate(**base)  # type: ignore[arg-type]


def test_select_best_returns_none_for_empty_list() -> None:
    assert select_best_vehicle_candidate([]) is None


def test_select_best_prefers_active_owns_no_conflict() -> None:
    owns = _c(person_id="p-owns", rel_type="OWNS_VEHICLE", is_active=True, conflict_flag=False)
    bought = _c(
        person_id="p-bought", rel_type="BOUGHT_VEHICLE", is_active=False, conflict_flag=False
    )
    assert select_best_vehicle_candidate([bought, owns]) == owns


def test_select_best_conflicted_owns_treated_as_lower_tier() -> None:
    conflicted_owns = _c(
        person_id="p-conflict",
        rel_type="OWNS_VEHICLE",
        is_active=True,
        conflict_flag=True,
        last_confirmed_at="2026-06-10T00:00:00+00:00",
    )
    bought = _c(
        person_id="p-bought",
        rel_type="BOUGHT_VEHICLE",
        is_active=False,
        conflict_flag=False,
        last_confirmed_at="2026-06-05T00:00:00+00:00",
    )
    # Conflicted OWNS drops to the same tier as BOUGHT; the more-recent date
    # then wins (conflicted_owns is more recent).
    assert select_best_vehicle_candidate([conflicted_owns, bought]) == conflicted_owns


def test_select_best_tie_breaks_on_most_recent_date() -> None:
    older = _c(person_id="p-a", last_confirmed_at="2026-05-01T00:00:00+00:00")
    newer = _c(person_id="p-b", last_confirmed_at="2026-06-01T00:00:00+00:00")
    assert select_best_vehicle_candidate([older, newer]) == newer


def test_select_best_tie_breaks_on_smallest_person_id() -> None:
    same_date = "2026-06-01T00:00:00+00:00"
    a = _c(person_id="person-b", last_confirmed_at=same_date)
    b = _c(person_id="person-a", last_confirmed_at=same_date)
    assert select_best_vehicle_candidate([a, b]) == b


def test_build_match_result_auto_merge() -> None:
    candidate = _c()
    result = build_vehicle_match_result(candidate)
    assert result.decision == MatchDecision.MERGE
    assert result.confidence == VEHICLE_MATCH_AUTO
    assert result.engine_type == EngineType.HEURISTIC
    assert result.matched_person_id == "person-1"
    assert result.reasons == [
        "vehicle_identity + email match (OWNS_VEHICLE, person person-1, vehicle vehicle-1)"
    ]
    assert result.feature_snapshot == {
        "candidate_person_id": "person-1",
        "vehicle_id": "vehicle-1",
        "rel_type": "OWNS_VEHICLE",
        "conflict_flag": False,
        "contact_channels": ["email"],
        "nric_blocked": False,
        "signal_source": "vehicle",
    }


def test_build_match_result_no_contact_channels_reports_none() -> None:
    candidate = _c(contact_channels=[])
    result = build_vehicle_match_result(candidate)
    # Empty channel list collapses to the "none" sentinel in the reason.
    assert "vehicle_identity + none match" in result.reasons[0]


def test_build_match_result_appends_conflict_note() -> None:
    candidate = _c(
        person_id="person-3",
        vehicle_id="vehicle-3",
        rel_type="BOUGHT_VEHICLE",
        is_active=False,
        conflict_flag=True,
    )
    result = build_vehicle_match_result(candidate)
    assert result.reasons == [
        "vehicle_identity + email match (BOUGHT_VEHICLE, person person-3, vehicle vehicle-3)"
        "; vehicle has conflicting ownership claims"
    ]
    assert result.feature_snapshot["conflict_flag"] is True


def test_build_no_match_result() -> None:
    candidate = _c(person_id="person-9", vehicle_id="vehicle-9", nric_blocked=True)
    result = build_vehicle_no_match_result(candidate)
    assert result.decision == MatchDecision.NO_MATCH
    assert result.confidence == 0.0
    assert result.reasons == ["nric_anti_match"]
    assert result.matched_person_id == "person-9"
    assert result.feature_snapshot == {
        "candidate_person_id": "person-9",
        "vehicle_id": "vehicle-9",
        "nric_blocked": True,
        "signal_source": "vehicle",
    }


def test_build_review_result_carries_additional_persons() -> None:
    best = _c(person_id="person-a", vehicle_id="vehicle-1", last_confirmed_at="2026-06-10")
    other = _c(
        person_id="person-b",
        vehicle_id="vehicle-2",
        rel_type="BOUGHT_VEHICLE",
        is_active=False,
        last_confirmed_at="2026-06-01",
    )
    result = build_vehicle_review_result([best, other])
    assert result.decision == MatchDecision.REVIEW
    assert result.confidence == VEHICLE_MATCH_REVIEW
    assert result.engine_type == EngineType.HEURISTIC
    assert result.matched_person_id == "person-a"
    assert result.additional_linked_person_ids == ["person-b"]
    assert result.feature_snapshot["candidate_count"] == 2


def test_confidence_constants_in_expected_bands() -> None:
    # Auto sits at the auto-merge threshold (0.90); review sits inside the
    # review band [0.60, 0.90).
    assert VEHICLE_MATCH_AUTO == 0.90
    assert 0.60 <= VEHICLE_MATCH_REVIEW < 0.90