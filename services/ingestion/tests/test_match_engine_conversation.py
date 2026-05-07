"""Regression tests for conversation-sourced matching policy."""

from __future__ import annotations

from collections.abc import Iterator

from src.matching.engine import MatchEngine
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
    def __init__(self, *, phone_fanout: int = 1) -> None:
        self.phone_fanout = phone_fanout

    def run(self, query: str, **params: object) -> _Result:
        _ = params
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
                    },
                    {
                        "identifier_type": "email",
                        "normalized_value": "ada@example.com",
                        "is_verified": True,
                        "last_confirmed_at": None,
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
        if "RETURN count(p) AS fanout" in query:
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
    assert result.matched_person_id is None


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
