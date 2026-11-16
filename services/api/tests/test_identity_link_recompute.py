"""Pure aggregation checks for the API HAS_IDENTIFIER maintenance."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from src.graph.identity_link_recompute import (
    build_link_rows,
    recompute_person_has_identifier_links,
)
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

    await recompute_person_has_identifier_links(tx, "person-1")  # type: ignore[arg-type]

    assert tx.queries[0] == LOCK_PERSON_FOR_LINK_RECOMPUTE
    assert tx.queries.index(LOCK_PERSON_FOR_LINK_RECOMPUTE) < tx.queries.index(
        FETCH_PERSON_ASSERTION_EVIDENCE
    )


def _evidence(
    source_record_pk: str,
    trust_tier: str | None,
    *,
    quality_flag: str | None = "valid",
    is_verified: bool | None = True,
) -> dict[str, object]:
    return {
        "source_record_pk": source_record_pk,
        "identifier_id": "ident-phone",
        "identifier_type": "phone",
        "identifier_scope": "global",
        "normalized_value": "+6591234567",
        "quality_flag": quality_flag,
        "is_verified": is_verified,
        "trust_tier": trust_tier,
        "observed_at": "2026-01-01T00:00:00Z",
    }


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
