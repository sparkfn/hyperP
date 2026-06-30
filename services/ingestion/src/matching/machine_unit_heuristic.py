"""Heuristic scoring for sales-record↔person candidate matching via shared MachineUnit evidence.

Both confidence constants sit inside [CONFIDENCE_REVIEW, CONFIDENCE_AUTO_MERGE) = [0.60, 0.90),
so this engine can never produce an auto-merge decision.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.models import EngineType, JsonValue, MatchDecision, MatchResult

#: OWNS_UNIT, is_active=True, conflict_flag=False — strongest machine-unit signal.
MACHINE_UNIT_OWNS_CONFIDENCE: float = 0.65
#: BOUGHT_UNIT-only, or any conflict-flagged unit.
MACHINE_UNIT_BOUGHT_CONFIDENCE: float = 0.60


@dataclass(frozen=True)
class MachineUnitCandidate:
    """A Person who shares a MachineUnit with a pending-customer sales SourceRecord."""

    person_id: str
    machine_unit_id: str
    rel_type: str
    is_active: bool
    conflict_flag: bool
    last_confirmed_at: str | None


def _is_best_tier(c: MachineUnitCandidate) -> bool:
    return c.rel_type == "OWNS_UNIT" and c.is_active and not c.conflict_flag


def select_best_machine_unit_candidate(
    candidates: list[MachineUnitCandidate],
) -> MachineUnitCandidate | None:
    """Select the single best candidate per the ranking in the design spec.

    1. OWNS_UNIT (active, no conflict) beats everything else.
    2. Tie-break by most-recent last_confirmed_at (ISO string comparison).
    3. Final tie-break by smallest person_id (deterministic).
    """
    if not candidates:
        return None

    def _score(c: MachineUnitCandidate) -> tuple[int, str]:
        return (1 if _is_best_tier(c) else 0, c.last_confirmed_at or "")

    best_score = max(_score(c) for c in candidates)
    tied = [c for c in candidates if _score(c) == best_score]
    return min(tied, key=lambda c: c.person_id)


def build_machine_unit_match_result(candidate: MachineUnitCandidate) -> MatchResult:
    """Build a REVIEW-band MatchResult for the selected candidate."""
    if _is_best_tier(candidate):
        confidence = MACHINE_UNIT_OWNS_CONFIDENCE
        reason = (
            f"same_machine_unit_owner_claim (OWNS_UNIT, person {candidate.person_id},"
            f" unit {candidate.machine_unit_id})"
        )
    else:
        confidence = MACHINE_UNIT_BOUGHT_CONFIDENCE
        reason = (
            f"same_machine_unit_purchase ({candidate.rel_type},"
            f" person {candidate.person_id}, unit {candidate.machine_unit_id})"
        )
    if candidate.conflict_flag:
        reason += "; unit has conflicting ownership claims"

    feature_snapshot: dict[str, JsonValue] = {
        "candidate_person_id": candidate.person_id,
        "machine_unit_id": candidate.machine_unit_id,
        "rel_type": candidate.rel_type,
        "conflict_flag": candidate.conflict_flag,
        "signal_source": "machine_unit",
    }
    return MatchResult(
        decision=MatchDecision.REVIEW,
        confidence=confidence,
        reasons=[reason],
        engine_type=EngineType.HEURISTIC,
        matched_person_id=candidate.person_id,
        feature_snapshot=feature_snapshot,
    )
