"""Tests for the record_type subtype backfill migration."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

from src.graph import queries
from src.graph.client import Neo4jClient
from src.graph.migrations import (
    COMPLETE_BITRIX_CHAT_SOURCE_MIGRATION,
    DEDUPLICATE_LEGACY_BITRIX_PROJECTIONS,
    FINALIZE_BITRIX_SOURCE_MIGRATION,
    LIST_BITRIX_RECORDS_FOR_OWNERSHIP,
    MIGRATE_PROJECTION_RELATIONSHIP_LIFECYCLE,
    MIGRATE_SOURCE_RECORD_LIFECYCLE,
    RECONCILE_PROJECTION_RELATIONSHIP_LIFECYCLE,
    RECONCILE_SOURCE_RECORD_LIFECYCLE,
    REHOME_LEGACY_BITRIX_RECORDS,
    REHOME_LEGACY_BITRIX_RUNS,
    REWRITE_DIRECT_BITRIX_PROJECTION_KEYS,
    REWRITE_LEGACY_BITRIX_PROJECTION_KEYS,
    START_BITRIX_CHAT_SOURCE_MIGRATION,
    apply_data_migrations,
    backfill_record_type_subtypes,
)


def test_backfill_query_is_exported_and_maps_sources_to_subtypes() -> None:
    query = queries.BACKFILL_RECORD_TYPE_SUBTYPES
    # Both legacy 'system' rows and intermediate 'public_record' rows are
    # reclassified (idempotent on re-run).
    assert "sr.record_type IN ['system', 'public_record']" in query
    # Mapping mirrors what the connectors now emit.
    assert "ENDS WITH ':contacts' THEN 'relationship'" in query
    assert "= 'sgbankruptcy'  THEN 'bankruptcy'" in query
    assert "= 'sgrentalflats' THEN 'rental_flat'" in query
    assert "ELSE 'identity'" in query


class _Result:
    def __init__(self, record: dict[str, object] | None) -> None:
        self._record = record

    def single(self) -> dict[str, object] | None:
        return self._record

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(())


class _Tx:
    def __init__(self, updated: int) -> None:
        self.updated = updated
        self.queries: list[str] = []

    def run(self, query: str, **_params: object) -> _Result:
        self.queries.append(query)
        return _Result({"updated": self.updated})


class _Client:
    """Minimal Neo4jClient stand-in that runs the work fn against a fake tx."""

    def __init__(self, updated: int) -> None:
        self.tx = _Tx(updated)

    def execute_write(self, work: object, **_kwargs: object) -> object:
        return cast("object", work)(self.tx)  # type: ignore[operator]


def test_backfill_runner_returns_updated_count_and_runs_query() -> None:
    client = _Client(updated=5)
    result = backfill_record_type_subtypes(cast(Neo4jClient, client))
    assert result == 5
    assert client.tx.queries == [queries.BACKFILL_RECORD_TYPE_SUBTYPES]


def test_backfill_runner_is_safe_when_nothing_to_update() -> None:
    client = _Client(updated=0)
    assert backfill_record_type_subtypes(cast(Neo4jClient, client)) == 0


def test_apply_data_migrations_runs_backfill() -> None:
    client = _Client(updated=3)
    apply_data_migrations(cast(Neo4jClient, client))
    assert client.tx.queries[0] == queries.BACKFILL_RECORD_TYPE_SUBTYPES
    assert client.tx.queries == [
        queries.BACKFILL_RECORD_TYPE_SUBTYPES,
        START_BITRIX_CHAT_SOURCE_MIGRATION,
        REHOME_LEGACY_BITRIX_RECORDS,
        REHOME_LEGACY_BITRIX_RUNS,
        DEDUPLICATE_LEGACY_BITRIX_PROJECTIONS,
        REWRITE_LEGACY_BITRIX_PROJECTION_KEYS,
        REWRITE_DIRECT_BITRIX_PROJECTION_KEYS,
        LIST_BITRIX_RECORDS_FOR_OWNERSHIP,
        FINALIZE_BITRIX_SOURCE_MIGRATION,
        COMPLETE_BITRIX_CHAT_SOURCE_MIGRATION,
        MIGRATE_SOURCE_RECORD_LIFECYCLE,
        MIGRATE_PROJECTION_RELATIONSHIP_LIFECYCLE,
        RECONCILE_SOURCE_RECORD_LIFECYCLE,
        RECONCILE_PROJECTION_RELATIONSHIP_LIFECYCLE,
    ]
