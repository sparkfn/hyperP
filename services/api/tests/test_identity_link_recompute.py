"""Unit coverage for the API-side link recompute and its C4 weight parity."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TypedDict, cast

import pytest
from neo4j import AsyncManagedTransaction
from src.graph.identity_link_recompute import (
    _aggregate,
    _Evidence,
    build_link_rows,
    recompute_person_has_identifier_links,
)
from src.graph.identity_link_weights import LINK_POLICY_VERSION
from src.graph.queries.identity_links import (
    FETCH_PERSON_ASSERTION_EVIDENCE,
    LOCK_PERSON_FOR_LINK_RECOMPUTE,
)


class _EmptyResult:
    async def single(self) -> None:
        return None

    def __aiter__(self) -> AsyncIterator[object]:
        return self

    async def __anext__(self) -> object:
        raise StopAsyncIteration


class _RecordingTransaction:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def run(self, query: str, **_params: object) -> _EmptyResult:
        self.queries.append(query)
        return _EmptyResult()


@pytest.mark.asyncio
async def test_recompute_takes_person_lock_before_reading_evidence() -> None:
    tx = _RecordingTransaction()

    await recompute_person_has_identifier_links(cast(AsyncManagedTransaction, tx), "person-1")

    assert tx.queries[0] == LOCK_PERSON_FOR_LINK_RECOMPUTE
    assert tx.queries.index(LOCK_PERSON_FOR_LINK_RECOMPUTE) < tx.queries.index(
        FETCH_PERSON_ASSERTION_EVIDENCE
    )
    # The recount runs before the rows are built, so the evidence is re-read
    # with fresh participant counts in the same pass.
    assert tx.queries.count(FETCH_PERSON_ASSERTION_EVIDENCE) == 2


def _evidence(
    source_record_pk: str,
    trust_tier: str | None,
    *,
    quality_flag: str | None = "valid",
    is_verified: bool | None = True,
    identifier_id: str = "ident-phone",
    identifier_type: str = "phone",
    normalized_value: str = "+6591234567",
    observed_at: str | None = "2026-01-01T00:00:00Z",
    extraction_confidence: float | None = None,
    record_type: str | None = "identity",
    link_status: str | None = "linked",
    participant_count: int | None = None,
) -> _Evidence:
    return _Evidence(
        source_record_pk=source_record_pk,
        identifier_id=identifier_id,
        identifier_type=identifier_type,
        identifier_scope="global",
        normalized_value=normalized_value,
        quality_flag=quality_flag,
        is_verified=is_verified,
        trust_tier=trust_tier,
        observed_at=observed_at,
        extraction_confidence=extraction_confidence,
        record_type=record_type,
        link_status=link_status,
        participant_count=participant_count,
    )


def test_aggregate_prefers_the_highest_trust_tier() -> None:
    rows, link_ids = build_link_rows(
        "person-1",
        [_evidence("sr-1", "tier_1"), _evidence("sr-2", "tier_4")],
    )

    assert rows[0]["max_tier"] == "tier_1"
    assert rows[0]["verified_count"] == 2
    assert link_ids == [rows[0]["link_id"]]


def test_aggregate_incomplete_metadata_falls_back_to_the_lowest_tier() -> None:
    incomplete = _evidence("sr-1", "tier_1", quality_flag=None, is_verified=None)
    rows, _link_ids = build_link_rows(
        "person-1",
        [incomplete, _evidence("sr-2", "tier_4")],
    )

    assert rows[0]["max_tier"] == "tier_4"
    assert rows[0]["verified_count"] == 1


def test_aggregate_with_only_incomplete_metadata_uses_the_lowest_tier() -> None:
    rows, _link_ids = build_link_rows(
        "person-1",
        [_evidence("sr-1", "tier_1", quality_flag=None, is_verified=None)],
    )

    assert rows[0]["max_tier"] == "tier_4"
    assert rows[0]["verified_count"] == 0


def test_build_link_rows_computes_c4_weights_with_per_type_reference() -> None:
    rows, _link_ids = build_link_rows(
        "person-1",
        [
            _evidence(
                "sr-old",
                "tier_3",
                is_verified=False,
                identifier_id="ident-old",
                normalized_value="+6591111111",
                observed_at="2026-01-01T00:00:00Z",
                participant_count=1,
            ),
            _evidence(
                "sr-new",
                "tier_1",
                is_verified=False,
                identifier_id="ident-new",
                normalized_value="+6592222222",
                observed_at="2027-01-01T00:00:00Z",
                participant_count=1,
            ),
            _evidence(
                "sr-mail",
                "tier_2",
                is_verified=False,
                identifier_id="ident-email",
                identifier_type="email",
                normalized_value="user@example.com",
                observed_at="2026-01-01T00:00:00Z",
                participant_count=3,
            ),
        ],
    )
    by_id = {str(row["identifier_id"]): row for row in rows}

    assert by_id["ident-old"]["score"] == 0.6
    assert by_id["ident-old"]["exclusivity"] == 1.0
    assert by_id["ident-old"]["rank_score"] == 0.3
    assert by_id["ident-old"]["policy_version"] == LINK_POLICY_VERSION
    assert by_id["ident-new"]["score"] == 1.0
    assert by_id["ident-new"]["rank_score"] == 1.0
    assert by_id["ident-email"]["score"] == 0.85
    assert by_id["ident-email"]["exclusivity"] == 0.333333
    assert by_id["ident-email"]["rank_score"] == 0.283333


class _VectorAssertion(TypedDict):
    source_record_pk: str
    trust_tier: str | None
    quality_flag: str | None
    is_verified: bool | None
    extraction_confidence: float | None
    record_type: str | None
    link_status: str | None
    observed_at: str | None


class _VectorExpected(TypedDict):
    score: float
    exclusivity: float
    rank_score: float


class _WeightVector(TypedDict):
    name: str
    identifier_type: str
    participant_count: int | None
    reference_at: str | None
    assertions: list[_VectorAssertion]
    expected: _VectorExpected


def _require_str(value: object, key: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"link weight vector field {key!r} must be a string")
    return value


def _optional_str(value: object, key: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ValueError(f"link weight vector field {key!r} must be a string or null")


def _optional_bool(value: object, key: str) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise ValueError(f"link weight vector field {key!r} must be a boolean or null")


def _optional_float(value: object, key: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"link weight vector field {key!r} must be a number or null")
    if isinstance(value, (int, float)):
        return float(value)
    raise ValueError(f"link weight vector field {key!r} must be a number or null")


def _optional_int(value: object, key: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"link weight vector field {key!r} must be an integer or null")
    if isinstance(value, int):
        return value
    raise ValueError(f"link weight vector field {key!r} must be an integer or null")


def _require_float(value: object, key: str) -> float:
    result = _optional_float(value, key)
    if result is None:
        raise ValueError(f"link weight vector field {key!r} must be a number")
    return result


def _field(entry: object, key: str) -> object:
    if not isinstance(entry, dict):
        raise ValueError("link weight vector entry must be an object")
    value: object = entry.get(key)
    return value


def _parse_assertion(entry: object) -> _VectorAssertion:
    return _VectorAssertion(
        source_record_pk=_require_str(_field(entry, "source_record_pk"), "source_record_pk"),
        trust_tier=_optional_str(_field(entry, "trust_tier"), "trust_tier"),
        quality_flag=_optional_str(_field(entry, "quality_flag"), "quality_flag"),
        is_verified=_optional_bool(_field(entry, "is_verified"), "is_verified"),
        extraction_confidence=_optional_float(
            _field(entry, "extraction_confidence"), "extraction_confidence"
        ),
        record_type=_optional_str(_field(entry, "record_type"), "record_type"),
        link_status=_optional_str(_field(entry, "link_status"), "link_status"),
        observed_at=_optional_str(_field(entry, "observed_at"), "observed_at"),
    )


def _parse_expected(entry: object) -> _VectorExpected:
    return _VectorExpected(
        score=_require_float(_field(entry, "score"), "score"),
        exclusivity=_require_float(_field(entry, "exclusivity"), "exclusivity"),
        rank_score=_require_float(_field(entry, "rank_score"), "rank_score"),
    )


def _parse_vector(entry: object) -> _WeightVector:
    raw_assertions = _field(entry, "assertions")
    if not isinstance(raw_assertions, list):
        raise ValueError("link weight vector needs an 'assertions' list")
    return _WeightVector(
        name=_require_str(_field(entry, "name"), "name"),
        identifier_type=_require_str(_field(entry, "identifier_type"), "identifier_type"),
        participant_count=_optional_int(_field(entry, "participant_count"), "participant_count"),
        reference_at=_optional_str(_field(entry, "reference_at"), "reference_at"),
        assertions=[_parse_assertion(item) for item in raw_assertions],
        expected=_parse_expected(_field(entry, "expected")),
    )


def _load_vectors() -> list[_WeightVector]:
    path = (
        Path(__file__).resolve().parents[2]
        / "ingestion"
        / "tests"
        / "fixtures"
        / "link_weight_vectors.json"
    )
    data: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("link weight vectors must be a JSON object")
    raw_vectors: object = data.get("vectors")
    if not isinstance(raw_vectors, list):
        raise ValueError("link weight vectors need a 'vectors' list")
    return [_parse_vector(entry) for entry in raw_vectors]


def test_shared_vectors_match_formula() -> None:
    """Drift guard: the API mirror reproduces the ingestion vectors exactly."""
    for vector in _load_vectors():
        rows = [
            _evidence(
                item["source_record_pk"],
                item["trust_tier"],
                quality_flag=item["quality_flag"],
                is_verified=item["is_verified"],
                identifier_type=vector["identifier_type"],
                observed_at=item["observed_at"],
                extraction_confidence=item["extraction_confidence"],
                record_type=item["record_type"],
                link_status=item["link_status"],
                participant_count=vector["participant_count"],
            )
            for item in vector["assertions"]
        ]
        aggregate = _aggregate(
            rows,
            participant_count=vector["participant_count"],
            reference_at=vector["reference_at"],
        )
        expected = vector["expected"]
        assert aggregate.score == expected["score"], vector["name"]
        assert aggregate.exclusivity == expected["exclusivity"], vector["name"]
        assert aggregate.rank_score == expected["rank_score"], vector["name"]
        assert aggregate.policy_version == LINK_POLICY_VERSION, vector["name"]
