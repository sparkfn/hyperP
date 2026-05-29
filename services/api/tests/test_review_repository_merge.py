from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

import pytest
from neo4j import AsyncManagedTransaction
from src.graph.queries import (
    CHECK_BOTH_PERSONS_ACTIVE,
    CHECK_NO_MATCH_LOCK,
    CREATE_NO_MATCH_LOCK_FROM_REVIEW,
    EXECUTE_MANUAL_MERGE,
    GET_PERSONS_FOR_REVIEW_MERGE,
)
from src.repositories.neo4j.review import _action_tx
from src.repositories.protocols.merge import GoldenProfileSelection
from src.types import ApiReviewActionType

type ReviewCaseRecord = dict[str, str | None]
type RecordValue = str | bool | None | ReviewCaseRecord | list[GoldenProfileSelection]
type Record = Mapping[str, RecordValue]
type Params = Mapping[str, RecordValue]


class _AsyncResult:
    def __init__(self, record: Record | None) -> None:
        self._record = record

    async def single(self) -> Record | None:
        return self._record


@dataclass(frozen=True)
class _Call:
    query: str
    params: Params


class _Tx:
    def __init__(self, records: Sequence[Record | None]) -> None:
        self._records: list[Record | None] = list(records)
        self.calls: list[_Call] = []

    async def run(self, query: str, **params: RecordValue) -> _AsyncResult:
        self.calls.append(_Call(query=query, params=params))
        record = self._records.pop(0) if self._records else None
        return _AsyncResult(record)


@pytest.mark.asyncio
async def test_review_merge_uses_requested_survivor_person() -> None:
    tx = _Tx(
        [
            {"left_person_id": "person-a", "right_person_id": "person-b"},
            {"absorbed": "person-a", "survivor": "person-b"},
            {"is_locked": False},
            {
                "review_case": {
                    "review_case_id": "case-1",
                    "queue_state": "resolved",
                    "resolution": "merge",
                }
            },
            {"merge_event_id": "merge-1"},
        ]
    )

    result = await _action_tx(
        cast(AsyncManagedTransaction, tx),
        "case-1",
        ApiReviewActionType.MERGE.value,
        "resolved",
        "merge",
        "same person",
        None,
        "reviewer@example.com",
        "person-b",
        [],
    )

    assert result == {
        "review_case_id": "case-1",
        "queue_state": "resolved",
        "resolution": "merge",
        "survivor_person_id": "person-b",
        "golden_profile_selections": [],
    }
    assert [call.query for call in tx.calls[:3]] == [
        GET_PERSONS_FOR_REVIEW_MERGE,
        CHECK_BOTH_PERSONS_ACTIVE,
        CHECK_NO_MATCH_LOCK,
    ]
    assert tx.calls[-1].query == EXECUTE_MANUAL_MERGE
    assert tx.calls[-1].params == {
        "from_id": "person-a",
        "to_id": "person-b",
        "reason": "same person",
        "actor_id": "reviewer@example.com",
    }


@pytest.mark.asyncio
async def test_review_merge_returns_survivor_and_golden_profile_selections() -> None:
    selections: list[GoldenProfileSelection] = [
        {
            "field_name": "preferred_nric",
            "source_kind": "identifier",
            "selected_value": "S1234567A",
            "source_record_pk": "sr-1",
            "identifier_type": "nric",
        }
    ]
    tx = _Tx(
        [
            {"left_person_id": "person-a", "right_person_id": "person-b"},
            {"absorbed": "person-a", "survivor": "person-b"},
            {"is_locked": False},
            {
                "review_case": {
                    "review_case_id": "case-1",
                    "queue_state": "resolved",
                    "resolution": "merge",
                }
            },
            {"merge_event_id": "merge-1"},
        ]
    )

    result = await _action_tx(
        cast(AsyncManagedTransaction, tx),
        "case-1",
        ApiReviewActionType.MERGE.value,
        "resolved",
        "merge",
        "same person",
        None,
        "reviewer@example.com",
        "person-b",
        selections,
    )

    assert result == {
        "review_case_id": "case-1",
        "queue_state": "resolved",
        "resolution": "merge",
        "survivor_person_id": "person-b",
        "golden_profile_selections": selections,
    }

    tx = _Tx(
        [
            {"left_person_id": "person-a", "right_person_id": "person-b"},
            {"absorbed": "person-a", "survivor": "person-b"},
            {"is_locked": True},
        ]
    )

    result = await _action_tx(
        cast(AsyncManagedTransaction, tx),
        "case-1",
        ApiReviewActionType.MERGE.value,
        "resolved",
        "merge",
        "same person",
        None,
        "reviewer@example.com",
        "person-b",
        [],
    )

    assert result == {"merge_blocked": True}
    assert [call.query for call in tx.calls] == [
        GET_PERSONS_FOR_REVIEW_MERGE,
        CHECK_BOTH_PERSONS_ACTIVE,
        CHECK_NO_MATCH_LOCK,
    ]


@pytest.mark.asyncio
async def test_review_merge_rejects_survivor_outside_review_pair() -> None:
    tx = _Tx([{"left_person_id": "person-a", "right_person_id": "person-b"}])

    result = await _action_tx(
        cast(AsyncManagedTransaction, tx),
        "case-1",
        ApiReviewActionType.MERGE.value,
        "resolved",
        "merge",
        "same person",
        None,
        "reviewer@example.com",
        "person-c",
        [],
    )

    assert result == {"merge_not_applicable": True}
    assert [call.query for call in tx.calls] == [GET_PERSONS_FOR_REVIEW_MERGE]


@pytest.mark.asyncio
async def test_manual_no_match_creates_review_lock_after_action() -> None:
    tx = _Tx(
        [
            {
                "review_case": {
                    "review_case_id": "case-1",
                    "queue_state": "resolved",
                    "resolution": "manual_no_match",
                }
            },
            None,
        ]
    )

    result = await _action_tx(
        cast(AsyncManagedTransaction, tx),
        "case-1",
        ApiReviewActionType.MANUAL_NO_MATCH.value,
        "resolved",
        "manual_no_match",
        "not the same person",
        None,
        "reviewer@example.com",
        None,
        [],
    )

    assert result == {
        "review_case_id": "case-1",
        "queue_state": "resolved",
        "resolution": "manual_no_match",
    }
    assert tx.calls[-1].query == CREATE_NO_MATCH_LOCK_FROM_REVIEW
    assert tx.calls[-1].params == {
        "review_case_id": "case-1",
        "notes": "not the same person",
        "actor_id": "reviewer@example.com",
    }
