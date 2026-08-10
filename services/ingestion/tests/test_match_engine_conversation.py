"""Regression tests for conversation-sourced matching policy."""

from __future__ import annotations

from collections.abc import Iterator

from src.matching.engine import MatchEngine
from src.matching.heuristic import _can_promote_conversation
from src.models import (
    CandidateResult,
    MatchDecision,
    NormalizedAttribute,
    NormalizedIdentifier,
    QualityFlag,
    RecordType,
)


class _Result:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self._records = records

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._records)

    def single(self) -> dict[str, object] | None:
        return self._records[0] if self._records else None


class _Tx:
    def __init__(self, *, phone_fanout: int = 1, identifier_source_type: str = "system") -> None:
        self.phone_fanout = phone_fanout
        self.identifier_source_type = identifier_source_type
        self.fanout_calls = 0
        self.no_match_lock_prefetch_calls = 0

    def run(self, query: str, **params: object) -> _Result:
        _ = params
        if "UNWIND $candidate_inputs AS candidate_input" in query:
            self.no_match_lock_prefetch_calls += 1
            return _Result([])
        if "MATCH (a:Person {person_id: $left_person_id})" in query:
            return _Result([{"is_locked": False}])
        if "MATCH (p:Person {person_id: $person_id})-[rel:IDENTIFIED_BY]->" in query:
            return _Result(
                [
                    {
                        "identifier_type": "phone",
                        "normalized_value": "+6512345678",
                        "is_verified": True,
                        "last_confirmed_at": None,
                        "source_record_type": self.identifier_source_type,
                    },
                    {
                        "identifier_type": "email",
                        "normalized_value": "ada@example.com",
                        "is_verified": True,
                        "last_confirmed_at": None,
                        "source_record_type": self.identifier_source_type,
                    },
                ]
            )
        if "MATCH (p:Person {person_id: $person_id})-[f:HAS_FACT]->" in query:
            return _Result(
                [
                    {
                        "attribute_name": "full_name",
                        "attribute_value": "Ada Lovelace",
                        "source_trust_tier": 1,
                        "observed_at": None,
                        "quality_flag": "valid",
                    }
                ]
            )
        if "MATCH (p:Person {person_id: $person_id})-[rel:LIVES_AT]->" in query:
            return _Result([])
        if "AS fanout" in query:
            self.fanout_calls += 1
            return _Result([{"fanout": self.phone_fanout}])
        return _Result([])


def test_conversation_record_auto_merges_with_phone_email_and_high_name() -> None:
    result = MatchEngine().evaluate(
        _Tx(),  # type: ignore[arg-type]
        [CandidateResult(person_id="person-1")],
        [
            NormalizedIdentifier(
                identifier_type="phone",
                normalized_value="+6512345678",
                is_verified=False,
                quality_flag=QualityFlag.VALID,
            ),
            NormalizedIdentifier(
                identifier_type="email",
                normalized_value="ada@example.com",
                is_verified=False,
                quality_flag=QualityFlag.VALID,
            ),
        ],
        None,
        [
            NormalizedAttribute(
                attribute_name="full_name",
                attribute_value="Ada Lovelace",
                quality_flag=QualityFlag.VALID,
            )
        ],
        record_type=RecordType.CONVERSATION,
    )

    assert result.decision == MatchDecision.MERGE
    assert result.matched_person_id == "person-1"
    assert "Conversation evidence promoted to merge" in result.reasons
    assert result.feature_snapshot["conversation_promotion"] is True


def test_conversation_record_does_not_auto_merge_on_phone_only() -> None:
    result = MatchEngine().evaluate(
        _Tx(),  # type: ignore[arg-type]
        [CandidateResult(person_id="person-1")],
        [
            NormalizedIdentifier(
                identifier_type="phone",
                normalized_value="+6512345678",
                is_verified=False,
                quality_flag=QualityFlag.VALID,
            )
        ],
        None,
        [],
        record_type=RecordType.CONVERSATION,
    )

    assert result.decision != MatchDecision.MERGE
    assert result.decision == MatchDecision.REVIEW
    assert result.matched_person_id == "person-1"


def _corroborated_features(**overrides: object) -> dict[str, object]:
    """A conversation feature snapshot that would otherwise promote (phone+email)."""
    base: dict[str, object] = {
        "phone_exact_match": True,
        "email_exact_match": True,
        "phone_high_fanout": False,
        "dob_exact_match": False,
        "dob_conflict": False,
        "name_mismatch": False,
        "name_similarity": None,
        "address_match": False,
        "identifier_system_corroborated": True,
    }
    base.update(overrides)
    return base


