from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

import pytest
from neo4j import AsyncManagedTransaction
from src.graph.queries import (
    CHECK_BOTH_PERSONS_ACTIVE,
    CHECK_NO_MATCH_LOCK,
    CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED,
    CREATE_NO_MATCH_LOCK_FROM_REVIEW,
    EXECUTE_MANUAL_MERGE,
    GET_PERSONS_FOR_REVIEW_MERGE,
    LINK_REVIEW_SALES_BOUGHT_UNIT,
    LINK_REVIEW_SALES_PURCHASED_ORDER,
    MARK_REVIEW_SALES_RECORD_LINKED,
    MARK_REVIEW_SALES_RECORD_UNRESOLVED,
    REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
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
    merge_call = next(c for c in tx.calls if c.query == EXECUTE_MANUAL_MERGE)
    assert merge_call.params == {
        "from_id": "person-a",
        "to_id": "person-b",
        "reason": "same person",
        "actor_id": "reviewer@example.com",
    }
    # Merge side-effects run after the merge, scoped to the merge event.
    assert [c.query for c in tx.calls[-2:]] == [
        CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED,
        REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
    ]
    assert tx.calls[-1].params == {
        "absorbed_id": "person-a",
        "survivor_id": "person-b",
        "merge_event_id": "merge-1",
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
    assert tx.calls[-2].query == CREATE_NO_MATCH_LOCK_FROM_REVIEW
    assert tx.calls[-2].params == {
        "review_case_id": "case-1",
        "notes": "not the same person",
        "actor_id": "reviewer@example.com",
    }
    assert tx.calls[-1].query == MARK_REVIEW_SALES_RECORD_UNRESOLVED


@pytest.mark.asyncio
async def test_merge_sales_link_approves_and_links() -> None:
    """MERGE on a sales review case (no person pair) links Order+Units and returns ActionResult."""
    tx = _Tx(
        [
            None,  # GET_PERSONS_FOR_REVIEW_MERGE → no person pair → sales path
            None,  # LINK_REVIEW_SALES_PURCHASED_ORDER (result not used)
            None,  # LINK_REVIEW_SALES_BOUGHT_UNIT (result not used)
            {"source_record_pk": "sr-42"},  # MARK_REVIEW_SALES_RECORD_LINKED → success
            {
                "review_case": {
                    "review_case_id": "rc-sales",
                    "queue_state": "resolved",
                    "resolution": "merge",
                }
            },
        ]
    )

    result = await _action_tx(
        cast(AsyncManagedTransaction, tx),
        "rc-sales",
        ApiReviewActionType.MERGE.value,
        "resolved",
        "merge",
        None,
        None,
        "reviewer@example.com",
        None,
        [],
    )

    assert result == {
        "review_case_id": "rc-sales",
        "queue_state": "resolved",
        "resolution": "merge",
    }
    query_seq = [c.query for c in tx.calls]
    assert query_seq[0] == GET_PERSONS_FOR_REVIEW_MERGE
    assert query_seq[1] == LINK_REVIEW_SALES_PURCHASED_ORDER
    assert query_seq[2] == LINK_REVIEW_SALES_BOUGHT_UNIT
    assert query_seq[3] == MARK_REVIEW_SALES_RECORD_LINKED


@pytest.mark.asyncio
async def test_merge_returns_not_applicable_when_no_persons_and_no_sales_link() -> None:
    """MERGE with no person pair and no sales SourceRecord yields merge_not_applicable."""
    tx = _Tx(
        [
            None,  # GET_PERSONS_FOR_REVIEW_MERGE
            None,  # LINK_REVIEW_SALES_PURCHASED_ORDER
            None,  # LINK_REVIEW_SALES_BOUGHT_UNIT
            None,  # MARK_REVIEW_SALES_RECORD_LINKED → no match → not applicable
        ]
    )

    result = await _action_tx(
        cast(AsyncManagedTransaction, tx),
        "rc-1",
        ApiReviewActionType.MERGE.value,
        "resolved",
        "merge",
        None,
        None,
        "reviewer@example.com",
        None,
        [],
    )

    assert result == {"merge_not_applicable": True}
    assert tx.calls[3].query == MARK_REVIEW_SALES_RECORD_LINKED


@pytest.mark.asyncio
async def test_reject_marks_sales_record_unresolved() -> None:
    """REJECT action marks any attached sales SourceRecord as unresolved."""
    tx = _Tx(
        [
            {
                "review_case": {
                    "review_case_id": "rc-2",
                    "queue_state": "resolved",
                    "resolution": "reject",
                }
            },
            None,  # MARK_REVIEW_SALES_RECORD_UNRESOLVED (result not used)
        ]
    )

    result = await _action_tx(
        cast(AsyncManagedTransaction, tx),
        "rc-2",
        ApiReviewActionType.REJECT.value,
        "resolved",
        "reject",
        None,
        None,
        "reviewer@example.com",
        None,
        [],
    )

    assert result == {
        "review_case_id": "rc-2",
        "queue_state": "resolved",
        "resolution": "reject",
    }
    assert tx.calls[-1].query == MARK_REVIEW_SALES_RECORD_UNRESOLVED
