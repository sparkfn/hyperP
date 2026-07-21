"""Tests for the record_type subtype backfill migration."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import pytest
from src.graph import migrations, queries
from src.graph.client import Neo4jClient
from src.graph.migrations import backfill_record_type_subtypes


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


def test_apply_data_migrations_runs_in_dependency_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    client = cast(Neo4jClient, object())
    functions = (
        ("backfill", "backfill_record_type_subtypes"),
        ("bitrix_migration", "migrate_bitrix_chat_source"),
        ("source_migration", "migrate_source_record_lifecycle"),
        ("projection_migration", "migrate_projection_relationship_lifecycle"),
        ("source_reconciliation", "reconcile_source_record_lifecycle"),
        ("projection_reconciliation", "reconcile_projection_relationship_lifecycle"),
    )
    for label, name in functions:
        monkeypatch.setattr(
            migrations,
            name,
            lambda _client, label=label: calls.append(label),
        )

    migrations.apply_data_migrations(client)

    assert calls == [
        "backfill",
        "bitrix_migration",
        "source_migration",
        "projection_migration",
        "source_reconciliation",
        "projection_reconciliation",
    ]
