"""Regression: system-family matching, with the per-record-type divergences.

`identity`, `bankruptcy`, `relationship`, and `crm_deal` make up the system
family (they replaced the former single `system` record type). They share the same
deterministic NRIC merge regardless of name. The one deliberate divergence
(Spec 2) is pinned here: `relationship` adds a Layer-2 phone + partial-name
auto-merge promotion. `identity` and `bankruptcy` keep the plain additive
behaviour and are identical in both the deterministic and heuristic layers.
"""

from __future__ import annotations

from collections.abc import Iterator

from src.matching.deterministic import evaluate_deterministic
from src.matching.engine import MatchEngine
from src.models import (
    SYSTEM_FAMILY,
    CandidateResult,
    MatchDecision,
    MatchResult,
    NormalizedAttribute,
    NormalizedIdentifier,
    QualityFlag,
    RecordType,
)

_FAMILY = (
    RecordType.IDENTITY,
    RecordType.BANKRUPTCY,
    RecordType.RELATIONSHIP,
    RecordType.CRM_DEAL,
)


def test_system_family_membership_includes_person_capable_crm_deals() -> None:
    assert SYSTEM_FAMILY == frozenset(_FAMILY)
    assert RecordType.CONVERSATION not in SYSTEM_FAMILY
    assert RecordType.SALES not in SYSTEM_FAMILY
    assert RecordType.RENTAL_FLAT not in SYSTEM_FAMILY
    assert RecordType.CRM_HISTORY not in SYSTEM_FAMILY
    assert RecordType.CALL not in SYSTEM_FAMILY


class _Result:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self._records = records

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._records)

    def single(self) -> dict[str, object] | None:
        return self._records[0] if self._records else None


class _NricTx:
    """Fake tx where candidate person-1 has a VALID matching NRIC edge.

    Mirrors the query shape this branch's deterministic govt-ID check uses
    (see ``test_deterministic_govt_id``): the candidate's NRIC edge must be
    VALID-quality for a hard merge to fire.
    """

    def run(self, query: str, **_params: object) -> _Result:
        if "rel.quality_flag = 'valid'" in query and "person_id AS person_id" in query:
            return _Result([{"person_id": "person-1"}])
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


def _assert_all_equal(results: list[MatchResult]) -> None:
    first = results[0]
    for other in results[1:]:
        assert other.decision == first.decision
        assert other.confidence == first.confidence
        assert other.reasons == first.reasons
        assert other.engine_type == first.engine_type
        assert other.matched_person_id == first.matched_person_id
        assert other.is_new_person == first.is_new_person


def test_deterministic_nric_merge_identical_across_system_family() -> None:
    tx = _NricTx()
    results = [
        evaluate_deterministic(tx, "person-1", _nric(), [], rt)  # type: ignore[arg-type]
        for rt in _FAMILY
    ]
    assert all(r is not None for r in results)
    non_null = [r for r in results if r is not None]
    _assert_all_equal(non_null)
    assert non_null[0].decision == MatchDecision.MERGE
    assert non_null[0].matched_person_id == "person-1"


def test_bankruptcy_merges_on_nric_regardless_of_name_conflict() -> None:
    tx = _NricTx()
    res = evaluate_deterministic(
        tx,  # type: ignore[arg-type]
        "person-1",
        _nric(),
        [
            NormalizedAttribute(
                attribute_name="full_name",
                attribute_value="Kok Pin",
                quality_flag=QualityFlag.VALID,
            )
        ],
        RecordType.BANKRUPTCY,
    )
    assert res is not None
    assert res.decision == MatchDecision.MERGE


def test_non_system_family_does_not_deterministically_merge_on_nric() -> None:
    tx = _NricTx()
    for rt in (RecordType.CONVERSATION, RecordType.SALES):
        assert evaluate_deterministic(tx, "person-1", _nric(), [], rt) is None  # type: ignore[arg-type]


class _HeuristicTx:
    """Fake tx providing candidate snapshot data for the heuristic layer."""

    def run(self, query: str, **params: object) -> _Result:
        _ = params
        if "MATCH (a:Person {person_id: $left_person_id})" in query:
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
        if "[f:HAS_FACT]->" in query:
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
        if "[rel:LIVES_AT]->" in query:
            return _Result([])
        if "RETURN count(p) AS fanout" in query:
            return _Result([{"fanout": 1}])
        return _Result([])


def _evaluate(record_type: RecordType) -> MatchResult:
    return MatchEngine().evaluate(
        _HeuristicTx(),  # type: ignore[arg-type]
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
        record_type=record_type,
    )


def test_heuristic_identity_and_bankruptcy_identical_relationship_promotes() -> None:
    # identity and bankruptcy score identically in Layer 2 (they are identical
    # across both the deterministic and heuristic layers).
    identity = _evaluate(RecordType.IDENTITY)
    bankruptcy = _evaluate(RecordType.BANKRUPTCY)
    _assert_all_equal([identity, bankruptcy])
    assert "Conversation evidence promoted to merge" not in identity.reasons

    # relationship adds a phone + partial-name promotion, so it diverges to MERGE.
    relationship = _evaluate(RecordType.RELATIONSHIP)
    assert relationship.decision == MatchDecision.MERGE
    assert any("promot" in r.lower() for r in relationship.reasons)
