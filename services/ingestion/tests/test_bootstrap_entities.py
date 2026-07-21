"""Entity/source-system bootstrap — query + seed data (seed data in Task 2)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import pytest
from src.graph import migrations, queries
from src.graph.bootstrap import _ENTITIES, _SOURCE_SYSTEMS, SOURCE_KEY_TO_ENTITY
from src.graph.client import Neo4jClient


def test_upsert_source_system_query_has_no_entity_match() -> None:
    """The entity-less upsert must not reference Entity nodes or OPERATED_BY."""
    q = queries.UPSERT_SOURCE_SYSTEM
    assert "MATCH (e:Entity" not in q
    assert "OPERATED_BY" not in q
    assert "MERGE (ss:SourceSystem {source_key: $source_key})" in q
    assert "RETURN ss.source_system_id AS source_system_id" in q


def test_sggov_entity_is_not_seeded() -> None:
    entity_keys = {entity["entity_key"] for entity in _ENTITIES}
    assert "sggov" not in entity_keys


def test_sg_source_systems_have_no_entity_key() -> None:
    by_key = {source["source_key"]: source for source in _SOURCE_SYSTEMS}
    assert by_key["sgbankruptcy"]["entity_key"] is None
    assert by_key["sgrentalflats"]["entity_key"] is None


def test_non_sg_source_systems_still_have_an_entity_key() -> None:
    by_key = {source["source_key"]: source for source in _SOURCE_SYSTEMS}
    assert by_key["fundbox_consumer_backend"]["entity_key"] == "fundbox"
    assert by_key["onediver"]["entity_key"] == "onediver"


def test_source_key_to_entity_omits_entity_less_sg_sources() -> None:
    assert "sgbankruptcy" not in SOURCE_KEY_TO_ENTITY
    assert "sgrentalflats" not in SOURCE_KEY_TO_ENTITY
    assert SOURCE_KEY_TO_ENTITY["fundbox_consumer_backend"] == "fundbox"


def test_shared_bitrix_chat_source_uses_record_scoped_entity_ownership() -> None:
    by_key = {source["source_key"]: source for source in _SOURCE_SYSTEMS}

    assert by_key["bitrix_chat"]["entity_key"] is None
    assert "bitrix_openlines" not in by_key
    assert "bitrix_chat" not in SOURCE_KEY_TO_ENTITY


def test_bitrix_source_migration_rehomes_legacy_data_before_retiring_source() -> None:
    rehome_records = getattr(migrations, "REHOME_LEGACY_BITRIX_RECORDS", "")
    rehome_runs = getattr(migrations, "REHOME_LEGACY_BITRIX_RUNS", "")
    finalize = getattr(migrations, "FINALIZE_BITRIX_SOURCE_MIGRATION", "")

    assert "bitrix_openlines" in rehome_records
    assert "bitrix_chat" in rehome_records
    assert "DELETE legacy_link" in rehome_records
    assert "IngestRun" in rehome_runs
    assert "DELETE stale_owner" in migrations.LINK_BITRIX_RECORD_TO_ENTITY
    assert "legacy.is_active = false" in finalize
    assert "canonical)-[ownership:OPERATED_BY]" in finalize
    assert "DELETE ownership" in finalize


def test_bitrix_source_migration_rewrites_denormalized_projection_provenance() -> None:
    deduplicate = getattr(migrations, "DEDUPLICATE_LEGACY_BITRIX_PROJECTIONS", "")
    rewrite = getattr(migrations, "REWRITE_LEGACY_BITRIX_PROJECTION_KEYS", "")
    rewrite_direct = getattr(migrations, "REWRITE_DIRECT_BITRIX_PROJECTION_KEYS", "")

    assert "IDENTIFIED_BY|LIVES_AT|KNOWS" in deduplicate
    assert "canonical_projection.source_system_key = 'bitrix_chat'" in deduplicate
    assert "DELETE duplicate" in deduplicate
    assert "IDENTIFIED_BY|LIVES_AT|KNOWS" in rewrite
    assert "projection.source_system_key = 'bitrix_openlines'" in rewrite
    assert "SET projection.source_system_key = 'bitrix_chat'" in rewrite
    assert "DESCRIBES_ADDRESS|MENTIONS_VEHICLE" in rewrite_direct
    assert "SET projection.source_system_key = 'bitrix_chat'" in rewrite_direct


def test_bitrix_ownership_scan_only_returns_missing_or_inconsistent_candidates() -> None:
    query = migrations.LIST_BITRIX_RECORDS_FOR_OWNERSHIP

    assert "OPTIONAL MATCH (record)-[:OWNED_BY]->(owner:Entity)" in query
    assert "record.entity_key IS NULL" in query
    assert "size(owner_entity_keys) <> 1" in query
    assert "head(owner_entity_keys) <> record.entity_key" in query
    assert "owner_entity_keys" in query
    assert "legacy.is_active <> false" in migrations.FINALIZE_BITRIX_SOURCE_MIGRATION


class _MigrationResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)

    def single(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None


class _BitrixMigrationTx:
    def __init__(
        self,
        records: list[dict[str, object]],
        entities: frozenset[str] = frozenset({"speedzone"}),
        *,
        migration_completed: bool = False,
    ) -> None:
        self.records = records
        self.entities = entities
        self.migration_completed = migration_completed
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **params: object) -> _MigrationResult:
        self.calls.append((query, params))
        start_query = getattr(migrations, "START_BITRIX_CHAT_SOURCE_MIGRATION", None)
        if start_query is not None and query == start_query:
            completed_at = "2026-07-21T00:00:00Z" if self.migration_completed else None
            return _MigrationResult([{"completed_at": completed_at}])
        if query == migrations.LIST_BITRIX_RECORDS_FOR_OWNERSHIP:
            return _MigrationResult(self.records)
        if query == migrations.LINK_BITRIX_RECORD_TO_ENTITY:
            entity_key = params.get("entity_key")
            if entity_key not in self.entities:
                return _MigrationResult([])
            return _MigrationResult([{"entity_key": entity_key}])
        return _MigrationResult([{"updated": 1}])


class _BitrixMigrationClient:
    def __init__(self, tx: _BitrixMigrationTx) -> None:
        self.tx = tx

    def execute_write(self, work: object, **_kwargs: object) -> object:
        return cast("object", work)(self.tx)  # type: ignore[operator]


def test_bitrix_source_migration_links_payload_tenant_before_finalizing() -> None:
    tx = _BitrixMigrationTx(
        [
            {
                "source_record_pk": "record-1",
                "entity_key": None,
                "raw_payload": '{"tenant":"speedzone"}',
            }
        ]
    )
    client = _BitrixMigrationClient(tx)

    assert migrations.migrate_bitrix_chat_source(cast(Neo4jClient, client)) == 1

    queries_run = [query for query, _params in tx.calls]
    assert queries_run == [
        migrations.START_BITRIX_CHAT_SOURCE_MIGRATION,
        migrations.REHOME_LEGACY_BITRIX_RECORDS,
        migrations.REHOME_LEGACY_BITRIX_RUNS,
        migrations.DEDUPLICATE_LEGACY_BITRIX_PROJECTIONS,
        migrations.REWRITE_LEGACY_BITRIX_PROJECTION_KEYS,
        migrations.REWRITE_DIRECT_BITRIX_PROJECTION_KEYS,
        migrations.LIST_BITRIX_RECORDS_FOR_OWNERSHIP,
        migrations.LINK_BITRIX_RECORD_TO_ENTITY,
        migrations.FINALIZE_BITRIX_SOURCE_MIGRATION,
        migrations.COMPLETE_BITRIX_CHAT_SOURCE_MIGRATION,
    ]
    assert tx.calls[7][1] == {
        "source_record_pk": "record-1",
        "entity_key": "speedzone",
    }


def test_bitrix_source_migration_second_correct_startup_does_not_relink() -> None:
    tx = _BitrixMigrationTx([], migration_completed=True)
    client = _BitrixMigrationClient(tx)

    assert migrations.migrate_bitrix_chat_source(cast(Neo4jClient, client)) == 0

    queries_run = [query for query, _params in tx.calls]
    assert migrations.LINK_BITRIX_RECORD_TO_ENTITY not in queries_run
    assert queries_run == [migrations.START_BITRIX_CHAT_SOURCE_MIGRATION]


@pytest.mark.parametrize(
    "record",
    [
        {"source_record_pk": "record-1", "entity_key": None, "raw_payload": "{}"},
        {
            "source_record_pk": "record-1",
            "entity_key": None,
            "raw_payload": '{"tenant":"unknown"}',
        },
    ],
)
def test_bitrix_source_migration_does_not_retire_source_when_ownership_fails(
    record: dict[str, object],
) -> None:
    tx = _BitrixMigrationTx([record])
    client = _BitrixMigrationClient(tx)

    with pytest.raises(RuntimeError, match="record-scoped entity|unknown entity"):
        migrations.migrate_bitrix_chat_source(cast(Neo4jClient, client))

    assert migrations.FINALIZE_BITRIX_SOURCE_MIGRATION not in [query for query, _params in tx.calls]
