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

from src.matching.deterministic import evaluate_deterministic
from src.matching.heuristic import evaluate_heuristic
from src.models import (
    CandidateResult,
    EngineType,
    MatchDecision,
    MatchResult,
    NormalizedAddress,
    NormalizedAttribute,
    NormalizedIdentifier,
    RecordType,
)

logger = logging.getLogger(__name__)


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
    ) -> MatchResult:
        """Run the full match chain and return the final result.

        ``record_type`` carries the incoming envelope's provenance class. When
        the incoming record is a ``conversation`` extract, the deterministic
        merge layer is suppressed (Layer 1 hard-merge rules cannot fire on
        heuristically-extracted evidence). Hard NO_MATCH rules (locks,
        conflicting government IDs) still apply because they are blockers,
        not merges.
        """
        if not candidates:
            return self._no_candidates_result()

        unique_candidates = {c.person_id: c for c in candidates}
        collected: list[MatchResult] = []

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
            )
            if per_candidate is None:
                continue
            collected.append(per_candidate)

        return self._pick_best(collected)

    def _evaluate_one(
        self,
        tx: ManagedTransaction,
        candidate_person_id: str,
        identifiers: list[NormalizedIdentifier],
        address: NormalizedAddress | None,
        attributes: list[NormalizedAttribute],
        record_type: RecordType,
    ) -> MatchResult | None:
        """Run one candidate through deterministic → heuristic → LLM."""
        det = evaluate_deterministic(
            tx,
            candidate_person_id,
            identifiers,
            attributes,
            record_type,
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
            reasons=["No candidate scored above 0.60 — creating separate person"],
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
