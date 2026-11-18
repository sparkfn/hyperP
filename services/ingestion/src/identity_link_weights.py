"""C4 link-weight formula over immutable C1 ``ASSERTS`` evidence.

Pure helpers implementing the weight contract: per-assertion strength
``s(a)``, noisy-OR link weight with a dormant candidate-only cap,
exclusivity from the maintained ``Identifier.participant_count`` counter,
and evidence-time-anchored decay. No Cypher and no I/O here; aggregation
lives in :mod:`src.identity_links` and the transaction writer in
:mod:`src.identity_link_writes`.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from src.identity_registry import identifier_policy

#: Policy version stamped on links carrying C4 computed weights.
LINK_POLICY_VERSION = "c4-w1"

#: Per-assertion tier strength keyed by tier NAME.
TIER_WEIGHTS: dict[str, float] = {
    "tier_1": 1.0,
    "tier_2": 0.85,
    "tier_3": 0.60,
    "tier_4": 0.35,
}

#: Conversation records drop one trust tier; ``tier_4`` stays put.
_TIER_DROP: dict[str, str] = {
    "tier_1": "tier_2",
    "tier_2": "tier_3",
    "tier_3": "tier_4",
    "tier_4": "tier_4",
}

#: Verified assertions carry a 1.15 multiplier.
VERIFIED_BOOST = 1.15

#: Candidate-only evidence (``link_status == 'pending_review'``) is capped,
#: and dormant in production: the evidence query still excludes pending_review
#: rows, so no candidate-only evidence reaches links today.
CANDIDATE_ONLY_CAP = 0.24

#: Record status marking candidate-only (unconfirmed) evidence.
CANDIDATE_LINK_STATUS = "pending_review"

#: Record type whose assertions drop one trust tier.
CONVERSATION_RECORD_TYPE = "conversation"


def tier_weight(
    trust_tier: str | None,
    *,
    record_type: str | None,
    complete: bool,
) -> float:
    """Return the tier strength for one assertion.

    A missing tier or incomplete assertion metadata falls back to ``tier_4``;
    conversation records then drop one tier further.
    """
    if complete and isinstance(trust_tier, str) and trust_tier in TIER_WEIGHTS:
        effective = trust_tier
    else:
        effective = "tier_4"
    if record_type == CONVERSATION_RECORD_TYPE:
        effective = _TIER_DROP[effective]
    return TIER_WEIGHTS[effective]


def quality_weight(quality_flag: str | None) -> float:
    """Return the parse-quality multiplier: 1.0 when valid, else 0.6."""
    if quality_flag == "valid":
        return 1.0
    return 0.6


def confidence_weight(extraction_confidence: float | None) -> float:
    """Return the extraction-confidence multiplier, defaulting to 1.0."""
    if extraction_confidence is None:
        return 1.0
    return min(1.0, max(0.0, extraction_confidence))


def assertion_score(
    *,
    trust_tier: str | None,
    quality_flag: str | None,
    is_verified: bool | None,
    complete: bool,
    extraction_confidence: float | None,
    record_type: str | None,
) -> float:
    """Return the strength ``s(a)`` of one assertion, bounded to [0, 1]."""
    boost = VERIFIED_BOOST if complete and is_verified is True else 1.0
    raw = (
        tier_weight(trust_tier, record_type=record_type, complete=complete)
        * quality_weight(quality_flag)
        * confidence_weight(extraction_confidence)
        * boost
    )
    return min(1.0, raw)


def noisy_or(values: Iterable[float]) -> float:
    """Return the noisy-OR (``1 - prod(1 - s)``) of assertion strengths."""
    product = 1.0
    for value in values:
        product *= 1.0 - value
    return 1.0 - product


def combine_weights(
    confirmed: Iterable[float],
    candidate: Iterable[float],
) -> float:
    """Combine confirmed and candidate-only strengths into one link weight.

    Candidate-only evidence is capped at ``CANDIDATE_ONLY_CAP`` and can only
    ever raise the confirmed weight, never reduce it.
    """
    weight_confirmed = noisy_or(confirmed)
    weight_candidate = min(CANDIDATE_ONLY_CAP, noisy_or(candidate))
    return 1.0 - (1.0 - weight_confirmed) * (1.0 - weight_candidate)


def exclusivity_for_count(participant_count: int | None) -> float:
    """Return ``1 / max(1, participant_count)``; missing counts assume 1."""
    if participant_count is None or participant_count < 1:
        return 1.0
    return 1.0 / participant_count


def half_life_for(identifier_type: str) -> int | None:
    """Return the decay half-life in days, or ``None`` if the type never ages."""
    return identifier_policy(identifier_type).half_life_days


def parse_evidence_time(value: str | None) -> datetime | None:
    """Parse an evidence ISO-8601 time to aware UTC; ``None`` when unusable."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def age_days_between(earlier: str | None, later: str | None) -> float | None:
    """Return ``(later - earlier)`` in days; ``None`` when either is unusable."""
    start = parse_evidence_time(earlier)
    end = parse_evidence_time(later)
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds() / 86400.0)


def decay_factor(age_days: float | None, half_life_days: int | None) -> float:
    """Return the ``0.5 ** (age / half_life)`` factor, or 1.0 when undecayed."""
    if age_days is None or half_life_days is None or half_life_days <= 0:
        return 1.0
    exponent: float = age_days / half_life_days
    factor: float = 0.5**exponent
    return factor


def rank_score(
    *,
    weight: float,
    exclusivity: float,
    age_days: float | None,
    half_life_days: int | None,
) -> float:
    """Return the decayed rank for one link."""
    return weight * exclusivity * decay_factor(age_days, half_life_days)


def round6(value: float) -> float:
    """Round a stored weight to 6 decimal places for stable comparisons."""
    return round(value, 6)


def max_evidence_time(values: Iterable[str | None]) -> str | None:
    """Return the latest usable evidence time, preserving its original text."""
    best: str | None = None
    best_at: datetime | None = None
    for value in values:
        parsed = parse_evidence_time(value)
        if parsed is None:
            continue
        if best_at is None or parsed > best_at:
            best_at = parsed
            best = value
    return best
