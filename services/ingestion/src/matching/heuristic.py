"""Layer 2 heuristic scoring — conditional weights across phone/email/DOB/name/address.

Confidence bands:
    ≥ 0.90 → MERGE  (auto-merge)
    0.60–0.89 → REVIEW
    < 0.60 → NO_MATCH (explicit, so the orchestrator can drop it)
"""

from __future__ import annotations

import logging

from neo4j import ManagedTransaction

from src.graph import queries
from src.matching.similarity import jaro_winkler_similarity
from src.matching.snapshot import CandidateSnapshot, fetch_candidate_snapshot
from src.models import (
    EngineType,
    JsonValue,
    MatchDecision,
    MatchResult,
    NormalizedAddress,
    NormalizedAttribute,
    NormalizedIdentifier,
)

logger = logging.getLogger(__name__)


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


def evaluate_heuristic(
    tx: ManagedTransaction,
    candidate_person_id: str,
    identifiers: list[NormalizedIdentifier],
    address: NormalizedAddress | None,
    attributes: list[NormalizedAttribute],
) -> MatchResult:
    """Conditional-weight heuristic scoring across phone/email/DOB/name/address."""
    snapshot = fetch_candidate_snapshot(tx, candidate_person_id)

    score = 0.0
    reasons: list[str] = []

    ident_evidence = _score_identifiers(tx, identifiers, snapshot, reasons)
    score += _cap_identifier_evidence(ident_evidence, reasons)
    score += _score_dob(attributes, snapshot, reasons)
    best_name_sim = _score_name(attributes, snapshot)
    score += _score_name_band(best_name_sim, attributes, snapshot, reasons)
    score += _score_address(address, snapshot, reasons)

    confidence = max(0.0, min(1.0, score))
    features = _build_feature_snapshot(
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
    return _band(confidence, reasons, candidate_person_id, features)


def _score_identifiers(
    tx: ManagedTransaction,
    identifiers: list[NormalizedIdentifier],
    snapshot: CandidateSnapshot,
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


def _cap_identifier_evidence(raw: float, reasons: list[str]) -> float:
    capped = min(raw, IDENTIFIER_EVIDENCE_CAP)
    if raw > IDENTIFIER_EVIDENCE_CAP:
        reasons.append(
            f"Identifier evidence capped from {raw:.2f} to {IDENTIFIER_EVIDENCE_CAP}"
        )
    return capped


def _score_dob(
    attributes: list[NormalizedAttribute],
    snapshot: CandidateSnapshot,
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


def _score_name(
    attributes: list[NormalizedAttribute],
    snapshot: CandidateSnapshot,
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


def _score_name_band(
    best_sim: float,
    attributes: list[NormalizedAttribute],
    snapshot: CandidateSnapshot,
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


def _score_address(
    address: NormalizedAddress | None,
    snapshot: CandidateSnapshot,
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


def _build_feature_snapshot(
    *,
    candidate_person_id: str,
    reasons: list[str],
    best_name_sim: float,
    attributes: list[NormalizedAttribute],
    snapshot: CandidateSnapshot,
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


def _band(
    confidence: float,
    reasons: list[str],
    candidate_person_id: str,
    features: dict[str, JsonValue],
) -> MatchResult:
    if confidence >= CONFIDENCE_AUTO_MERGE:
        decision = MatchDecision.MERGE
        matched: str | None = candidate_person_id
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
