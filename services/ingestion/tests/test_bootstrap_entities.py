"""Entity/source-system bootstrap — query + seed data (seed data in Task 2)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import pytest
from src.graph import fundbox_source_migration, migrations, queries
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
    assert by_key["fundbox"]["entity_key"] == "fundbox"
    assert by_key["onediver"]["entity_key"] == "onediver"


def test_fundbox_source_systems_use_canonical_keys_and_names() -> None:
    fundbox_sources = {
        source["source_key"]: source["display_name"]
        for source in _SOURCE_SYSTEMS
        if source["entity_key"] == "fundbox" and source["source_key"].startswith("fundbox")
    }

    assert fundbox_sources == {
        "fundbox": "Fundbox",
        "fundbox:contacts": "Fundbox — contacts",
        "fundbox:legacy": "Fundbox — legacy profiles",
        "fundbox:merged": "Fundbox — merged users",
        "fundbox:sales": "Fundbox — orders / sales",
    }


def test_source_key_to_entity_omits_entity_less_sg_sources() -> None:
    assert "sgbankruptcy" not in SOURCE_KEY_TO_ENTITY
    assert "sgrentalflats" not in SOURCE_KEY_TO_ENTITY
    assert SOURCE_KEY_TO_ENTITY["fundbox"] == "fundbox"


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


def test_fundbox_source_migration_preserves_graph_identity_and_provenance() -> None:
    links = fundbox_source_migration.REHOME_SOURCE_LINKS
    provenance = fundbox_source_migration.REWRITE_SOURCE_PROVENANCE
    references = fundbox_source_migration.REWRITE_SOURCE_RECORD_REFERENCES

    assert "SourceRecord" in links
    assert "IngestRun" in links
    assert "SOLD_THROUGH" in links
    assert "legacy.source_system_id" in links
    assert "legacy.created_at" in links
    assert "MERGE (record)-[:FROM_SOURCE]->(canonical)" in links
    assert "node.source_system_key = $canonical_key" in provenance
    assert "relationship.source_system_key = $canonical_key" in provenance
    assert "run.source_key = $canonical_key" in provenance
    assert "lock.source_system = $canonical_key" in provenance
    assert "version.migration_source_system = $canonical_key" in provenance
    assert "migration.current_source_system = $canonical_key" in provenance
    assert "vehicle.source_systems" in provenance
    assert "node.source_record_id STARTS WITH 'fundbox_consumer_backend-'" in references
    assert "relationship.source_record_id STARTS WITH 'fundbox_consumer_backend-'" in references
    assert "version.migration_source_record_id" in references
    assert "migration.current_source_record_id" in references
    assert "record.source_version_key = NULL" in references
    assert "record.raw_payload = replace" in references


class _FundboxMigrationTx:
    def __init__(self, *, migration_completed: bool = False) -> None:
        self.migration_completed = migration_completed
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **params: object) -> _MigrationResult:
        self.calls.append((query, params))
        if query == fundbox_source_migration.START_MIGRATION:
            completed_at = "2026-07-23T00:00:00Z" if self.migration_completed else None
            return _MigrationResult([{"completed_at": completed_at}])
        if query == fundbox_source_migration.COMPLETE_MIGRATION:
            return _MigrationResult([{"completed_at": "2026-07-23T00:00:00Z"}])
        if query == fundbox_source_migration.CHECK_LEGACY_SOURCE_LINKS:
            return _MigrationResult([{"remaining": 0}])
        return _MigrationResult([{"updated": 1, "removed": 1}])


class _FundboxMigrationClient:
    def __init__(self, tx: _FundboxMigrationTx) -> None:
        self.tx = tx

    def execute_write(self, work: object, **_kwargs: object) -> object:
        return cast("object", work)(self.tx)  # type: ignore[operator]


def test_fundbox_source_migration_runs_each_mapping_once() -> None:
    tx = _FundboxMigrationTx()
    client = _FundboxMigrationClient(tx)

    assert migrations.migrate_fundbox_source_keys(cast(Neo4jClient, client)) == 11

    link_calls = [
        params
        for query, params in tx.calls
        if query == fundbox_source_migration.REHOME_SOURCE_LINKS
    ]
    assert link_calls == [
        {"legacy_key": legacy_key, "canonical_key": canonical_key}
        for legacy_key, canonical_key in fundbox_source_migration.SOURCE_KEY_MAPPINGS
    ]
    assert tx.calls[-5][0] == fundbox_source_migration.REWRITE_SOURCE_RECORD_REFERENCES
    assert tx.calls[-4][0] == fundbox_source_migration.REMOVE_LEGACY_OWNERSHIP
    assert tx.calls[-3][0] == fundbox_source_migration.CHECK_LEGACY_SOURCE_LINKS
    assert tx.calls[-2][0] == fundbox_source_migration.DELETE_LEGACY_SOURCES
    assert tx.calls[-1][0] == fundbox_source_migration.COMPLETE_MIGRATION


def test_fundbox_source_migration_skips_completed_marker() -> None:
    tx = _FundboxMigrationTx(migration_completed=True)
    client = _FundboxMigrationClient(tx)

    assert migrations.migrate_fundbox_source_keys(cast(Neo4jClient, client)) == 0
    assert tx.calls == [(fundbox_source_migration.START_MIGRATION, {})]


def test_fundbox_source_migration_refuses_to_drop_unexpected_links() -> None:
    tx = _FundboxMigrationTx()
    client = _FundboxMigrationClient(tx)
    original_run = tx.run

    def run_with_remaining_link(query: str, **params: object) -> _MigrationResult:
        if query == fundbox_source_migration.CHECK_LEGACY_SOURCE_LINKS:
            tx.calls.append((query, params))
            return _MigrationResult([{"remaining": 1}])
        return original_run(query, **params)

    tx.run = run_with_remaining_link  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="unexpected legacy relationships"):
        migrations.migrate_fundbox_source_keys(cast(Neo4jClient, client))

    queries_run = [query for query, _params in tx.calls]
    assert fundbox_source_migration.DELETE_LEGACY_SOURCES not in queries_run
    assert fundbox_source_migration.COMPLETE_MIGRATION not in queries_run


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