def test_promotion_blocked_by_strong_name_mismatch() -> None:
    # Guard is tested directly: a strong name mismatch must veto promotion even
    # when an independent identifier pair (phone + email) corroborates.
    assert _can_promote_conversation(_corroborated_features()) is True
    assert _can_promote_conversation(_corroborated_features(name_mismatch=True)) is False


def test_promotion_blocked_by_dob_conflict_and_high_fanout() -> None:
    assert _can_promote_conversation(_corroborated_features(dob_conflict=True)) is False
    assert _can_promote_conversation(_corroborated_features(phone_high_fanout=True)) is False


def test_promotion_blocked_when_corroboration_is_not_system_sourced() -> None:
    # Exclusively conversation-sourced evidence must stay capped at review
    # (matching-spec): no independent non-conversation source corroborates.
    assert (
        _can_promote_conversation(_corroborated_features(identifier_system_corroborated=False))
        is False
    )


def test_conversation_record_does_not_auto_merge_on_conversation_only_evidence() -> None:
    """A conversation record matching a candidate whose identifiers are also
    conversation-sourced is exclusively conversation-sourced — cap at review."""
    result = MatchEngine().evaluate(
        _Tx(identifier_source_type="conversation"),  # type: ignore[arg-type]
        [CandidateResult(person_id="person-1")],
        [
            NormalizedIdentifier(
                identifier_type="phone",
                normalized_value="+6512345678",
                is_verified=False,
                quality_flag=QualityFlag.VALID,
            ),
            NormalizedIdentifier(
                identifier_type="email",
                normalized_value="ada@example.com",
                is_verified=False,
                quality_flag=QualityFlag.VALID,
            ),
        ],
        None,
        [
            NormalizedAttribute(
                attribute_name="full_name",
                attribute_value="Ada Lovelace",
                quality_flag=QualityFlag.VALID,
            )
        ],
        record_type=RecordType.CONVERSATION,
    )

    assert result.decision != MatchDecision.MERGE
    assert result.feature_snapshot["conversation_promotion"] is False
    assert result.feature_snapshot["identifier_system_corroborated"] is False


def test_conversation_record_does_not_auto_merge_high_fanout_phone() -> None:
    result = MatchEngine().evaluate(
        _Tx(phone_fanout=9),  # type: ignore[arg-type]
        [CandidateResult(person_id="person-1")],
        [
            NormalizedIdentifier(
                identifier_type="phone",
                normalized_value="+6512345678",
                is_verified=False,
                quality_flag=QualityFlag.VALID,
            ),
            NormalizedIdentifier(
                identifier_type="email",
                normalized_value="ada@example.com",
                is_verified=False,
                quality_flag=QualityFlag.VALID,
            ),
        ],
        None,
        [
            NormalizedAttribute(
                attribute_name="full_name",
                attribute_value="Ada Lovelace",
                quality_flag=QualityFlag.VALID,
            )
        ],
        record_type=RecordType.CONVERSATION,
    )

    assert result.decision != MatchDecision.MERGE


def test_match_engine_reuses_phone_fanout_across_candidates() -> None:
    tx = _Tx()
    MatchEngine().evaluate(
        tx,  # type: ignore[arg-type]
        [CandidateResult(person_id="person-1"), CandidateResult(person_id="person-2")],
        [
            NormalizedIdentifier(
                identifier_type="phone",
                normalized_value="+6512345678",
                is_verified=False,
                quality_flag=QualityFlag.VALID,
            ),
            NormalizedIdentifier(
                identifier_type="email",
                normalized_value="ada@example.com",
                is_verified=False,
                quality_flag=QualityFlag.VALID,
            ),
        ],
        None,
        [
            NormalizedAttribute(
                attribute_name="full_name",
                attribute_value="Ada Lovelace",
                quality_flag=QualityFlag.VALID,
            )
        ],
        record_type=RecordType.CONVERSATION,
    )

    assert tx.fanout_calls == 1


def test_match_engine_prefetches_no_match_locks_once_for_all_candidates() -> None:
    tx = _Tx()
    MatchEngine().evaluate(
        tx,  # type: ignore[arg-type]
        [CandidateResult(person_id="person-1"), CandidateResult(person_id="person-2")],
        [
            NormalizedIdentifier(
                identifier_type="phone",
                normalized_value="+6512345678",
                is_verified=False,
                quality_flag=QualityFlag.VALID,
            ),
            NormalizedIdentifier(
                identifier_type="email",
                normalized_value="ada@example.com",
                is_verified=False,
                quality_flag=QualityFlag.VALID,
            ),
        ],
        None,
        [],
        record_type=RecordType.CONVERSATION,
    )

    assert tx.no_match_lock_prefetch_calls == 1
