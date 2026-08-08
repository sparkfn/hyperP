"""Same-transaction fence ordering for Bitrix mutation families."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TypeVar, cast

from neo4j import ManagedTransaction, Record
from src.bitrix_ingestion_models import FenceContext
from src.graph.bitrix_deal_scope import BitrixDealScopeRepository, DealScopeObservation
from src.graph.client import Neo4jClient
from src.graph.queries.bitrix_deal_scope import UPSERT_DEAL_SCOPE_MEMBERSHIPS
from src.graph.queries.ingestion_control import LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE

T = TypeVar("T")


class _Result:
    def __init__(self, records: tuple[Record, ...]) -> None:
        self._records = records

    def single(self) -> Record | None:
        return self._records[0] if self._records else None

    def __iter__(self) -> Iterator[Record]:
        return iter(self._records)


class _Transaction:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def run(self, query: str, **_parameters: object) -> _Result:
        self.queries.append(query)
        if query == LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE:
            return _Result((cast(Record, {"fence_lock_version": 2}),))
        assert query == UPSERT_DEAL_SCOPE_MEMBERSHIPS
        return _Result(
            (
                cast(
                    Record,
                    {
                        "deal_id": "7",
                        "scope_sequence": 1,
                        "scope_state": "in_scope",
                        "entity_key": "eko",
                        "category_id": "2",
                        "source_record_pk": "source-7",
                    },
                ),
            )
        )


class _Client:
    def __init__(self) -> None:
        self.transaction = _Transaction()

    def execute_write(self, work: Callable[[ManagedTransaction], T]) -> T:
        return work(cast(ManagedTransaction, self.transaction))


def _fence() -> FenceContext:
    return FenceContext(
        logical_run_id="logical-1",
        ingest_run_id="ingest-1",
        source_key="bitrix_chat",
        stream_key="crm_deals",
        stream_generation=1,
        fencing_token=1,
        attempt_generation=1,
    )


def test_deal_scope_write_acquires_fence_before_domain_mutation() -> None:
    client = _Client()
    repository = BitrixDealScopeRepository(cast(Neo4jClient, client))

    repository.record_batch(
        [
            DealScopeObservation(
                deal_id="7",
                scope_state="in_scope",
                category_id="2",
                entity_key="eko",
                source_record_pk="source-7",
            )
        ],
        fence_context=_fence(),
    )

    assert client.transaction.queries == [
        LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE,
        UPSERT_DEAL_SCOPE_MEMBERSHIPS,
    ]
