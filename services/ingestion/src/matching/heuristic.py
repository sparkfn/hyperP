"""Layer 2 heuristic scoring — conditional weights across phone/email/DOB/name/address.

Confidence bands:
    ≥ 0.90 → MERGE  (auto-merge)
    0.60–0.89 → REVIEW
    < 0.60 → NO_MATCH (explicit, so the orchestrator can drop it)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

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
    RecordType,
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
# Calibrated to the Jaro-Winkler distribution, not an absolute "30% similar":
# unrelated name pairs floor around 0.35–0.49 (JW credits length + shared
# prefix), while same-person variants (Bob/Robert ≈ 0.50, Li Wei/Wei Li ≈ 0.67,
# typos ≈ 0.85+) sit at/above 0.50. A strict ``< 0.50`` cutoff fires the
# strong-mismatch penalty for clearly different names without catching variants.
# A false mismatch only routes to review (never a false merge), so erring toward
# the higher cutoff is precision-favoring.
NAME_MISMATCH_THRESHOLD = 0.50
NAME_MISMATCH_PENALTY = -0.25
ADDRESS_MATCH_WEIGHT = 0.10

CONFIDENCE_AUTO_MERGE = 0.90
CONFIDENCE_REVIEW = 0.60
CONVERSATION_PROMOTED_CONFIDENCE = 0.91
CONVERSATION_PROMOTION_NAME_THRESHOLD = 0.80


@dataclass
class HeuristicSignals:
    """Structured scoring signals collected while scoring a candidate pair.

    Scorers set these fields directly; the persisted feature snapshot and the
    conversation-promotion gate read from them. This replaces deriving feature
    flags by parsing human-readable reason strings — the merge-gating logic must
    not depend on log wording.

    ``identifier_system_corroborated`` is True when at least one matched
    identifier is backed by a non-conversation source record on the candidate
    side, so a pair whose evidence is *exclusively* conversation-sourced stays
    capped at the review band (matching-spec: exclusively-conversation pairs
    cannot auto-merge).
    """

    phone_exact_match: bool = False
    phone_high_fanout: bool = False
    email_exact_match: bool = False
    dob_exact_match: bool = False
    dob_conflict: bool = False
    address_match: bool = False
    name_similarity: float | None = None
    name_mismatch: bool = False
    identifier_evidence_raw: float = 0.0
    identifier_system_corroborated: bool = False


def evaluate_heuristic(
    tx: ManagedTransaction,
    candidate_person_id: str,
    identifiers: list[NormalizedIdentifier],
    address: NormalizedAddress | None,
    attributes: list[NormalizedAttribute],
    record_type: RecordType = RecordType.IDENTITY,
) -> MatchResult:
    """Conditional-weight heuristic scoring across phone/email/DOB/name/address."""
    snapshot = fetch_candidate_snapshot(tx, candidate_person_id)

    score = 0.0
    reasons: list[str] = []
    signals = HeuristicSignals()

    raw_ident_evidence = _score_identifiers(tx, identifiers, snapshot, reasons, signals)
    signals.identifier_evidence_raw = raw_ident_evidence
    score += _cap_identifier_evidence(raw_ident_evidence, reasons)
    score += _score_dob(attributes, snapshot, reasons, signals)
    best_name_sim = _score_name(attributes, snapshot)
    score += _score_name_band(best_name_sim, attributes, snapshot, reasons, signals)
    score += _score_address(address, snapshot, reasons, signals)

    confidence = max(0.0, min(1.0, score))
    features = _build_feature_snapshot(candidate_person_id, signals, raw_score=score)
    confidence = _promote_conversation_confidence(record_type, confidence, reasons, features)

    logger.info(
        "Heuristic score for candidate %s: %.2f (raw=%.2f, reasons=%s)",
        candidate_person_id,
        confidence,
        score,
        reasons,
    )
    return _band(confidence, reasons, candidate_person_id, features)


def _is_system_sourced(cand_rec: dict[str, object]) -> bool:
    """True when a candidate identifier edge is backed by a non-conversation source.

    Conservative: unknown / unresolvable provenance is treated as *not*
    system-sourced, so a conversation record cannot be promoted to auto-merge on
    evidence we cannot confirm is independent of conversation extraction.
    """
    src_type = cand_rec.get("source_record_type")
    return isinstance(src_type, str) and src_type != RecordType.CONVERSATION.value


def _score_identifiers(
    tx: ManagedTransaction,
    identifiers: list[NormalizedIdentifier],
    snapshot: CandidateSnapshot,
    reasons: list[str],
    signals: HeuristicSignals,
) -> float:
    """Phone + email match scoring; returns raw (uncapped) identifier evidence.

    The match weight and the high-fanout penalty are *additive* (matching-spec
    weight table): a shared-but-matching phone is weak positive evidence, never
    a net negative versus no match at all.
    """
    evidence = 0.0
    cand_phones = snapshot.phones_by_value()
    cand_emails = snapshot.emails_by_value()

    for ident in identifiers:
        if ident.identifier_type == "phone" and ident.normalized_value in cand_phones:
            cand_rec = cand_phones[ident.normalized_value]
            verified = bool(ident.is_verified and cand_rec.get("is_verified"))
            weight = PHONE_VERIFIED_WEIGHT if verified else PHONE_UNVERIFIED_WEIGHT
            evidence += weight
            signals.phone_exact_match = True
            signals.identifier_system_corroborated = (
                signals.identifier_system_corroborated or _is_system_sourced(cand_rec)
            )
            reasons.append(
                f"Phone match ({'verified' if verified else 'unverified'}: +{weight:.2f})"
            )
            fanout_rec = tx.run(
                queries.CHECK_IDENTIFIER_FANOUT,
                identifier_type="phone",
                normalized_value=ident.normalized_value,
            ).single()
            fanout = int(fanout_rec["fanout"]) if fanout_rec else 0
            if fanout > PHONE_FANOUT_PENALTY_THRESHOLD:
                evidence += PHONE_FANOUT_PENALTY
                signals.phone_high_fanout = True
                reasons.append(
                    f"Phone {ident.normalized_value} seen on {fanout} persons "
                    f"({PHONE_FANOUT_PENALTY:+.2f})"
                )

        elif ident.identifier_type == "email" and ident.normalized_value in cand_emails:
            cand_rec = cand_emails[ident.normalized_value]
            verified = bool(ident.is_verified and cand_rec.get("is_verified"))
            weight = EMAIL_VERIFIED_WEIGHT if verified else EMAIL_UNVERIFIED_WEIGHT
            evidence += weight
            signals.email_exact_match = True
            signals.identifier_system_corroborated = (
                signals.identifier_system_corroborated or _is_system_sourced(cand_rec)
            )
            reasons.append(
                f"Email match ({'verified' if verified else 'unverified'}: +{weight:.2f})"
            )
    return evidence


def _cap_identifier_evidence(raw: float, reasons: list[str]) -> float:
    capped = min(raw, IDENTIFIER_EVIDENCE_CAP)
    if raw > IDENTIFIER_EVIDENCE_CAP:
        reasons.append(f"Identifier evidence capped from {raw:.2f} to {IDENTIFIER_EVIDENCE_CAP}")
    return capped


def _score_dob(
    attributes: list[NormalizedAttribute],
    snapshot: CandidateSnapshot,
    reasons: list[str],
    signals: HeuristicSignals,
) -> float:
    incoming_dobs = [a.attribute_value for a in attributes if a.attribute_name == "dob"]
    cand_dobs = snapshot.dobs()
    if not incoming_dobs or not cand_dobs:
        return 0.0
    incoming = incoming_dobs[0]
    if incoming in cand_dobs:
        signals.dob_exact_match = True
        reasons.append(f"DOB exact match (+{DOB_MATCH_WEIGHT:.2f})")
        return DOB_MATCH_WEIGHT
    signals.dob_conflict = True
    reasons.append(
        f"DOB conflict: incoming={incoming}, candidate={cand_dobs[0]} ({DOB_CONFLICT_PENALTY:+.2f})"
    )
    return DOB_CONFLICT_PENALTY


def _score_name(
    attributes: list[NormalizedAttribute],
    snapshot: CandidateSnapshot,
) -> float:
    """Return the best name similarity in [0, 1]; reasons appended by _score_name_band."""
    incoming = [
        a.attribute_value
        for a in attributes
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
    signals: HeuristicSignals,
) -> float:
    # Only apply when both sides actually had names — otherwise best_sim is 0.
    has_incoming = any(
        a.attribute_name in ("full_name", "preferred_name", "legal_name") for a in attributes
    )
    if not has_incoming or not snapshot.names():
        return 0.0
    signals.name_similarity = best_sim
    if best_sim > NAME_HIGH_THRESHOLD:
        reasons.append(f"High name similarity ({best_sim:.2f}: +{NAME_HIGH_WEIGHT:.2f})")
        return NAME_HIGH_WEIGHT
    if best_sim >= NAME_MEDIUM_THRESHOLD:
        reasons.append(f"Medium name similarity ({best_sim:.2f}: +{NAME_MEDIUM_WEIGHT:.2f})")
        return NAME_MEDIUM_WEIGHT
    if best_sim < NAME_MISMATCH_THRESHOLD:
        signals.name_mismatch = True
        reasons.append(f"Strong name mismatch ({best_sim:.2f}: {NAME_MISMATCH_PENALTY:+.2f})")
        return NAME_MISMATCH_PENALTY
    return 0.0


def _score_address(
    address: NormalizedAddress | None,
    snapshot: CandidateSnapshot,
    reasons: list[str],
    signals: HeuristicSignals,
) -> float:
    if address is None or not snapshot.addrs:
        return 0.0
    incoming_full = address.normalized_full.lower().strip()
    if not incoming_full:
        return 0.0
    for caddr in snapshot.addrs:
        cand_full = str(caddr.get("normalized_full") or "").lower().strip()
        if incoming_full == cand_full:
            signals.address_match = True
            reasons.append(f"Address match (+{ADDRESS_MATCH_WEIGHT:.2f})")
            return ADDRESS_MATCH_WEIGHT
    return 0.0


def _build_feature_snapshot(
    candidate_person_id: str,
    signals: HeuristicSignals,
    *,
    raw_score: float,
) -> dict[str, JsonValue]:
    """Build the persisted feature snapshot from structured signals (not reasons)."""
    return {
        "candidate_person_id": candidate_person_id,
        "phone_exact_match": signals.phone_exact_match,
        "phone_high_fanout": signals.phone_high_fanout,
        "email_exact_match": signals.email_exact_match,
        "dob_exact_match": signals.dob_exact_match,
        "dob_conflict": signals.dob_conflict,
        "name_similarity": signals.name_similarity,
        # Strong name mismatch is a hard conflict signal (matching-spec
        # "Conflict signals") — both sides had names and they barely match.
        "name_mismatch": signals.name_mismatch,
        "address_match": signals.address_match,
        # True when a matched identifier is backed by a non-conversation source
        # on the candidate side (independent corroboration for promotion).
        "identifier_system_corroborated": signals.identifier_system_corroborated,
        "identifier_evidence_raw": signals.identifier_evidence_raw,
        "identifier_evidence_capped": min(signals.identifier_evidence_raw, IDENTIFIER_EVIDENCE_CAP),
        "raw_score": raw_score,
        "conversation_promotion": False,
    }


def _promote_conversation_confidence(
    record_type: RecordType,
    confidence: float,
    reasons: list[str],
    features: dict[str, JsonValue],
) -> float:
    if record_type != RecordType.CONVERSATION:
        return confidence
    if confidence >= CONFIDENCE_AUTO_MERGE:
        return confidence
    if not _can_promote_conversation(features):
        return confidence
    features["conversation_promotion"] = True
    features["pre_promotion_confidence"] = confidence
    reasons.append("Conversation evidence promoted to merge")
    return CONVERSATION_PROMOTED_CONFIDENCE


def _can_promote_conversation(features: dict[str, JsonValue]) -> bool:
    phone_exact = features["phone_exact_match"] is True
    email_exact = features["email_exact_match"] is True
    high_name = (
        _float_feature(features.get("name_similarity")) >= CONVERSATION_PROMOTION_NAME_THRESHOLD
    )
    address_match = features["address_match"] is True
    dob_exact = features["dob_exact_match"] is True
    dob_conflict = features["dob_conflict"] is True
    name_mismatch = features["name_mismatch"] is True
    high_fanout_phone = features["phone_high_fanout"] is True
    system_corroborated = features["identifier_system_corroborated"] is True
    has_identifier = phone_exact or email_exact
    corroborated = (phone_exact and email_exact) or (
        has_identifier and (high_name or address_match or dob_exact)
    )
    # Conversation evidence may auto-merge only when (a) corroborated by an
    # independent identifier, (b) that identifier is backed by a non-conversation
    # source on the candidate side — a pair whose evidence is *exclusively*
    # conversation-sourced must stay in review (matching-spec) — and (c) no hard
    # conflict signal is present (strong name mismatch, DOB conflict, or
    # high-fanout phone all block promotion).
    return (
        corroborated
        and system_corroborated
        and not dob_conflict
        and not name_mismatch
        and not high_fanout_phone
    )


def _float_feature(value: JsonValue | None) -> float:
    if isinstance(value, int | float):
        return float(value)
    return 0.0


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
