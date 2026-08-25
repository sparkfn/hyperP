"""Match engine — orchestrates the deterministic → heuristic → LLM chain.

The engine is split into focused modules and re-composed here:

1. :meth:`MatchEngine.evaluate` orchestrates the chain across all candidates.
2. :mod:`src.matching.deterministic` runs Layer 1 hard rules per candidate.
3. :mod:`src.matching.heuristic` runs Layer 2 conditional-weight scoring.
4. :mod:`src.matching.snapshot` pre-fetches candidate-side data once per pair.
5. LLM adjudication (Phase 5) is stubbed below.
"""

from __future__ import annotations

import logging

from neo4j import ManagedTransaction

from src.matching.deterministic import evaluate_deterministic, prefetch_no_match_lock_owners
from src.matching.heuristic import evaluate_heuristic
from src.models import (
    CandidateResult,
    EngineType,
    JsonValue,
    MatchDecision,
    MatchResult,
    NormalizedAddress,
    NormalizedAttribute,
    NormalizedIdentifier,
    RecordType,
)

logger = logging.getLogger(__name__)

REASSIGNMENT_AUTO_THRESHOLD: float = 0.90
REASSIGNMENT_MIN_MARGIN: float = 0.10


def ambiguous_prior_owners_result(prior_person_ids: tuple[str, ...]) -> MatchResult:
    """Require review when one immutable version previously supported many people."""
    if len(prior_person_ids) < 2:
        raise ValueError("ambiguous prior-owner policy requires at least two owners")
    return MatchResult(
        decision=MatchDecision.REVIEW,
        confidence=0.0,
        reasons=["ambiguous_prior_owners"],
        engine_type=EngineType.DETERMINISTIC,
        matched_person_id=min(prior_person_ids),
    )


