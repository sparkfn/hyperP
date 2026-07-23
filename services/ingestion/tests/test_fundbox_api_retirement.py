from __future__ import annotations

from collections.abc import Callable
from typing import cast

from neo4j import ManagedTransaction
from src.graph.client import Neo4jClient
from src.retirement import retire_source_evidence


class _Result:
    def single(self) -> dict[str, int]:
        return {"retired_count": 1}


class _Transaction:
    def __init__(self) -> None:
        self.query = ""
        self.params: dict[str, object] = {}

    def run(self, query: str, **params: object) -> _Result:
        self.query = query
        self.params = params
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
    )

    assert retired == 1
    assert "SourceSystem {source_key: $source_system}" in transaction.query
    assert "rel.source_record_pk IN source_record_pks" in transaction.query
    assert "rel.is_active = false" in transaction.query
    assert "DELETE" not in transaction.query
    assert transaction.params == {
        "source_system": "fundbox:sales",
        "source_record_id": "fundbox-order-9",
        "retired_at": "2026-07-17T00:00:00+00:00",
    }
