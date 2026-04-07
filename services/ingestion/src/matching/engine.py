"""Match engine — chains deterministic, heuristic, and LLM stages.

Deterministic rules (Phase 2) and heuristic scoring (Phase 3) are implemented;
LLM adjudication (Phase 5) remains stubbed.

The engine is split into three layers:

1. :meth:`MatchEngine.evaluate` orchestrates the chain across all candidates.
2. :meth:`MatchEngine._evaluate_deterministic` and ``_evaluate_heuristic``
   produce a single per-candidate :class:`MatchResult` (or ``None``).
3. Per-feature helpers (``_check_no_match_lock``, ``_score_phone``, …) keep
   each stage testable in isolation.
"""

from __future__ import annotations

import logging

from neo4j import ManagedTransaction

from src.graph import queries
from src.matching.similarity import jaro_winkler_similarity
from src.models import (
    CandidateResult,
    EngineType,
    JsonValue,
    MatchDecision,
    MatchResult,
    NormalizedAddress,
    NormalizedAttribute,
    NormalizedIdentifier,
    QualityFlag,
    RecordType,
)

logger = logging.getLogger(__name__)


# Cypher snippets that don't have a home in queries.py because they're only
# referenced from this module. Kept here so the engine is self-contained.
_FIND_OWNERS_OF_IDENTIFIER = """
MATCH (id:Identifier {
    identifier_type: $identifier_type,
    normalized_value: $normalized_value
})<-[rel:IDENTIFIED_BY]-(owner:Person {status: 'active'})
WHERE rel.is_active = true
  AND owner.person_id <> $candidate_person_id
RETURN owner.person_id AS owner_person_id
"""

_PERSON_HAS_IDENTIFIER = """
MATCH (p:Person {person_id: $person_id})
      -[rel:IDENTIFIED_BY]->(id:Identifier {
          identifier_type: $identifier_type,
          normalized_value: $normalized_value
      })
WHERE rel.is_active = true
RETURN p.person_id AS person_id
LIMIT 1
"""

_PERSON_HAS_CONFLICTING_GOVT_ID = """
MATCH (p:Person {person_id: $person_id})
      -[rel:IDENTIFIED_BY]->(id:Identifier {
          identifier_type: 'government_id_hash'
      })
WHERE rel.is_active = true
  AND id.normalized_value <> $normalized_value
RETURN id.normalized_value AS conflicting_value
LIMIT 1
"""

# Heuristic scoring constants — exposed at module level so tests / monitoring
# can read the same values the engine uses.
IDENTIFIER_EVIDENCE_CAP = 0.85
PHONE_FANOUT_PENALTY_THRESHOLD = 5
PHONE_FANOUT_PENALTY = -0.25
PHONE_VERIFIED_WEIGHT = 0.35
PHONE_UNVERIFIED_WEIGHT = 0.20
EMAIL_VERIFIED_WEIGHT = 0.35
EMAIL_UNVERIFIED_WEIGHT = 0.20
DOB_MATCH_WEIGHT = 0.25
DOB_CONFLICT_PENALTY = -0.30
NAME_HIGH_THRESHOLD = 0.8
NAME_HIGH_WEIGHT = 0.20
NAME_MEDIUM_THRESHOLD = 0.5
NAME_MEDIUM_WEIGHT = 0.10
NAME_MISMATCH_THRESHOLD = 0.3
NAME_MISMATCH_PENALTY = -0.25
ADDRESS_MATCH_WEIGHT = 0.10

CONFIDENCE_AUTO_MERGE = 0.90
CONFIDENCE_REVIEW = 0.60

# Trusted external IDs that produce a deterministic merge on exact match.
_TRUSTED_ID_TYPES: tuple[str, ...] = ("external_customer_id", "membership_id")


def _is_usable(flag: QualityFlag) -> bool:
    return flag in (QualityFlag.VALID, QualityFlag.PARTIAL_PARSE)


# A Neo4j Record dict — heterogeneous query result. Kept as ``dict[str, object]``
# rather than ``dict[str, Any]`` so callers can't silently propagate untyped data.
RecordDict = dict[str, object]