class MatchEngine:
    """Evaluate candidates through a deterministic → heuristic → LLM chain.

    The engine receives candidate persons discovered during graph traversal
    and returns a single :class:`MatchResult` indicating what action to take.
    """

    def evaluate(
        self,
        tx: ManagedTransaction,
        candidates: list[CandidateResult],
        identifiers: list[NormalizedIdentifier],
        address: NormalizedAddress | None,
        attributes: list[NormalizedAttribute],
        record_type: RecordType = RecordType.IDENTITY,
        *,
        continuity_person_id: str | None = None,
    ) -> MatchResult:
        """Run the full match chain and return the final result.

        ``record_type`` carries the incoming envelope's provenance class. When
        the incoming record is a ``conversation`` extract, the deterministic
        merge layer is suppressed (Layer 1 hard-merge rules cannot fire on
        heuristically-extracted evidence). Hard NO_MATCH rules (locks,
        conflicting government IDs) still apply because they are blockers,
        not merges.
        """
        if not candidates and continuity_person_id is None:
            return self._no_candidates_result()

        unique_candidates = {c.person_id: c for c in candidates}
        if continuity_person_id is not None:
            unique_candidates.setdefault(
                continuity_person_id,
                CandidateResult(person_id=continuity_person_id, source="continuity"),
            )
        collected: list[MatchResult] = []
        continuity_result: MatchResult | None = None
        phone_fanout_cache: dict[str, int] = {}
        no_match_lock_owners = prefetch_no_match_lock_owners(
            tx,
            list(unique_candidates),
            identifiers,
        )

        # Evaluate every candidate (no short-circuit on the first deterministic
        # MERGE): an incoming record that independently MERGE-matches more than
        # one distinct person must be linked to all of them, so every match has
        # to be collected rather than dropped after the first.
        for person_id in unique_candidates:
            per_candidate = self._evaluate_one(
                tx,
                person_id,
                identifiers,
                address,
                attributes,
                record_type,
                phone_fanout_cache,
                no_match_lock_owners,
            )
            if per_candidate is None:
                continue
            collected.append(per_candidate)

            if person_id == continuity_person_id:
                continuity_result = per_candidate

        result = self._pick_best(collected)
        if continuity_person_id is None:
            return result
        if result.additional_linked_person_ids:
            destinations = [
                candidate
                for candidate in collected
                if candidate.matched_person_id not in {None, continuity_person_id}
                and candidate.decision is MatchDecision.MERGE
            ]
            if len(destinations) == 1 and continuity_result is not None:
                return self._apply_reassignment_policy(
                    destinations[0], continuity_person_id, continuity_result
                )
            proposed = destinations[0].matched_person_id if len(destinations) == 1 else None
            return self._continuity_review(result, continuity_person_id, proposed, 0.0)
        if result.matched_person_id is None or result.is_new_person:
            return self._continuity_review(result, continuity_person_id, None, 0.0)
        if result.matched_person_id == continuity_person_id:
            return result
        return self._apply_reassignment_policy(result, continuity_person_id, continuity_result)

    @staticmethod
    def _apply_reassignment_policy(
        destination: MatchResult,
        continuity_person_id: str,
        continuity: MatchResult | None,
    ) -> MatchResult:
        continuity_confidence = continuity.confidence if continuity is not None else 0.0
        margin = destination.confidence - continuity_confidence
        if (
            continuity is not None
            and destination.decision is MatchDecision.MERGE
            and destination.confidence >= REASSIGNMENT_AUTO_THRESHOLD
            and margin + 1e-12 >= REASSIGNMENT_MIN_MARGIN
        ):
            return destination
        return MatchEngine._continuity_review(
            destination,
            continuity_person_id,
            destination.matched_person_id,
            continuity_confidence,
        )

    @staticmethod
    def _continuity_review(
        destination: MatchResult,
        continuity_person_id: str,
        proposed_person_id: str | None,
        continuity_confidence: float,
    ) -> MatchResult:
        merge_candidate_person_ids = sorted(
            {
                person_id
                for person_id in [
                    destination.matched_person_id,
                    *destination.additional_linked_person_ids,
                ]
                if person_id is not None
            }
        )
        feature_snapshot = dict(destination.feature_snapshot)
        merge_candidate_values: list[JsonValue] = list(merge_candidate_person_ids)
        if len(merge_candidate_person_ids) > 1:
            feature_snapshot["merge_candidate_person_ids"] = merge_candidate_values
        return MatchResult(
            decision=MatchDecision.REVIEW,
            confidence=destination.confidence,
            reasons=[
                *destination.reasons,
                "Changed record requires review before reassigning its prior person",
            ],
            engine_type=destination.engine_type,
            matched_person_id=continuity_person_id,
            proposed_person_id=proposed_person_id,
            feature_snapshot={
                **feature_snapshot,
                "continuity_person_id": continuity_person_id,
                "continuity_confidence": continuity_confidence,
                "proposed_person_id": proposed_person_id,
                "proposed_confidence": destination.confidence,
            },
        )

    def _evaluate_one(
        self,
        tx: ManagedTransaction,
        candidate_person_id: str,
        identifiers: list[NormalizedIdentifier],
        address: NormalizedAddress | None,
        attributes: list[NormalizedAttribute],
        record_type: RecordType,
        phone_fanout_cache: dict[str, int],
        no_match_lock_owners: dict[str, str],
    ) -> MatchResult | None:
        """Run one candidate through deterministic → heuristic → LLM."""
        det = evaluate_deterministic(
            tx,
            candidate_person_id,
            identifiers,
            attributes,
            record_type,
            no_match_lock_owners=no_match_lock_owners,
        )
        if det is not None:
            # Hard NO_MATCH: drop the candidate without falling through.
            if det.decision == MatchDecision.NO_MATCH:
                return None
            return det

        heur = evaluate_heuristic(
            tx,
            candidate_person_id,
            identifiers,
            address,
            attributes,
            record_type,
            phone_fanout_cache=phone_fanout_cache,
        )
        if heur.decision != MatchDecision.NO_MATCH:
            return heur

        return self._evaluate_llm(
            candidate_person_id,
            identifiers,
            address,
            attributes,
        )

    @staticmethod
    def _no_candidates_result() -> MatchResult:
        return MatchResult(
            decision=MatchDecision.NO_MATCH,
            confidence=0.0,
            reasons=["No matching candidates found"],
            engine_type=EngineType.DETERMINISTIC,
            is_new_person=True,
        )

    @staticmethod
    def _pick_best(collected: list[MatchResult]) -> MatchResult:
        """Choose the highest-confidence MERGE → REVIEW → NO_MATCH fallback."""
        if not collected:
            return MatchResult(
                decision=MatchDecision.NO_MATCH,
                confidence=0.0,
                reasons=[
                    "Candidates exist but no engine produced a confident match "
                    "— creating separate person"
                ],
                engine_type=EngineType.DETERMINISTIC,
                is_new_person=True,
            )

        merges = [r for r in collected if r.decision == MatchDecision.MERGE]
        if merges:
            return MatchEngine._resolve_merges(merges)

        reviews = [r for r in collected if r.decision == MatchDecision.REVIEW]
        if reviews:
            reviews.sort(key=lambda r: r.confidence, reverse=True)
            return reviews[0]

        return MatchResult(
            decision=MatchDecision.NO_MATCH,
            confidence=0.0,
            reasons=["No candidate scored above the review threshold — creating separate person"],
            engine_type=EngineType.HEURISTIC,
            is_new_person=True,
        )

    @staticmethod
    def _resolve_merges(merges: list[MatchResult]) -> MatchResult:
        """Pick the primary match and, on a multi-person match, the extra links.

        Primary = highest confidence, ties broken by ``person_id`` so the
        outcome is deterministic regardless of candidate iteration order. When
        the record MERGE-matched more than one distinct person, the record and
        its evidence are linked to all of them (the extras are returned in
        ``additional_linked_person_ids``); the persons are NOT merged, since
        they may legitimately share an identifier.
        """
        merges.sort(key=lambda r: (-r.confidence, r.matched_person_id or ""))
        primary = merges[0]
        additional = sorted(
            {
                r.matched_person_id
                for r in merges[1:]
                if r.matched_person_id is not None
                and r.matched_person_id != primary.matched_person_id
            }
        )
        if not additional:
            return primary
        reasons = [
            *primary.reasons,
            (
                f"Record also MERGE-matched {len(additional)} other person(s) "
                f"{additional} — linking record + evidence to all (persons not merged)"
            ),
        ]
        return primary.model_copy(
            update={"additional_linked_person_ids": additional, "reasons": reasons}
        )

    def _evaluate_llm(
        self,
        candidate_person_id: str,
        identifiers: list[NormalizedIdentifier],
        address: NormalizedAddress | None,
        attributes: list[NormalizedAttribute],
    ) -> MatchResult | None:
        """LLM-assisted adjudication.

        .. TODO:: Phase 5 — Implement structured LLM adjudication.
           Must operate in shadow/assist mode only during MVP.
           Must return structured JSON matching the MatchDecision contract.
           Must not override hard conflict rules.
           Must log prompt and model versions.
        """
        return None  # pass-through to next stage
