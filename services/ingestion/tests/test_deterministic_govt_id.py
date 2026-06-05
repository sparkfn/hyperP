"""Government-ID hard-rule symmetry: candidate-side NRIC edge must be VALID.

An incoming VALID NRIC must not hard-merge (confidence 1.0) against a candidate
whose only matching NRIC edge is low-quality (e.g. ``partial_parse``).
"""

from __future__ import annotations

from collections.abc import Iterator

from src.matching.deterministic import evaluate_deterministic
from src.models import (
    MatchDecision,
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
    def __init__(self, *, candidate_has_valid_nric: bool) -> None:
        self.candidate_has_valid_nric = candidate_has_valid_nric

    def run(self, query: str, **params: object) -> _Result:
        _ = params
        # Only the VALID-gated govt-ID query should match a candidate edge.
        if "rel.quality_flag = 'valid'" in query and "person_id AS person_id" in query:
            if self.candidate_has_valid_nric:
                return _Result([{"person_id": "person-1"}])
            return _Result([])
        # No locks, no conflicts.
        return _Result([])


def _nric() -> list[NormalizedIdentifier]:
    return [
        NormalizedIdentifier(
            identifier_type="nric",
            normalized_value="hash-abc",
            quality_flag=QualityFlag.VALID,
        )
    ]


def test_valid_candidate_nric_hard_merges() -> None:
    result = evaluate_deterministic(
        _Tx(candidate_has_valid_nric=True),  # type: ignore[arg-type]
        "person-1",
        _nric(),
        RecordType.IDENTITY,
    )
    assert result is not None
    assert result.decision == MatchDecision.MERGE


def test_low_quality_candidate_nric_does_not_hard_merge() -> None:
    result = evaluate_deterministic(
        _Tx(candidate_has_valid_nric=False),  # type: ignore[arg-type]
        "person-1",
        _nric(),
        RecordType.IDENTITY,
    )
    # Falls through to heuristic scoring instead of an unsafe confidence-1.0 merge.
    assert result is None
