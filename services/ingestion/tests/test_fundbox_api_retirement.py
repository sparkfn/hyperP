from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import cast

from _txmock import summary_counter_result
from neo4j import ManagedTransaction
from src.graph.client import Neo4jClient
from src.graph.queries.person_list_summary import (
    APPLY_PERSON_LIST_SUMMARY_DELTAS,
    CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID,
)
from src.graph.queries.profile_analysis_dirty import (
    FIND_SOURCE_IDENTITY_DEAL_ELEMENT_IDS,
    LOCK_SOURCE_RECORD_IDENTITY,
)
from src.retirement import retire_source_evidence


class _Result:
    def __init__(
        self,
        record: dict[str, object] | None = None,
        rows: list[dict[str, object]] | None = None,
    ) -> None:
        self._rows = rows if rows is not None else ([] if record is None else [record])

    def single(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._rows)

    def consume(self) -> None:
        return None


class _Transaction:
    def __init__(self) -> None:
        self.query = ""
        self.params: dict[str, object] = {}
        self.queries: list[str] = []
        self.deal_capture_count = 0

    def run(self, query: str, **params: object) -> _Result:
        self.queries.append(query)
        if query == LOCK_SOURCE_RECORD_IDENTITY:
            return _Result({"locked_at": "2026-07-17T00:00:00+00:00"})
        if query == FIND_SOURCE_IDENTITY_DEAL_ELEMENT_IDS:
            return _Result(rows=[{"deal_element_id": "4:retirement-deal:1"}])
        if query == CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID:
            self.deal_capture_count += 1
            return _Result(
                rows=[
                    {
                        "deal_element_id": "4:retirement-deal:1",
                        "resolved_element_id": "4:retirement-deal:1",
                        "qualifying": self.deal_capture_count == 1,
                        "observed_at": (
                            "2026-07-01T00:00:00+00:00" if self.deal_capture_count == 1 else None
                        ),
                    }
                ]
            )
        counter = summary_counter_result(query)
        if counter is not None:
            return counter
        if "SET rel.is_active = false" in query:
            self.query = query
            self.params = params
            return _Result({"retired_count": 1, "source_record_pks": ["retirement-sr-1"]})
        return _Result()


class _Client:
    def __init__(self, transaction: _Transaction) -> None:
        self.transaction = transaction

    def execute_write(self, work: Callable[[ManagedTransaction], int]) -> int:
        return work(cast(ManagedTransaction, self.transaction))


def test_retirement_deactivates_only_source_scoped_evidence_and_keeps_entities() -> None:
    transaction = _Transaction()
    client = cast(Neo4jClient, _Client(transaction))

    retired = retire_source_evidence(
        client,
        "fundbox:sales",
        "fundbox-order-9",
        "2026-07-17T00:00:00+00:00",
        "2026-07-17T00:00:00+00:00",
    )

    assert retired == 1
    assert "SourceSystem {source_key: $source_system}" in transaction.query
    assert "SourceRecordIdentityLock" in transaction.query
    assert "source_instance_id: $source_instance_id" in transaction.query
    assert "reconciliation_snapshot_at" in transaction.query
    assert "rel.source_record_pk IN source_record_pks" in transaction.query
    assert "rel.is_active = false" in transaction.query
    assert "DELETE" not in transaction.query
    assert transaction.params == {
        "source_system": "fundbox:sales",
        "source_instance_id": "legacy-default",
        "source_record_id": "fundbox-order-9",
        "retired_at": "2026-07-17T00:00:00+00:00",
        "reconciliation_snapshot_at": "2026-07-17T00:00:00+00:00",
    }


def test_retirement_locks_the_source_identity_before_it_captures() -> None:
    transaction = _Transaction()
    client = cast(Neo4jClient, _Client(transaction))

    retire_source_evidence(
        client,
        "fundbox:sales",
        "fundbox-order-9",
        "2026-07-17T00:00:00+00:00",
        "2026-07-17T00:00:00+00:00",
    )

    # The identity lock and bounded deal selector precede both production
    # captures; a qualifying-before/false-after result publishes its delta.
    assert transaction.queries[0] == LOCK_SOURCE_RECORD_IDENTITY
    find_index = transaction.queries.index(FIND_SOURCE_IDENTITY_DEAL_ELEMENT_IDS)
    capture_indices = [
        index
        for index, query in enumerate(transaction.queries)
        if query == CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID
    ]
    apply_index = transaction.queries.index(APPLY_PERSON_LIST_SUMMARY_DELTAS)
    assert find_index < capture_indices[0] < capture_indices[1] < apply_index
    assert transaction.deal_capture_count == 2
    assert transaction.queries.count(CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID) == 2
    assert transaction.queries[-1] == APPLY_PERSON_LIST_SUMMARY_DELTAS
