"""Select match dispositions from confidence and record context."""

from collections.abc import Mapping

from src.models import JsonValue, MatchDecision, RecordType

DEFAULT_AUTO_MERGE: float = 0.40
DEFAULT_REVIEW: float = 0.20
RELATIONSHIP_AUTO_MERGE: float = 0.20
RELATIONSHIP_REVIEW: float = 0.10


def thresholds_for(record_type: RecordType) -> tuple[float, float]:
    """Return ``(auto_merge, review)`` thresholds for a record type."""
    if record_type == RecordType.RELATIONSHIP:
        return RELATIONSHIP_AUTO_MERGE, RELATIONSHIP_REVIEW
    return DEFAULT_AUTO_MERGE, DEFAULT_REVIEW


def has_hard_conflict(features: Mapping[str, JsonValue]) -> bool:
    """Return whether structured signals veto an automatic merge."""
    return any(
        features.get(key) is True for key in ("dob_conflict", "name_mismatch", "phone_high_fanout")
    )


def classify_confidence(
    confidence: float,
    record_type: RecordType,
    *,
    has_hard_conflict: bool = False,
) -> MatchDecision:
    """Classify confidence using the policy for ``record_type``."""
    auto_merge, review = thresholds_for(record_type)
    relationship_conflict = record_type == RecordType.RELATIONSHIP and has_hard_conflict
    if confidence >= auto_merge and not relationship_conflict:
        return MatchDecision.MERGE
    if confidence >= review:
        return MatchDecision.REVIEW
    return MatchDecision.NO_MATCH
