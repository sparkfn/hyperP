"""Bankruptcy NRIC merge is gated on a partial name when both sides are named."""

from __future__ import annotations

from collections.abc import Iterator

from src.matching.deterministic import evaluate_deterministic
from src.models import (
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
    """Candidate person-1 has a VALID matching NRIC; full_name fact configurable."""

    def __init__(self, candidate_name: str | None) -> None:
        self._candidate_name = candidate_name

    def run(self, query: str, **_params: object) -> _Result:
        if "rel.quality_flag = 'valid'" in query and "person_id AS person_id" in query:
            return _Result([{"person_id": "person-1"}])
        if "conflicting_value" in query:
            return _Result([])
        if "owner_person_id" in query:
            return _Result([])
        if "is_locked" in query:
            return _Result([{"is_locked": False}])
        if "[f:HAS_FACT]->" in query:
            if self._candidate_name is None:
                return _Result([])
            return _Result(
                [
                    {
                        "attribute_name": "full_name",
                        "attribute_value": self._candidate_name,
                        "source_trust_tier": 1,
                        "observed_at": None,
                        "quality_flag": "valid",
                    }
                ]
            )
        return _Result([])


def _nric() -> list[NormalizedIdentifier]:
    return [
        NormalizedIdentifier(
            identifier_type="nric",
            normalized_value="S1234567A",
            is_verified=True,
            quality_flag=QualityFlag.VALID,
        )
    ]


def _name(value: str) -> list[NormalizedAttribute]:
    return [
        NormalizedAttribute(
            attribute_name="full_name", attribute_value=value, quality_flag=QualityFlag.VALID
        )
    ]


def test_bankruptcy_merges_on_nric_plus_partial_name() -> None:
    res = evaluate_deterministic(
        _Tx("Ada Lovelace"),  # type: ignore[arg-type]
        "person-1",
        _nric(),
        _name("Ada Lovelace"),
        RecordType.BANKRUPTCY,
    )
    assert res is not None and res.decision == MatchDecision.MERGE


def test_bankruptcy_merges_on_nric_when_no_incoming_name() -> None:
    res = evaluate_deterministic(
        _Tx("Ada Lovelace"),  # type: ignore[arg-type]
        "person-1",
        _nric(),
        [],
        RecordType.BANKRUPTCY,
    )
    assert res is not None and res.decision == MatchDecision.MERGE


def test_bankruptcy_blocks_nric_merge_on_name_conflict() -> None:
    # "Ada Lovelace" vs "Kok Pin" -> Jaro-Winkler 0.317 < 0.50.
    res = evaluate_deterministic(
        _Tx("Kok Pin"),  # type: ignore[arg-type]
        "person-1",
        _nric(),
        _name("Ada Lovelace"),
        RecordType.BANKRUPTCY,
    )
    assert res is None  # falls through to heuristic


def test_identity_still_merges_on_nric_with_conflicting_name() -> None:
    res = evaluate_deterministic(
        _Tx("Kok Pin"),  # type: ignore[arg-type]
        "person-1",
        _nric(),
        _name("Ada Lovelace"),
        RecordType.IDENTITY,
    )
    assert res is not None and res.decision == MatchDecision.MERGE
