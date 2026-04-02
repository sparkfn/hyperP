"""Match engine — chains deterministic, heuristic, and LLM stages.

Deterministic rules (Phase 2) and heuristic scoring (Phase 3) are
implemented.  LLM adjudication (Phase 5) remains stubbed.
"""

from __future__ import annotations

import logging
from typing import Any

from neo4j import ManagedTransaction

from src.graph import queries
from src.models import (
    CandidateResult,
    EngineType,
    MatchDecision,
    MatchResult,
    NormalizedAddress,
    NormalizedAttribute,
    NormalizedIdentifier,
)

logger = logging.getLogger(__name__)


class MatchEngine:
    """Evaluate candidates through a deterministic -> heuristic -> LLM chain.

    The engine receives candidate persons discovered during graph traversal
    and returns a single ``MatchResult`` indicating what action to take.
    """

    def evaluate(
        self,
        tx: ManagedTransaction,
        candidates: list[CandidateResult],
        identifiers: list[NormalizedIdentifier],
        address: NormalizedAddress | None,
        attributes: list[NormalizedAttribute],
    ) -> MatchResult:
        """Run the full match chain and return the final result.

        Parameters
        ----------
        tx:
            Active Neo4j managed transaction (for lock checks).
        candidates:
            Candidate persons from graph traversal.
        identifiers:
            Normalized identifiers from the incoming source record.
        address:
            Normalized address (may be ``None``).
        attributes:
            Normalized non-identifier, non-address attributes.

        Returns
        -------
        MatchResult
        """
        # No candidates -> new person
        if not candidates:
            return MatchResult(
                decision=MatchDecision.NO_MATCH,
                confidence=0.0,
                reasons=["No matching candidates found"],
                engine_type=EngineType.DETERMINISTIC,
                is_new_person=True,
            )

        # De-duplicate candidate person IDs
        unique_candidates = {c.person_id: c for c in candidates}

        # Collect results from all candidates across all stages.
        # For each candidate: deterministic first, then heuristic, then LLM.
        # If deterministic returns a result, skip heuristic/LLM for that candidate.
        collected: list[MatchResult] = []

        for person_id in unique_candidates:
            result = self._evaluate_deterministic(
                tx, person_id, identifiers, address, attributes,
            )
            if result is not None:
                # Deterministic results are authoritative — if it's a merge,
                # return immediately (hard rule).
                if result.decision == MatchDecision.MERGE:
                    return result
                # Hard no-match: skip this candidate entirely.
                if result.decision == MatchDecision.NO_MATCH:
                    continue
                collected.append(result)
                continue

            result = self._evaluate_heuristic(
                tx, person_id, identifiers, address, attributes,
            )
            if result is not None:
                collected.append(result)
                continue

            result = self._evaluate_llm(
                person_id, identifiers, address, attributes,
            )
            if result is not None:
                collected.append(result)

        # Pick the best result from the collected set.
        if not collected:
            # All candidates were either hard-no-matched or produced no signal.
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

        # Prefer the highest-confidence merge; fall back to highest-confidence review.
        merges = [r for r in collected if r.decision == MatchDecision.MERGE]
        if merges:
            merges.sort(key=lambda r: r.confidence, reverse=True)
            return merges[0]

        reviews = [r for r in collected if r.decision == MatchDecision.REVIEW]
        if reviews:
            reviews.sort(key=lambda r: r.confidence, reverse=True)
            return reviews[0]

        # Only no-match results remain — create new person.
        return MatchResult(
            decision=MatchDecision.NO_MATCH,
            confidence=0.0,
            reasons=[
                "No candidate scored above 0.60 — creating separate person"
            ],
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
    ) -> MatchResult | None:
        """Apply hard rules.  Return a result or ``None`` to pass through."""

        # ----------------------------------------------------------
        # NO_MATCH_LOCK check: if the incoming record's identifiers
        # point to any person that has an active lock against the
        # candidate, return hard no-match.
        # ----------------------------------------------------------
        for ident in identifiers:
            if ident.quality_flag not in ("valid", "partial_parse"):
                continue
            # Find persons that already own this identifier
            owner_result = tx.run(
                """
                MATCH (id:Identifier {
                    identifier_type: $identifier_type,
                    normalized_value: $normalized_value
                })<-[rel:IDENTIFIED_BY]-(owner:Person {status: 'active'})
                WHERE rel.is_active = true
                  AND owner.person_id <> $candidate_person_id
                RETURN owner.person_id AS owner_person_id
                """,
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

        # ----------------------------------------------------------
        # Government ID hash: exact match -> hard merge, conflict -> hard no-match
        # ----------------------------------------------------------
        govt_ids_incoming = [
            i for i in identifiers
            if i.identifier_type == "government_id_hash"
            and i.quality_flag == "valid"
        ]

        for govt_id in govt_ids_incoming:
            # If the candidate shares the same government ID hash -> hard merge
            result = tx.run(
                """
                MATCH (p:Person {person_id: $person_id})
                      -[rel:IDENTIFIED_BY]->(id:Identifier {
                          identifier_type: 'government_id_hash',
                          normalized_value: $normalized_value
                      })
                WHERE rel.is_active = true
                RETURN p.person_id AS person_id
                LIMIT 1
                """,
                person_id=candidate_person_id,
                normalized_value=govt_id.normalized_value,
            )
            record = result.single()
            if record:
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

            # Check if candidate has a *different* government ID -> hard no-match
            conflict_result = tx.run(
                """
                MATCH (p:Person {person_id: $person_id})
                      -[rel:IDENTIFIED_BY]->(id:Identifier {
                          identifier_type: 'government_id_hash'
                      })
                WHERE rel.is_active = true
                  AND id.normalized_value <> $normalized_value
                RETURN id.normalized_value AS conflicting_value
                LIMIT 1
                """,
                person_id=candidate_person_id,
                normalized_value=govt_id.normalized_value,
            )
            conflict_record = conflict_result.single()
            if conflict_record:
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

        # ----------------------------------------------------------
        # Trusted migration-map IDs: external_customer_id, membership_id
        # Namespace-scoped deterministic merges.
        # ----------------------------------------------------------
        trusted_id_types = ("external_customer_id", "membership_id")
        trusted_ids_incoming = [
            i for i in identifiers
            if i.identifier_type in trusted_id_types
            and i.quality_flag == "valid"
        ]

        for tid in trusted_ids_incoming:
            result = tx.run(
                """
                MATCH (p:Person {person_id: $person_id})
                      -[rel:IDENTIFIED_BY]->(id:Identifier {
                          identifier_type: $identifier_type,
                          normalized_value: $normalized_value
                      })
                WHERE rel.is_active = true
                RETURN p.person_id AS person_id
                LIMIT 1
                """,
                person_id=candidate_person_id,
                identifier_type=tid.identifier_type,
                normalized_value=tid.normalized_value,
            )
            record = result.single()
            if record:
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

        return None  # no deterministic signal

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
        """Heuristic scoring engine with conditional weighting.

        Compares the incoming record's features against the candidate
        person's graph data.  Returns a ``MatchResult`` when confidence
        falls into a decision band, or ``None`` to pass through.

        Confidence bands:
            >= 0.90  ->  MERGE (auto-merge)
            0.60-0.89 -> REVIEW
            < 0.60  ->  NO_MATCH (explicit, so evaluate() can collect it)
        """
        score = 0.0
        reasons: list[str] = []
        identifier_evidence = 0.0  # tracks positive evidence from identifiers
        _IDENTIFIER_CAP = 0.85  # prevent two unverified phones from auto-merge

        # ---- Fetch candidate's identifiers from graph --------------------
        cand_idents_result = tx.run(
            queries.FETCH_PERSON_IDENTIFIERS,
            person_id=candidate_person_id,
        )
        cand_idents: list[dict[str, Any]] = [dict(r) for r in cand_idents_result]

        cand_phones = {
            i["normalized_value"]: i for i in cand_idents
            if i["identifier_type"] == "phone"
        }
        cand_emails = {
            i["normalized_value"]: i for i in cand_idents
            if i["identifier_type"] == "email"
        }

        # ---- Fetch candidate's attribute facts ---------------------------
        cand_facts_result = tx.run(
            queries.FETCH_PERSON_FACTS,
            person_id=candidate_person_id,
        )
        cand_facts: list[dict[str, Any]] = [dict(r) for r in cand_facts_result]

        cand_names = [
            f["attribute_value"] for f in cand_facts
            if f["attribute_name"] in ("full_name", "preferred_name", "legal_name")
        ]
        cand_dobs = [
            f["attribute_value"] for f in cand_facts
            if f["attribute_name"] == "dob"
        ]

        # ---- Fetch candidate's addresses ---------------------------------
        cand_addrs_result = tx.run(
            queries.FETCH_PERSON_ADDRESSES,
            person_id=candidate_person_id,
        )
        cand_addrs: list[dict[str, Any]] = [dict(r) for r in cand_addrs_result]

        # ---- Phone comparison --------------------------------------------
        for ident in identifiers:
            if ident.identifier_type != "phone":
                continue
            if ident.normalized_value in cand_phones:
                cand_phone_rec = cand_phones[ident.normalized_value]
                # Check fanout (negative signal for shared phones)
                fanout_result = tx.run(
                    queries.CHECK_IDENTIFIER_FANOUT,
                    identifier_type="phone",
                    normalized_value=ident.normalized_value,
                )
                fanout_rec = fanout_result.single()
                fanout = fanout_rec["fanout"] if fanout_rec else 0

                if fanout > 5:
                    penalty = -0.25
                    score += penalty
                    reasons.append(
                        f"Phone {ident.normalized_value} seen on {fanout} persons (-0.25)"
                    )
                else:
                    is_verified = ident.is_verified and cand_phone_rec.get("is_verified")
                    weight = 0.35 if is_verified else 0.20
                    identifier_evidence += weight
                    reasons.append(
                        f"Phone match ({'verified' if is_verified else 'unverified'}: "
                        f"+{weight:.2f})"
                    )

        # ---- Email comparison --------------------------------------------
        for ident in identifiers:
            if ident.identifier_type != "email":
                continue
            if ident.normalized_value in cand_emails:
                cand_email_rec = cand_emails[ident.normalized_value]
                is_verified = ident.is_verified and cand_email_rec.get("is_verified")
                weight = 0.35 if is_verified else 0.20
                identifier_evidence += weight
                reasons.append(
                    f"Email match ({'verified' if is_verified else 'unverified'}: "
                    f"+{weight:.2f})"
                )

        # Apply identifier cap
        capped_identifier_evidence = min(identifier_evidence, _IDENTIFIER_CAP)
        if identifier_evidence > _IDENTIFIER_CAP:
            reasons.append(
                f"Identifier evidence capped from {identifier_evidence:.2f} "
                f"to {_IDENTIFIER_CAP}"
            )
        score += capped_identifier_evidence

        # ---- DOB comparison ----------------------------------------------
        incoming_dobs = [
            a.attribute_value for a in attributes if a.attribute_name == "dob"
        ]
        if incoming_dobs and cand_dobs:
            incoming_dob = incoming_dobs[0]
            if incoming_dob in cand_dobs:
                score += 0.25
                reasons.append("DOB exact match (+0.25)")
            else:
                score -= 0.30
                reasons.append(
                    f"DOB conflict: incoming={incoming_dob}, "
                    f"candidate={cand_dobs[0]} (-0.30)"
                )

        # ---- Name comparison ---------------------------------------------
        incoming_names = [
            a.attribute_value for a in attributes
            if a.attribute_name in ("full_name", "preferred_name", "legal_name")
        ]
        best_sim = 0.0
        if incoming_names and cand_names:
            for inc_name in incoming_names:
                for cand_name in cand_names:
                    sim = _jaro_winkler_similarity(inc_name, cand_name)
                    best_sim = max(best_sim, sim)

            if best_sim > 0.8:
                score += 0.20
                reasons.append(f"High name similarity ({best_sim:.2f}: +0.20)")
            elif best_sim >= 0.5:
                score += 0.10
                reasons.append(f"Medium name similarity ({best_sim:.2f}: +0.10)")
            elif best_sim < 0.3:
                score -= 0.25
                reasons.append(f"Strong name mismatch ({best_sim:.2f}: -0.25)")

        # ---- Address comparison ------------------------------------------
        if address and cand_addrs:
            incoming_full = address.normalized_full.lower().strip()
            for caddr in cand_addrs:
                cand_full = (caddr.get("normalized_full") or "").lower().strip()
                if incoming_full and incoming_full == cand_full:
                    score += 0.10
                    reasons.append("Address match (+0.10)")
                    break

        # ---- Build feature snapshot for audit ------------------------------
        features: dict[str, Any] = {
            "candidate_person_id": candidate_person_id,
            "phone_exact_match": any(
                r for r in reasons if r.startswith("Phone match")
            ),
            "email_exact_match": any(
                r for r in reasons if r.startswith("Email match")
            ),
            "dob_exact_match": any(
                r for r in reasons if r.startswith("DOB exact")
            ),
            "dob_conflict": any(
                r for r in reasons if r.startswith("DOB conflict")
            ),
            "name_similarity": best_sim if incoming_names and cand_names else None,
            "address_match": any(
                r for r in reasons if r.startswith("Address match")
            ),
            "identifier_evidence_raw": identifier_evidence,
            "identifier_evidence_capped": capped_identifier_evidence,
            "raw_score": score,
        }

        # ---- Clamp to [0, 1] --------------------------------------------
        confidence = max(0.0, min(1.0, score))

        logger.info(
            "Heuristic score for candidate %s: %.2f (raw=%.2f, reasons=%s)",
            candidate_person_id, confidence, score, reasons,
        )

        # ---- Apply confidence bands --------------------------------------
        if confidence >= 0.90:
            return MatchResult(
                decision=MatchDecision.MERGE,
                confidence=confidence,
                reasons=reasons,
                engine_type=EngineType.HEURISTIC,
                matched_person_id=candidate_person_id,
                feature_snapshot=features,
            )
        elif confidence >= 0.60:
            return MatchResult(
                decision=MatchDecision.REVIEW,
                confidence=confidence,
                reasons=reasons,
                engine_type=EngineType.HEURISTIC,
                matched_person_id=candidate_person_id,
                feature_snapshot=features,
            )
        else:
            return MatchResult(
                decision=MatchDecision.NO_MATCH,
                confidence=confidence,
                reasons=reasons,
                engine_type=EngineType.HEURISTIC,
                matched_person_id=None,
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
# String similarity helpers (no external dependencies)
# ======================================================================

def _jaro_similarity(s1: str, s2: str) -> float:
    """Compute the Jaro similarity between two strings."""
    if s1 == s2:
        return 1.0
    len_s1, len_s2 = len(s1), len(s2)
    if len_s1 == 0 or len_s2 == 0:
        return 0.0

    match_distance = max(len_s1, len_s2) // 2 - 1
    if match_distance < 0:
        match_distance = 0

    s1_matches = [False] * len_s1
    s2_matches = [False] * len_s2

    matches = 0
    transpositions = 0

    for i in range(len_s1):
        start = max(0, i - match_distance)
        end = min(i + match_distance + 1, len_s2)
        for j in range(start, end):
            if s2_matches[j] or s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    k = 0
    for i in range(len_s1):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1

    jaro = (
        matches / len_s1
        + matches / len_s2
        + (matches - transpositions / 2) / matches
    ) / 3.0
    return jaro


def _jaro_winkler_similarity(s1: str, s2: str, prefix_weight: float = 0.1) -> float:
    """Compute the Jaro-Winkler similarity between two strings.

    Case-insensitive.  No external dependencies.
    """
    s1 = s1.lower().strip()
    s2 = s2.lower().strip()

    jaro = _jaro_similarity(s1, s2)

    # Count common prefix (up to 4 characters)
    prefix_len = 0
    for i in range(min(len(s1), len(s2), 4)):
        if s1[i] == s2[i]:
            prefix_len += 1
        else:
            break

    return jaro + prefix_len * prefix_weight * (1.0 - jaro)
