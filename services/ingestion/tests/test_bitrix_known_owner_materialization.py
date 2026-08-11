"""Known-owner snapshots stay resumable and below Neo4j transaction ceilings."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TypeVar

from src.graph.bitrix_backfill import BitrixBackfillRepository
from src.graph.queries.bitrix_backfill import (
    LIST_KNOWN_OWNER_IDS,
    PREPARE_KNOWN_OWNER_SET,
    SEAL_KNOWN_OWNER_SET,
    UPSERT_KNOWN_OWNER_MEMBERS,
)

T = TypeVar("T")


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._rows)

    def single(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None


class _Transaction:
    def __init__(self, owner_ids: tuple[str, ...]) -> None:
        self.owner_ids = owner_ids
        self.member_batch_sizes: list[int] = []

    def run(self, query: str, **parameters: object) -> _Result:
        if query == LIST_KNOWN_OWNER_IDS:
            return _Result([{"deal_id": deal_id} for deal_id in self.owner_ids])
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


def test_large_owner_snapshot_is_materialized_in_bounded_transactions() -> None:
    owner_ids = tuple(str(value) for value in range(1, 2502))
    transaction = _Transaction(owner_ids)
    repository = BitrixBackfillRepository(_Client(transaction))  # type: ignore[arg-type]

    membership = repository.materialize_known_owner_set(
        generation_id="successor-2",
        membership_set_id="successor-2:known-owners:boundary",
    )

    assert membership.deal_ids == owner_ids
    assert transaction.member_batch_sizes == [1000, 1000, 501]