class MatchEngine:
    """Evaluate candidates through a deterministic → heuristic → LLM chain.

    The engine receives candidate persons discovered during graph traversal
    and returns a single :class:`MatchResult` indicating what action to take.
    """

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def evaluate(
        self,
        tx: ManagedTransaction,
        candidates: list[CandidateResult],
        identifiers: list[NormalizedIdentifier],
        address: NormalizedAddress | None,
        attributes: list[NormalizedAttribute],
        record_type: RecordType = RecordType.SYSTEM,
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

        for person_id in unique_candidates:
            per_candidate = self._evaluate_one(
                tx, person_id, identifiers, address, attributes, record_type,
            )
            if per_candidate is None:
                continue
            # Deterministic MERGE is authoritative — short-circuit immediately.
            if (
                per_candidate.decision == MatchDecision.MERGE
                and per_candidate.engine_type == EngineType.DETERMINISTIC
            ):
                return per_candidate
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
        det = self._evaluate_deterministic(
            tx, candidate_person_id, identifiers, address, attributes, record_type,
        )
        if det is not None:
            # Hard NO_MATCH: drop the candidate without falling through.
            if det.decision == MatchDecision.NO_MATCH:
                return None
            return det

        heur = self._evaluate_heuristic(
            tx, candidate_person_id, identifiers, address, attributes,
        )
        if heur is not None:
            return heur

        return self._evaluate_llm(
            candidate_person_id, identifiers, address, attributes,
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
            merges.sort(key=lambda r: r.confidence, reverse=True)
            return merges[0]

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

    # ------------------------------------------------------------------
    # Stage 1: Deterministic rules
    # ------------------------------------------------------------------

    def _evaluate_deterministic(
        self,
        tx: ManagedTransaction,
        candidate_person_id: str,
        identifiers: list[NormalizedIdentifier],
        address: NormalizedAddress | None,
        attributes: list[NormalizedAttribute],
        record_type: RecordType,
    ) -> MatchResult | None:
        """Apply hard rules. Returns a result or ``None`` to fall through.

        Hard NO_MATCH rules (NO_MATCH_LOCK, conflicting government IDs) always
        run regardless of ``record_type`` — they are blockers, not merges.
        Hard MERGE rules (exact government ID, trusted external ID) are
        suppressed when the incoming record is a ``conversation`` extract:
        heuristically-extracted identifiers are never sufficient on their own
        for an auto-merge. Conversation pairs always fall through to Layer 2.

        TODO: also gate on candidate-side evidence type — if the candidate's
        only support for the matching identifier is a conversation source
        record, the deterministic merge should likewise be suppressed.
        """
        if (locked := self._check_no_match_lock(tx, candidate_person_id, identifiers)):
            return locked
        if (govt := self._check_government_id(tx, candidate_person_id, identifiers)):
            # Conflicting govt IDs (hard NO_MATCH) still apply for conversation
            # records; only the MERGE branch is suppressed below.
            if govt.decision == MatchDecision.NO_MATCH:
                return govt
            if record_type == RecordType.SYSTEM:
                return govt
        if record_type != RecordType.SYSTEM:
            return None
        if (trusted := self._check_trusted_id(tx, candidate_person_id, identifiers)):
            return trusted
        return None

    @staticmethod
    def _check_no_match_lock(
        tx: ManagedTransaction,
        candidate_person_id: str,
        identifiers: list[NormalizedIdentifier],
    ) -> MatchResult | None:
        """Hard NO_MATCH if any owner of these identifiers has a lock vs. candidate."""
        for ident in identifiers:
            if not _is_usable(ident.quality_flag):
                continue
            owner_result = tx.run(
                _FIND_OWNERS_OF_IDENTIFIER,
                identifier_type=ident.identifier_type,
                normalized_value=ident.normalized_value,
                candidate_person_id=candidate_person_id,
            )
            for owner_rec in owner_result:
                owner_pid = owner_rec["owner_person_id"]
                lock_result = tx.run(
                    queries.CHECK_NO_MATCH_LOCK,
                    left_person_id=owner_pid,
                    right_person_id=candidate_person_id,
                )
                lock_rec = lock_result.single()
                if lock_rec and lock_rec["is_locked"]:
                    logger.info(
                        "NO_MATCH_LOCK between %s and candidate %s — hard no-match",
                        owner_pid, candidate_person_id,
                    )
                    return MatchResult(
                        decision=MatchDecision.NO_MATCH,
                        confidence=1.0,
                        reasons=[
                            f"NO_MATCH_LOCK exists between person {owner_pid} "
                            f"and candidate {candidate_person_id}"
                        ],
                        engine_type=EngineType.DETERMINISTIC,
                        matched_person_id=None,
                    )
        return None

    @staticmethod
    def _check_government_id(
        tx: ManagedTransaction,
        candidate_person_id: str,
        identifiers: list[NormalizedIdentifier],
    ) -> MatchResult | None:
        """Government ID hash: exact match → hard MERGE; conflict → hard NO_MATCH."""
        govt_ids = [
            i for i in identifiers
            if i.identifier_type == "government_id_hash"
            and i.quality_flag == QualityFlag.VALID
        ]
        for govt_id in govt_ids:
            if tx.run(
                _PERSON_HAS_IDENTIFIER,
                person_id=candidate_person_id,
                identifier_type="government_id_hash",
                normalized_value=govt_id.normalized_value,
            ).single():
                logger.info(
                    "Deterministic hard merge: candidate %s shares govt ID hash",
                    candidate_person_id,
                )
                return MatchResult(
                    decision=MatchDecision.MERGE,
                    confidence=1.0,
                    reasons=["Exact government ID hash match"],
                    engine_type=EngineType.DETERMINISTIC,
                    matched_person_id=candidate_person_id,
                )

            if tx.run(
                _PERSON_HAS_CONFLICTING_GOVT_ID,
                person_id=candidate_person_id,
                normalized_value=govt_id.normalized_value,
            ).single():
                logger.info(
                    "Deterministic hard no-match: candidate %s has conflicting govt ID",
                    candidate_person_id,
                )
                return MatchResult(
                    decision=MatchDecision.NO_MATCH,
                    confidence=1.0,
                    reasons=["Conflicting government ID hash — hard no-match"],
                    engine_type=EngineType.DETERMINISTIC,
                    matched_person_id=None,
                )
        return None

    @staticmethod
    def _check_trusted_id(
        tx: ManagedTransaction,
        candidate_person_id: str,
        identifiers: list[NormalizedIdentifier],
    ) -> MatchResult | None:
        """Trusted migration-map IDs: exact match → namespace-scoped hard MERGE."""
        trusted_ids = [
            i for i in identifiers
            if i.identifier_type in _TRUSTED_ID_TYPES
            and i.quality_flag == QualityFlag.VALID
        ]
        for tid in trusted_ids:
            if tx.run(
                _PERSON_HAS_IDENTIFIER,
                person_id=candidate_person_id,
                identifier_type=tid.identifier_type,
                normalized_value=tid.normalized_value,
            ).single():
                logger.info(
                    "Deterministic hard merge: candidate %s shares trusted ID %s=%s",
                    candidate_person_id, tid.identifier_type, tid.normalized_value,
                )
                return MatchResult(
                    decision=MatchDecision.MERGE,
                    confidence=1.0,
                    reasons=[
                        f"Exact trusted {tid.identifier_type} match: "
                        f"{tid.normalized_value}"
                    ],
                    engine_type=EngineType.DETERMINISTIC,
                    matched_person_id=candidate_person_id,
                )
        return None

    # ------------------------------------------------------------------
    # Stage 2: Heuristic scoring (Phase 3)
    # ------------------------------------------------------------------

    def _evaluate_heuristic(
        self,
        tx: ManagedTransaction,
        candidate_person_id: str,
        identifiers: list[NormalizedIdentifier],
        address: NormalizedAddress | None,
        attributes: list[NormalizedAttribute],
    ) -> MatchResult | None:
        """Conditional-weight heuristic scoring across phone/email/DOB/name/address.

        Confidence bands:
            ≥ 0.90 → MERGE  (auto-merge)
            0.60–0.89 → REVIEW
            < 0.60 → NO_MATCH (explicit, so the orchestrator can drop it)
        """
        snapshot = self._fetch_candidate_snapshot(tx, candidate_person_id)

        score = 0.0
        reasons: list[str] = []

        ident_evidence = self._score_identifiers(
            tx, identifiers, snapshot, reasons
        )
        score += self._cap_identifier_evidence(ident_evidence, reasons)
        score += self._score_dob(attributes, snapshot, reasons)
        best_name_sim = self._score_name(attributes, snapshot, reasons)
        score += self._score_name_band(best_name_sim, attributes, snapshot, reasons)
        score += self._score_address(address, snapshot, reasons)

        confidence = max(0.0, min(1.0, score))
        features = self._build_feature_snapshot(
            candidate_person_id=candidate_person_id,
            reasons=reasons,
            best_name_sim=best_name_sim,
            attributes=attributes,
            snapshot=snapshot,
            ident_evidence=ident_evidence,
            raw_score=score,
        )

        logger.info(
            "Heuristic score for candidate %s: %.2f (raw=%.2f, reasons=%s)",
            candidate_person_id, confidence, score, reasons,
        )
        return self._band(confidence, reasons, candidate_person_id, features)

    # ---- candidate-side data fetch -----------------------------------

    @staticmethod
    def _fetch_candidate_snapshot(
        tx: ManagedTransaction, candidate_person_id: str,
    ) -> _CandidateSnapshot:
        """Pull all candidate-side rows the heuristic scorer needs in one shot."""
        idents: list[RecordDict] = [
            dict(r) for r in tx.run(
                queries.FETCH_PERSON_IDENTIFIERS,
                person_id=candidate_person_id,
            )
        ]
        facts: list[RecordDict] = [
            dict(r) for r in tx.run(
                queries.FETCH_PERSON_FACTS,
                person_id=candidate_person_id,
            )
        ]
        addrs: list[RecordDict] = [
            dict(r) for r in tx.run(
                queries.FETCH_PERSON_ADDRESSES,
                person_id=candidate_person_id,
            )
        ]
        return _CandidateSnapshot(idents=idents, facts=facts, addrs=addrs)

    # ---- per-feature scorers -----------------------------------------

    @staticmethod
    def _score_identifiers(
        tx: ManagedTransaction,
        identifiers: list[NormalizedIdentifier],
        snapshot: _CandidateSnapshot,
        reasons: list[str],
    ) -> float:
        """Phone + email match scoring (with phone-fanout penalty)."""
        evidence = 0.0
        cand_phones = snapshot.phones_by_value()
        cand_emails = snapshot.emails_by_value()

        for ident in identifiers:
            if ident.identifier_type == "phone" and ident.normalized_value in cand_phones:
                cand_rec = cand_phones[ident.normalized_value]
                fanout_rec = tx.run(
                    queries.CHECK_IDENTIFIER_FANOUT,
                    identifier_type="phone",
                    normalized_value=ident.normalized_value,
                ).single()
                fanout = int(fanout_rec["fanout"]) if fanout_rec else 0
                if fanout > PHONE_FANOUT_PENALTY_THRESHOLD:
                    evidence += PHONE_FANOUT_PENALTY
                    reasons.append(
                        f"Phone {ident.normalized_value} seen on {fanout} persons "
                        f"({PHONE_FANOUT_PENALTY:+.2f})"
                    )
                else:
                    verified = bool(ident.is_verified and cand_rec.get("is_verified"))
                    weight = PHONE_VERIFIED_WEIGHT if verified else PHONE_UNVERIFIED_WEIGHT
                    evidence += weight
                    reasons.append(
                        f"Phone match ({'verified' if verified else 'unverified'}: "
                        f"+{weight:.2f})"
                    )

            elif ident.identifier_type == "email" and ident.normalized_value in cand_emails:
                cand_rec = cand_emails[ident.normalized_value]
                verified = bool(ident.is_verified and cand_rec.get("is_verified"))
                weight = EMAIL_VERIFIED_WEIGHT if verified else EMAIL_UNVERIFIED_WEIGHT
                evidence += weight
                reasons.append(
                    f"Email match ({'verified' if verified else 'unverified'}: "
                    f"+{weight:.2f})"
                )
        return evidence

    @staticmethod
    def _cap_identifier_evidence(raw: float, reasons: list[str]) -> float:
        capped = min(raw, IDENTIFIER_EVIDENCE_CAP)
        if raw > IDENTIFIER_EVIDENCE_CAP:
            reasons.append(
                f"Identifier evidence capped from {raw:.2f} to {IDENTIFIER_EVIDENCE_CAP}"
            )
        return capped

    @staticmethod
    def _score_dob(
        attributes: list[NormalizedAttribute],
        snapshot: _CandidateSnapshot,
        reasons: list[str],
    ) -> float:
        incoming_dobs = [a.attribute_value for a in attributes if a.attribute_name == "dob"]
        cand_dobs = snapshot.dobs()
        if not incoming_dobs or not cand_dobs:
            return 0.0
        incoming = incoming_dobs[0]
        if incoming in cand_dobs:
            reasons.append(f"DOB exact match (+{DOB_MATCH_WEIGHT:.2f})")
            return DOB_MATCH_WEIGHT
        reasons.append(
            f"DOB conflict: incoming={incoming}, candidate={cand_dobs[0]} "
            f"({DOB_CONFLICT_PENALTY:+.2f})"
        )
        return DOB_CONFLICT_PENALTY

    @staticmethod
    def _score_name(
        attributes: list[NormalizedAttribute],
        snapshot: _CandidateSnapshot,
        reasons: list[str],
    ) -> float:
        """Return the best name similarity in [0, 1]; reasons appended by _score_name_band."""
        incoming = [
            a.attribute_value for a in attributes
            if a.attribute_name in ("full_name", "preferred_name", "legal_name")
        ]
        cand_names = snapshot.names()
        if not incoming or not cand_names:
            return 0.0
        best = 0.0
        for inc in incoming:
            for cand in cand_names:
                best = max(best, jaro_winkler_similarity(inc, cand))
        return best

    @staticmethod
    def _score_name_band(
        best_sim: float,
        attributes: list[NormalizedAttribute],
        snapshot: _CandidateSnapshot,
        reasons: list[str],
    ) -> float:
        # Only apply when both sides actually had names — otherwise best_sim is 0.
        has_incoming = any(
            a.attribute_name in ("full_name", "preferred_name", "legal_name")
            for a in attributes
        )
        if not has_incoming or not snapshot.names():
            return 0.0
        if best_sim > NAME_HIGH_THRESHOLD:
            reasons.append(f"High name similarity ({best_sim:.2f}: +{NAME_HIGH_WEIGHT:.2f})")
            return NAME_HIGH_WEIGHT
        if best_sim >= NAME_MEDIUM_THRESHOLD:
            reasons.append(f"Medium name similarity ({best_sim:.2f}: +{NAME_MEDIUM_WEIGHT:.2f})")
            return NAME_MEDIUM_WEIGHT
        if best_sim < NAME_MISMATCH_THRESHOLD:
            reasons.append(
                f"Strong name mismatch ({best_sim:.2f}: {NAME_MISMATCH_PENALTY:+.2f})"
            )
            return NAME_MISMATCH_PENALTY
        return 0.0

    @staticmethod
    def _score_address(
        address: NormalizedAddress | None,
        snapshot: _CandidateSnapshot,
        reasons: list[str],
    ) -> float:
        if address is None or not snapshot.addrs:
            return 0.0
        incoming_full = address.normalized_full.lower().strip()
        if not incoming_full:
            return 0.0
        for caddr in snapshot.addrs:
            cand_full = str(caddr.get("normalized_full") or "").lower().strip()
            if incoming_full == cand_full:
                reasons.append(f"Address match (+{ADDRESS_MATCH_WEIGHT:.2f})")
                return ADDRESS_MATCH_WEIGHT
        return 0.0

    @staticmethod
    def _build_feature_snapshot(
        *,
        candidate_person_id: str,
        reasons: list[str],
        best_name_sim: float,
        attributes: list[NormalizedAttribute],
        snapshot: _CandidateSnapshot,
        ident_evidence: float,
        raw_score: float,
    ) -> dict[str, JsonValue]:
        had_names = any(
            a.attribute_name in ("full_name", "preferred_name", "legal_name")
            for a in attributes
        ) and bool(snapshot.names())
        return {
            "candidate_person_id": candidate_person_id,
            "phone_exact_match": any(r.startswith("Phone match") for r in reasons),
            "email_exact_match": any(r.startswith("Email match") for r in reasons),
            "dob_exact_match": any(r.startswith("DOB exact") for r in reasons),
            "dob_conflict": any(r.startswith("DOB conflict") for r in reasons),
            "name_similarity": best_name_sim if had_names else None,
            "address_match": any(r.startswith("Address match") for r in reasons),
            "identifier_evidence_raw": ident_evidence,
            "identifier_evidence_capped": min(ident_evidence, IDENTIFIER_EVIDENCE_CAP),
            "raw_score": raw_score,
        }

    @staticmethod
    def _band(
        confidence: float,
        reasons: list[str],
        candidate_person_id: str,
        features: dict[str, JsonValue],
    ) -> MatchResult:
        if confidence >= CONFIDENCE_AUTO_MERGE:
            decision = MatchDecision.MERGE
            matched = candidate_person_id
        elif confidence >= CONFIDENCE_REVIEW:
            decision = MatchDecision.REVIEW
            matched = candidate_person_id
        else:
            decision = MatchDecision.NO_MATCH
            matched = None
        return MatchResult(
            decision=decision,
            confidence=confidence,
            reasons=reasons,
            engine_type=EngineType.HEURISTIC,
            matched_person_id=matched,
            feature_snapshot=features,
        )

    # ------------------------------------------------------------------
    # Stage 3: LLM adjudication (STUB — Phase 5)
    # ------------------------------------------------------------------

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


# ======================================================================
# Helper datastructures
# ======================================================================


class _CandidateSnapshot:
    """Pre-fetched view of a candidate Person's identifiers, facts, addresses.

    Bundling these into one object lets the heuristic scorer iterate them
    multiple times without re-querying Neo4j, and keeps method signatures
    short. Lazily indexes the rows on first access.
    """

    __slots__ = (
        "idents",
        "facts",
        "addrs",
        "_phones_by_value",
        "_emails_by_value",
        "_names",
        "_dobs",
    )

    def __init__(
        self,
        *,
        idents: list[RecordDict],
        facts: list[RecordDict],
        addrs: list[RecordDict],
    ) -> None:
        self.idents = idents
        self.facts = facts
        self.addrs = addrs
        self._phones_by_value: dict[str, RecordDict] | None = None
        self._emails_by_value: dict[str, RecordDict] | None = None
        self._names: list[str] | None = None
        self._dobs: list[str] | None = None

    def phones_by_value(self) -> dict[str, RecordDict]:
        if self._phones_by_value is None:
            self._phones_by_value = {
                str(i["normalized_value"]): i
                for i in self.idents
                if i.get("identifier_type") == "phone"
            }
        return self._phones_by_value

    def emails_by_value(self) -> dict[str, RecordDict]:
        if self._emails_by_value is None:
            self._emails_by_value = {
                str(i["normalized_value"]): i
                for i in self.idents
                if i.get("identifier_type") == "email"
            }
        return self._emails_by_value

    def names(self) -> list[str]:
        if self._names is None:
            self._names = [
                str(f["attribute_value"])
                for f in self.facts
                if f.get("attribute_name") in ("full_name", "preferred_name", "legal_name")
            ]
        return self._names

    def dobs(self) -> list[str]:
        if self._dobs is None:
            self._dobs = [
                str(f["attribute_value"])
                for f in self.facts
                if f.get("attribute_name") == "dob"
            ]
        return self._dobs
