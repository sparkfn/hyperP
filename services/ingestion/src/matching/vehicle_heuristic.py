"""Vehicle-identity matching heuristic for pending-customer sales records.

A pending-customer sale that shares a Vehicle identity with an active Person —
AND whose customer email/phone overlaps that Person's identifiers — auto-links
at ``VEHICLE_MATCH_AUTO`` (0.90). When the best candidate is NRIC-blocked (the
customer's NRIC disagrees with the Person's NRIC) a ``NO_MATCH`` decision is
recorded for the pair and the sale is not linked. Multiple distinct candidate
persons fall to the review band.

The contact-channel overlap is enforced inside ``FIND_VEHICLE_CANDIDATES_FOR_SALES``
(see ``graph/queries/sales.py``): the query only returns Persons who BOTH share
a Vehicle identity with the sale AND carry an email/phone Identifier matching
the sale's customer. ``contact_channels`` records which channels overlapped so
the heuristic can surface them in the match reason / feature snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from src.models import EngineType, JsonValue, MatchDecision, MatchResult

#: Auto-merge confidence for a single clear vehicle candidate. Sits above the
#: global auto-merge threshold (``CONFIDENCE_AUTO_MERGE``) so the decision is
#: ``MatchDecision.MERGE`` and the sale is linked immediately.
VEHICLE_MATCH_AUTO: float = 0.90
#: Review-band confidence for the multiple-distinct-persons case. Sits above
#: the global review threshold (``CONFIDENCE_REVIEW``) but below
#: ``VEHICLE_MATCH_AUTO`` so the decision is ``MatchDecision.REVIEW`` and a
#: ReviewCase is created.
VEHICLE_MATCH_REVIEW: float = 0.70


@dataclass(frozen=True)
class VehicleCandidate:
    """A Person who shares a Vehicle identity with a pending-customer sale.

    Built from a ``FIND_VEHICLE_CANDIDATES_FOR_SALES`` row. ``contact_channels``
    is the set of identifier kinds (``"email"`` / ``"phone"``) that overlapped
    between the sale's customer and the Person — the query's required overlap.
    ``nric_blocked`` is True when the customer's NRIC disagrees with one of the
    Person's NRIC identifiers (anti-match guard).
    """

    person_id: str
    vehicle_id: str
    rel_type: str
    is_active: bool
    conflict_flag: bool
    last_confirmed_at: str | None
    contact_channels: list[str] = field(default_factory=list)
    nric_blocked: bool = False


def _is_best_tier(c: VehicleCandidate) -> bool:
    """OWNS_VEHICLE, is_active=True, conflict_flag=False -- strongest signal."""
    return c.rel_type == "OWNS_VEHICLE" and c.is_active and not c.conflict_flag


def select_best_vehicle_candidate(
    candidates: list[VehicleCandidate],
) -> VehicleCandidate | None:
    """Pick the strongest candidate, or ``None`` for an empty list.

    Ranking:
      1. Best-tier first (``_is_best_tier``).
      2. Most-recent ``last_confirmed_at`` (ISO string comparison).
      3. Smallest ``person_id`` (stable tie-break).
    """
    if not candidates:
        return None

    # 1. Best tier (OWNS_VEHICLE, active, no conflict) ranks first.
    best_tier_rank = max(1 if _is_best_tier(c) else 0 for c in candidates)
    top = [c for c in candidates if (1 if _is_best_tier(c) else 0) == best_tier_rank]
    # 2. Most-recent last_confirmed_at (None/empty sorts oldest).
    most_recent = max(top, key=lambda c: c.last_confirmed_at or "")
    # 3. Smallest person_id among ties.
    tied = [c for c in top if (c.last_confirmed_at or "") == (most_recent.last_confirmed_at or "")]
    return min(tied, key=lambda c: c.person_id)


def build_vehicle_match_result(candidate: VehicleCandidate) -> MatchResult:
    """Build the auto-merge ``MatchResult`` for a single clear vehicle candidate."""
    channels = ",".join(sorted(candidate.contact_channels)) or "none"
    reason = (
        f"vehicle_identity + {channels} match "
        f"({candidate.rel_type}, person {candidate.person_id}, "
        f"vehicle {candidate.vehicle_id})"
    )
    if candidate.conflict_flag:
        reason += "; vehicle has conflicting ownership claims"

    feature_snapshot: dict[str, JsonValue] = {
        "candidate_person_id": candidate.person_id,
        "vehicle_id": candidate.vehicle_id,
        "rel_type": candidate.rel_type,
        "conflict_flag": candidate.conflict_flag,
        "contact_channels": list(candidate.contact_channels),
        "nric_blocked": False,
        "signal_source": "vehicle",
    }
    return MatchResult(
        decision=MatchDecision.MERGE,
        confidence=VEHICLE_MATCH_AUTO,
        reasons=[reason],
        engine_type=EngineType.HEURISTIC,
        matched_person_id=candidate.person_id,
        feature_snapshot=feature_snapshot,
    )


def build_vehicle_no_match_result(candidate: VehicleCandidate) -> MatchResult:
    """Build the ``NO_MATCH`` result for an NRIC-blocked best candidate."""
    feature_snapshot: dict[str, JsonValue] = {
        "candidate_person_id": candidate.person_id,
        "vehicle_id": candidate.vehicle_id,
        "nric_blocked": True,
        "signal_source": "vehicle",
    }
    return MatchResult(
        decision=MatchDecision.NO_MATCH,
        confidence=0.0,
        reasons=["nric_anti_match"],
        engine_type=EngineType.HEURISTIC,
        matched_person_id=candidate.person_id,
        feature_snapshot=feature_snapshot,
    )


def build_vehicle_review_result(
    candidates: list[VehicleCandidate],
) -> MatchResult:
    """Build the review-band ``MatchResult`` for multiple distinct candidate persons.

    The primary matched person is the best-ranked candidate; every other
    distinct person id is carried on ``additional_linked_person_ids`` so their
    evidence is linked without merging them (mirrors the multi-match policy).
    NRIC-blocked candidates are filtered out of the ``additional`` list — a
    Person whose NRIC disagrees with the sale's customer must never receive
    evidence edges (the blocked Person is recorded on the ``best``'s NO_MATCH
    pair above, not here).
    """
    best = select_best_vehicle_candidate(candidates)
    # ``best`` is non-None here -- callers guard the empty case.
    assert best is not None
    person_ids = {
        c.person_id for c in candidates if not c.nric_blocked or c.person_id == best.person_id
    }
    additional = sorted(pid for pid in person_ids if pid != best.person_id)
    feature_snapshot: dict[str, JsonValue] = {
        "candidate_person_id": best.person_id,
        "vehicle_id": best.vehicle_id,
        "candidate_person_ids": cast(list[JsonValue], sorted(person_ids)),
        "candidate_count": len(person_ids),
        "signal_source": "vehicle",
    }
    return MatchResult(
        decision=MatchDecision.REVIEW,
        confidence=VEHICLE_MATCH_REVIEW,
        reasons=["multiple_vehicle_candidates"],
        engine_type=EngineType.HEURISTIC,
        matched_person_id=best.person_id,
        additional_linked_person_ids=additional,
        feature_snapshot=feature_snapshot,
    )
