"""Multi-match: a record matching two distinct persons links to all of them."""

from __future__ import annotations

from collections.abc import Iterator

from src.matching.engine import MatchEngine
from src.models import (
    CandidateResult,
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
    """Every queried candidate shares the incoming NRIC → deterministic merge."""

    def run(self, query: str, **params: object) -> _Result:
        if "MATCH (a:Person {person_id: $left_person_id})" in query:
            return _Result([{"is_locked": False}])
        # Candidate-side VALID govt-ID match — every candidate owns the NRIC.
        if "rel.quality_flag = 'valid'" in query and "person_id AS person_id" in query:
            return _Result([{"person_id": params.get("person_id")}])
        return _Result([])


def _nric() -> list[NormalizedIdentifier]:
    return [
        NormalizedIdentifier(
            identifier_type="nric",
            normalized_value="hash-shared",
            quality_flag=QualityFlag.VALID,
        )
    ]


def test_multi_match_picks_primary_and_lists_extra_links() -> None:
    result = MatchEngine().evaluate(
        _Tx(),  # type: ignore[arg-type]
        [CandidateResult(person_id="person-b"), CandidateResult(person_id="person-a")],
        _nric(),
        None,
        [],
        record_type=RecordType.SYSTEM,
    )

    assert result.decision == MatchDecision.MERGE
    # Primary is deterministic — confidence tie broken by person_id ascending.
    assert result.matched_person_id == "person-a"
    # The other matched person is recorded for linking, NOT merging.
    assert result.additional_linked_person_ids == ["person-b"]


def test_single_match_has_no_additional_links() -> None:
    result = MatchEngine().evaluate(
        _Tx(),  # type: ignore[arg-type]
        [CandidateResult(person_id="person-a")],
        _nric(),
        None,
        [],
        record_type=RecordType.SYSTEM,
    )

    assert result.decision == MatchDecision.MERGE
    assert result.matched_person_id == "person-a"
    assert result.additional_linked_person_ids == []
