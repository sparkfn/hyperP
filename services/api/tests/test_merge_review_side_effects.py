from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

import pytest
from neo4j import AsyncManagedTransaction
from src.graph.queries import (
    CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED,
    REDIRECT_PERSON_PAIR_CASES_ABSORBED_LEFT,
    REDIRECT_PERSON_PAIR_CASES_ABSORBED_RIGHT,
    REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
    REVERT_MERGE,
    REVERT_PERSON_PAIR_CASE_CLOSURES,
    REVERT_PERSON_PAIR_REDIRECTS_LEFT,
    REVERT_PERSON_PAIR_REDIRECTS_RIGHT,
    REVERT_RECORD_PERSON_CASE_REDIRECTS,
)
from src.repositories.neo4j._merge_side_effects import (
    apply_merge_review_side_effects,
    revert_merge_review_side_effects,
)
from src.repositories.neo4j.merge import _unmerge_tx

# --- Query constant shape -------------------------------------------------


def test_close_query_targets_only_absorbed_survivor_pair() -> None:
    assert "cancelled_superseded" in CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED
    assert "closed_by_merge_event_id" in CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED
    assert "queue_state IN ['open', 'assigned', 'deferred']" in CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED
    assert "$survivor_id" in CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED


def test_redirect_pair_queries_repoint_absorbed_side() -> None:
    for q, side in [
        (REDIRECT_PERSON_PAIR_CASES_ABSORBED_LEFT, "ABOUT_LEFT"),
        (REDIRECT_PERSON_PAIR_CASES_ABSORBED_RIGHT, "ABOUT_RIGHT"),
    ]:
        assert side in q
        assert "redirected_pair_by_merge_event_id" in q
        assert "redirected_pair_side" in q
        assert "$survivor_id" in q
        assert "queue_state IN ['open', 'assigned', 'deferred']" in q


def test_redirect_query_rewires_about_right_for_record_cases() -> None:
    assert "ABOUT_RIGHT" in REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED
    assert "entity_type: 'source_record'" in REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED
    assert "redirected_by_merge_event_id" in REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED


def test_revert_queries_are_event_scoped_and_state_guarded() -> None:
    assert "redirected_by_merge_event_id = $merge_event_id" in REVERT_RECORD_PERSON_CASE_REDIRECTS
    assert "queue_state IN ['open', 'assigned', 'deferred']" in REVERT_RECORD_PERSON_CASE_REDIRECTS
    for q, side in [
        (REVERT_PERSON_PAIR_REDIRECTS_LEFT, "'left'"),
        (REVERT_PERSON_PAIR_REDIRECTS_RIGHT, "'right'"),
    ]:
        assert "redirected_pair_by_merge_event_id = $merge_event_id" in q
        assert side in q
        assert "queue_state IN ['open', 'assigned', 'deferred']" in q
    assert "closed_by_merge_event_id = $merge_event_id" in REVERT_PERSON_PAIR_CASE_CLOSURES
    assert "rc.queue_state = 'cancelled'" in REVERT_PERSON_PAIR_CASE_CLOSURES


# --- Helper dispatch ------------------------------------------------------

type ParamValue = str | None


@dataclass(frozen=True)
class _Call:
    query: str
    params: Mapping[str, ParamValue]


class _AsyncResult:
    async def single(self) -> None:
        return None

    def __aiter__(self) -> AsyncIterator[None]:
        return self

    async def __anext__(self) -> None:
        raise StopAsyncIteration


class _RecordingTx:
    def __init__(self) -> None:
        self.calls: list[_Call] = []

    async def run(self, query: str, **params: ParamValue) -> _AsyncResult:
        self.calls.append(_Call(query=query, params=params))
        return _AsyncResult()


@pytest.mark.asyncio
async def test_apply_closes_moot_then_redirects_pair_then_record() -> None:
    tx = _RecordingTx()
    result = await apply_merge_review_side_effects(
        cast(AsyncManagedTransaction, tx), "merge-1", "person-a", "person-b"
    )
    assert [c.query for c in tx.calls] == [
        CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED,
        REDIRECT_PERSON_PAIR_CASES_ABSORBED_LEFT,
        REDIRECT_PERSON_PAIR_CASES_ABSORBED_RIGHT,
        REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
    ]
    assert result == []
    pair_params = {
        "absorbed_id": "person-a",
        "survivor_id": "person-b",
        "merge_event_id": "merge-1",
    }
    assert tx.calls[0].params == pair_params
    assert tx.calls[1].params == pair_params
    assert tx.calls[2].params == pair_params
    assert tx.calls[3].params == pair_params


@pytest.mark.asyncio
async def test_revert_reverts_record_then_pair_redirects_then_closures() -> None:
    tx = _RecordingTx()
    result = await revert_merge_review_side_effects(cast(AsyncManagedTransaction, tx), "merge-1")
    assert [c.query for c in tx.calls] == [
        REVERT_RECORD_PERSON_CASE_REDIRECTS,
        REVERT_PERSON_PAIR_REDIRECTS_LEFT,
        REVERT_PERSON_PAIR_REDIRECTS_RIGHT,
        REVERT_PERSON_PAIR_CASE_CLOSURES,
    ]
    assert all(c.params == {"merge_event_id": "merge-1"} for c in tx.calls)
    assert result == []


# --- Unmerge wiring -------------------------------------------------------


@pytest.mark.asyncio
async def test_unmerge_reverts_review_side_effects() -> None:
    class _ScriptedResult:
        def __init__(self, record: Mapping[str, object] | None) -> None:
            self._record = record

        async def single(self) -> Mapping[str, object] | None:
            return self._record

        def __aiter__(self) -> AsyncIterator[None]:
            return self

        async def __anext__(self) -> None:
            raise StopAsyncIteration

    class _ScriptedTx:
        def __init__(self, records: Sequence[Mapping[str, object] | None]) -> None:
            self._records = list(records)
            self.queries: list[str] = []

        async def run(self, query: str, **params: object) -> _ScriptedResult:
            self.queries.append(query)
            record = self._records.pop(0) if self._records else None
            return _ScriptedResult(record)

    tx = _ScriptedTx(
        [
            {"absorbed_id": "person-a", "survivor_id": "person-b"},  # GET_UNMERGE_TARGET
            {"removed_count": 1, "current_survivor_id": "person-b"},  # REVERT_MERGE
            None,  # CREATE_UNMERGE_AUDIT
            None,  # FLAG_AFFECTED_RECORDS_FOR_REVIEW
            None,  # REVERT_RECORD_PERSON_CASE_REDIRECTS
            None,  # REVERT_PERSON_PAIR_REDIRECTS_LEFT
            None,  # REVERT_PERSON_PAIR_REDIRECTS_RIGHT
            None,  # REVERT_PERSON_PAIR_CASE_CLOSURES
        ]
    )

    result = await _unmerge_tx(
        cast(AsyncManagedTransaction, tx), "merge-1", "oops", "admin@example.com"
    )

    assert result is not None
    assert result.absorbed_id == "person-a"
    assert result.current_survivor_id == "person-b"
    assert result.reverted_review_case_ids == []
    assert REVERT_RECORD_PERSON_CASE_REDIRECTS in tx.queries
    assert REVERT_PERSON_PAIR_REDIRECTS_LEFT in tx.queries
    assert REVERT_PERSON_PAIR_REDIRECTS_RIGHT in tx.queries
    assert REVERT_PERSON_PAIR_CASE_CLOSURES in tx.queries
    assert tx.queries.index(REVERT_MERGE) < tx.queries.index(REVERT_RECORD_PERSON_CASE_REDIRECTS)
