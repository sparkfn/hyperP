from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

import pytest
from neo4j import AsyncManagedTransaction
from src.graph.queries import (
    CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED,
    REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
    REVERT_MERGE,
    REVERT_PERSON_PAIR_CASE_CLOSURES,
    REVERT_RECORD_PERSON_CASE_REDIRECTS,
)
from src.repositories.neo4j._merge_side_effects import (
    apply_merge_review_side_effects,
    revert_merge_review_side_effects,
)
from src.repositories.neo4j.merge import _unmerge_tx

# --- Query constant shape -------------------------------------------------


def test_close_query_targets_open_person_pair_cases() -> None:
    assert "cancelled_superseded" in CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED
    assert "closed_by_merge_event_id" in CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED
    assert "queue_state IN ['open', 'assigned', 'deferred']" in CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED


def test_redirect_query_rewires_about_right_for_record_cases() -> None:
    assert "ABOUT_RIGHT" in REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED
    assert "entity_type: 'source_record'" in REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED
    assert "redirected_by_merge_event_id" in REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED


def test_revert_queries_are_event_scoped_and_state_guarded() -> None:
    assert (
        "redirected_by_merge_event_id = $merge_event_id" in REVERT_RECORD_PERSON_CASE_REDIRECTS
    )
    assert "queue_state IN ['open', 'assigned', 'deferred']" in REVERT_RECORD_PERSON_CASE_REDIRECTS
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


class _RecordingTx:
    def __init__(self) -> None:
        self.calls: list[_Call] = []

    async def run(self, query: str, **params: ParamValue) -> _AsyncResult:
        self.calls.append(_Call(query=query, params=params))
        return _AsyncResult()


@pytest.mark.asyncio
async def test_apply_closes_then_redirects_with_event_stamp() -> None:
    tx = _RecordingTx()
    await apply_merge_review_side_effects(
        cast(AsyncManagedTransaction, tx), "merge-1", "person-a", "person-b"
    )
    assert [c.query for c in tx.calls] == [
        CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED,
        REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
    ]
    assert tx.calls[0].params == {"absorbed_id": "person-a", "merge_event_id": "merge-1"}
    assert tx.calls[1].params == {
        "absorbed_id": "person-a",
        "survivor_id": "person-b",
        "merge_event_id": "merge-1",
    }


@pytest.mark.asyncio
async def test_revert_reverts_redirects_then_closures() -> None:
    tx = _RecordingTx()
    await revert_merge_review_side_effects(cast(AsyncManagedTransaction, tx), "merge-1")
    assert [c.query for c in tx.calls] == [
        REVERT_RECORD_PERSON_CASE_REDIRECTS,
        REVERT_PERSON_PAIR_CASE_CLOSURES,
    ]
    assert all(c.params == {"merge_event_id": "merge-1"} for c in tx.calls)


# --- Unmerge wiring -------------------------------------------------------


@pytest.mark.asyncio
async def test_unmerge_reverts_review_side_effects() -> None:
    class _ScriptedResult:
        def __init__(self, record: Mapping[str, object] | None) -> None:
            self._record = record

        async def single(self) -> Mapping[str, object] | None:
            return self._record

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
            None,  # REVERT_PERSON_PAIR_CASE_CLOSURES
        ]
    )

    result = await _unmerge_tx(
        cast(AsyncManagedTransaction, tx), "merge-1", "oops", "admin@example.com"
    )

    assert result == ("person-a", "person-b")
    assert REVERT_RECORD_PERSON_CASE_REDIRECTS in tx.queries
    assert REVERT_PERSON_PAIR_CASE_CLOSURES in tx.queries
    # Revert runs after the graph unmerge is reverted.
    assert tx.queries.index(REVERT_MERGE) < tx.queries.index(REVERT_RECORD_PERSON_CASE_REDIRECTS)
