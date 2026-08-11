"""Known-owner snapshots stay resumable and below Neo4j transaction ceilings."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TypeVar, cast

import pytest
from src.bitrix_ingestion_models import FenceContext
from src.graph.bitrix_backfill import BitrixBackfillRepository
from src.graph.client import Neo4jClient
from src.graph.queries.bitrix_backfill import (
    LIST_KNOWN_OWNER_IDS,
    PREPARE_KNOWN_OWNER_SET,
    SEAL_KNOWN_OWNER_SET,
    UPSERT_KNOWN_OWNER_MEMBERS,
)
from src.graph.queries.ingestion_control import LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE

T = TypeVar("T")


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._rows)

    def single(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None


class _Transaction:
    def __init__(self, owner_ids: tuple[str, ...], *, fence_active: bool = True) -> None:
        self.owner_ids = owner_ids
        self.fence_active = fence_active
        self.member_batch_sizes: list[int] = []
        self.queries: list[str] = []

    def run(self, query: str, **parameters: object) -> _Result:
        self.queries.append(query)
        if query == LIST_KNOWN_OWNER_IDS:
            return _Result([{"deal_id": deal_id} for deal_id in self.owner_ids])
        if query == LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE:
            rows = [{"fence_lock_version": 2}] if self.fence_active else []
            return _Result(rows)
        if query == PREPARE_KNOWN_OWNER_SET:
            return _Result([{"status": "building"}])
        if query == UPSERT_KNOWN_OWNER_MEMBERS:
            members = parameters["members"]
            assert isinstance(members, list)
            self.member_batch_sizes.append(len(members))
            return _Result([{"batch_count": len(members)}])
        if query == SEAL_KNOWN_OWNER_SET:
            return _Result(
                [
                    {
                        "member_count": len(self.owner_ids),
                        "digest": parameters["digest"],
                    }
                ]
            )
        raise AssertionError("unexpected query")


class _Client:
    def __init__(self, transaction: _Transaction) -> None:
        self.transaction = transaction

    def execute_read(self, work: Callable[[_Transaction], T]) -> T:
        return work(self.transaction)

    def execute_write(self, work: Callable[[_Transaction], T]) -> T:
        return work(self.transaction)


def _fence() -> FenceContext:
    return FenceContext(
        logical_run_id="logical-1",
        ingest_run_id="ingest-1",
        source_key="bitrix_chat",
        stream_key="crm_deals",
        stream_generation=12,
        fencing_token=12,
        attempt_generation=1,
    )


def test_large_owner_snapshot_is_materialized_in_bounded_transactions() -> None:
    owner_ids = tuple(str(value) for value in range(1, 2502))
    transaction = _Transaction(owner_ids)
    repository = BitrixBackfillRepository(cast(Neo4jClient, _Client(transaction)))

    membership = repository.materialize_known_owner_set(
        generation_id="successor-2",
        membership_set_id="successor-2:known-owners:boundary",
        fence_context=_fence(),
    )

    assert membership.deal_ids == owner_ids
    assert transaction.member_batch_sizes == [1000, 1000, 501]
    assert transaction.queries == [
        LIST_KNOWN_OWNER_IDS,
        LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE,
        PREPARE_KNOWN_OWNER_SET,
        LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE,
        UPSERT_KNOWN_OWNER_MEMBERS,
        LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE,
        UPSERT_KNOWN_OWNER_MEMBERS,
        LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE,
        UPSERT_KNOWN_OWNER_MEMBERS,
        LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE,
        SEAL_KNOWN_OWNER_SET,
    ]


def test_stale_owner_snapshot_fence_rejects_before_preparation_mutates() -> None:
    transaction = _Transaction(("2", "10"), fence_active=False)
    repository = BitrixBackfillRepository(cast(Neo4jClient, _Client(transaction)))

    with pytest.raises(RuntimeError, match="fence is stale"):
        repository.materialize_known_owner_set(
            generation_id="successor-2",
            membership_set_id="successor-2:known-owners:boundary",
            fence_context=_fence(),
        )

    assert transaction.queries == [
        LIST_KNOWN_OWNER_IDS,
        LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE,
    ]


def test_existing_owner_members_must_keep_their_original_ordinal() -> None:
    assert "WITH owner_set, item, member" in UPSERT_KNOWN_OWNER_MEMBERS
    assert "WHERE member.ordinal = item.ordinal" in UPSERT_KNOWN_OWNER_MEMBERS
