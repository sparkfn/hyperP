"""Per-record-type confidence disposition policy."""

from src.matching.thresholds import classify_confidence
from src.models import MatchDecision, RecordType


def test_relationship_confidence_boundaries() -> None:
    assert classify_confidence(0.099, RecordType.RELATIONSHIP) == MatchDecision.NO_MATCH
    assert classify_confidence(0.10, RecordType.RELATIONSHIP) == MatchDecision.REVIEW
    assert classify_confidence(0.199, RecordType.RELATIONSHIP) == MatchDecision.REVIEW
    assert classify_confidence(0.20, RecordType.RELATIONSHIP) == MatchDecision.MERGE


def test_relationship_hard_conflict_vetoes_auto_merge() -> None:
    assert (
        classify_confidence(0.20, RecordType.RELATIONSHIP, has_hard_conflict=True)
        == MatchDecision.REVIEW
    )


def test_identity_retains_existing_confidence_boundaries() -> None:
    assert classify_confidence(0.199, RecordType.IDENTITY) == MatchDecision.NO_MATCH
    assert classify_confidence(0.20, RecordType.IDENTITY) == MatchDecision.REVIEW
    assert classify_confidence(0.399, RecordType.IDENTITY) == MatchDecision.REVIEW
    assert classify_confidence(0.40, RecordType.IDENTITY) == MatchDecision.MERGE


def test_identity_hard_conflict_does_not_change_existing_disposition() -> None:
    assert (
        classify_confidence(0.40, RecordType.IDENTITY, has_hard_conflict=True)
        == MatchDecision.MERGE
    )
