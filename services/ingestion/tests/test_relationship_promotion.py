"""Relationship records auto-merge on phone + partial name, blocked by conflicts."""

from __future__ import annotations

from collections.abc import Iterator

from src.matching.engine import MatchEngine
from src.models import (
    CandidateResult,
    MatchDecision,
    MatchResult,
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
    def __init__(
        self, *, cand_name: str = "Ada Lovelace", cand_dob: str | None = None, fanout: int = 1
    ) -> None:
        self._cand_name = cand_name
        self._cand_dob = cand_dob
        self._fanout = fanout

    def run(self, query: str, **_params: object) -> _Result:
        if "is_locked" in query:
            return _Result([{"is_locked": False}])
        if "owner_person_id" in query:
            return _Result([])
        if "AS person_id" in query and "LIMIT 1" in query:
            return _Result([])
        if "conflicting_value" in query:
            return _Result([])
        if "[rel:IDENTIFIED_BY]->" in query:
            return _Result(
                [
                    {
                        "identifier_type": "phone",
                        "normalized_value": "+6512345678",
                        "is_verified": False,
                        "last_confirmed_at": None,
                    }
                ]
            )
        if "[f:HAS_FACT]->" in query:
            facts: list[dict[str, object]] = [
                {
                    "attribute_name": "full_name",
                    "attribute_value": self._cand_name,
                    "source_trust_tier": 1,
                    "observed_at": None,
                    "quality_flag": "valid",
                }
            ]
            if self._cand_dob is not None:
                facts.append(
                    {
                        "attribute_name": "dob",
                        "attribute_value": self._cand_dob,
                        "source_trust_tier": 1,
                        "observed_at": None,
                        "quality_flag": "valid",
                    }
                )
            return _Result(facts)
        if "[rel:LIVES_AT]->" in query:
            return _Result([])
        if "AS fanout" in query:
            return _Result([{"fanout": self._fanout}])
        return _Result([])


def _evaluate(tx: _Tx, *, record_type: RecordType, incoming_dob: str | None = None) -> MatchResult:
    attrs = [
        NormalizedAttribute(
            attribute_name="full_name", attribute_value="Ada Lovelace", quality_flag=QualityFlag.VALID
        )
    ]
    if incoming_dob is not None:
        attrs.append(
            NormalizedAttribute(
                attribute_name="dob", attribute_value=incoming_dob, quality_flag=QualityFlag.VALID
            )
        )
    return MatchEngine().evaluate(
        tx,  # type: ignore[arg-type]
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
        attrs,
        record_type=record_type,
    )


def test_relationship_promotes_on_phone_plus_name() -> None:
    res = _evaluate(_Tx(), record_type=RecordType.RELATIONSHIP)
    assert res.decision == MatchDecision.MERGE
    assert any("promot" in r.lower() for r in res.reasons)


def test_identity_not_promoted_same_inputs() -> None:
    res = _evaluate(_Tx(), record_type=RecordType.IDENTITY)
    assert res.decision != MatchDecision.MERGE


def test_relationship_blocked_by_dob_conflict() -> None:
    res = _evaluate(
        _Tx(cand_dob="1980-02-02"), record_type=RecordType.RELATIONSHIP, incoming_dob="1990-01-01"
    )
    assert res.decision != MatchDecision.MERGE


def test_relationship_blocked_by_high_fanout_phone() -> None:
    res = _evaluate(_Tx(fanout=9), record_type=RecordType.RELATIONSHIP)
    assert res.decision != MatchDecision.MERGE


def test_relationship_blocked_by_name_mismatch() -> None:
    # "Ada Lovelace" vs "Kok Pin" -> Jaro-Winkler 0.317 < 0.50.
    res = _evaluate(_Tx(cand_name="Kok Pin"), record_type=RecordType.RELATIONSHIP)
    assert res.decision != MatchDecision.MERGE
