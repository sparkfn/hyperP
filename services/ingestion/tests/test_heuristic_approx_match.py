"""Approximate (near-miss) phone/email scoring — Track B heuristic integration."""

from __future__ import annotations

from collections.abc import Iterator

from src.matching.heuristic import evaluate_heuristic
from src.models import MatchDecision, NormalizedIdentifier, QualityFlag, RecordType


class _Result:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self._records = records

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._records)

    def single(self) -> dict[str, object] | None:
        return self._records[0] if self._records else None


class _Tx:
    """Candidate has phone +6591234567 and email ada@gmail.com (both system-sourced)."""

    def run(self, query: str, **_params: object) -> _Result:
        if "[rel:IDENTIFIED_BY]->" in query:
            return _Result(
                [
                    {
                        "identifier_type": "phone",
                        "normalized_value": "+6591234567",
                        "is_verified": False,
                        "last_confirmed_at": None,
                        "source_record_type": "identity",
                    },
                    {
                        "identifier_type": "email",
                        "normalized_value": "ada@gmail.com",
                        "is_verified": False,
                        "last_confirmed_at": None,
                        "source_record_type": "identity",
                    },
                ]
            )
        if "[f:HAS_FACT]->" in query:
            return _Result([])
        if "[rel:LIVES_AT]->" in query:
            return _Result([])
        if "AS fanout" in query:
            return _Result([{"fanout": 1}])
        return _Result([])


def test_phone_near_miss_scores_small_positive_signal() -> None:
    # Incoming +6591234568 is a 1-digit typo of the candidate's +6591234567.
    result = evaluate_heuristic(
        _Tx(),  # type: ignore[arg-type]
        "person-1",
        [
            NormalizedIdentifier(
                identifier_type="phone",
                normalized_value="+6591234568",
                is_verified=False,
                quality_flag=QualityFlag.VALID,
            )
        ],
        None,
        [],
        record_type=RecordType.IDENTITY,
    )

    assert result.feature_snapshot["phone_approx_match"] is True
    assert result.feature_snapshot["phone_exact_match"] is False
    assert result.decision == MatchDecision.NO_MATCH
    assert any("near-match" in r.lower() for r in result.reasons)


def test_email_near_miss_scores_small_positive_signal() -> None:
    # Incoming ada@gmial.com is a known-domain typo of the candidate's ada@gmail.com.
    result = evaluate_heuristic(
        _Tx(),  # type: ignore[arg-type]
        "person-1",
        [
            NormalizedIdentifier(
                identifier_type="email",
                normalized_value="ada@gmial.com",
                is_verified=False,
                quality_flag=QualityFlag.VALID,
            )
        ],
        None,
        [],
        record_type=RecordType.IDENTITY,
    )

    assert result.feature_snapshot["email_approx_match"] is True
    assert result.feature_snapshot["email_exact_match"] is False
    assert result.decision == MatchDecision.NO_MATCH


def test_conversation_record_with_only_approx_phone_does_not_promote() -> None:
    # Approximate evidence alone must never enable conversation promotion —
    # phone_exact_match stays False, so _can_promote_conversation's
    # has_identifier check fails regardless of the approx signal.
    result = evaluate_heuristic(
        _Tx(),  # type: ignore[arg-type]
        "person-1",
        [
            NormalizedIdentifier(
                identifier_type="phone",
                normalized_value="+6591234568",
                is_verified=False,
                quality_flag=QualityFlag.VALID,
            )
        ],
        None,
        [],
        record_type=RecordType.CONVERSATION,
    )

    assert result.decision != MatchDecision.MERGE
    assert result.feature_snapshot["conversation_promotion"] is False


def test_exact_match_takes_precedence_over_approx() -> None:
    # When the incoming phone exactly matches, no approximate scoring runs for
    # that identifier — phone_approx_match stays False.
    result = evaluate_heuristic(
        _Tx(),  # type: ignore[arg-type]
        "person-1",
        [
            NormalizedIdentifier(
                identifier_type="phone",
                normalized_value="+6591234567",
                is_verified=False,
                quality_flag=QualityFlag.VALID,
            )
        ],
        None,
        [],
        record_type=RecordType.IDENTITY,
    )

    assert result.feature_snapshot["phone_exact_match"] is True
    assert result.feature_snapshot["phone_approx_match"] is False
