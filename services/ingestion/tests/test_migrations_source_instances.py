"""Contracts for the explicit source-instance graph migration."""

from __future__ import annotations

from pathlib import Path

from src.graph import migrations
from src.graph.queries.source_instance_migrations import (
    MIGRATE_SOURCE_RECORD_IDENTITY_LOCKS,
    MIGRATE_SOURCE_RECORD_SOURCE_INSTANCES_BATCH,
)
from src.graph.schema_init import BASE_LIFECYCLE_CONSTRAINTS
from src.source_instances import LEGACY_DEFAULT_SOURCE_INSTANCE_ID


def test_source_instance_migration_assigns_default_provenance_and_sv2_keys() -> None:
    query = MIGRATE_SOURCE_RECORD_SOURCE_INSTANCES_BATCH

    assert "LIMIT $batch_size" in query
    assert "version.source_instance_id IS NULL" in query
    assert "version.source_version_key STARTS WITH 'sv1:'" in query
    assert "coalesce(version.source_instance_id, $legacy_source_instance_id)" in query
    assert "version.source_instance_id = target_source_instance_id" in query
    assert "version.parent_source_instance_id" in query
    assert "'sv2:'" in query
    assert "WHEN version.source_version_key STARTS WITH 'sv1:'" in query
    assert "ELSE version.source_version_key END" in query
    assert "stable_pk" in query
    assert "coalesce(version.legacy_repair_id, randomUUID())" in query
    assert "RETURN count(version) AS updated" in query


def test_source_instance_migration_converts_existing_locks_before_lifecycle_use() -> None:
    query = MIGRATE_SOURCE_RECORD_IDENTITY_LOCKS

    assert "MATCH (lock:SourceRecordIdentityLock)" in query
    assert "WHERE lock.source_instance_id IS NULL" in query
    assert "LIMIT $batch_size" in query
    assert "lock.source_instance_id = $legacy_source_instance_id" in query
    assert LEGACY_DEFAULT_SOURCE_INSTANCE_ID == "legacy-default"


def test_graph_schema_uses_the_triple_lock_constraint() -> None:
    schema = "\n".join(BASE_LIFECYCLE_CONSTRAINTS)

    assert "source_record_identity_lock_triple_unique" in schema
    assert "REQUIRE (lock.source_system, lock.source_instance_id, lock.source_record_id)" in schema
    assert "source_record_identity_lock_unique IF NOT EXISTS" not in schema


def test_data_migrations_apply_lifecycle_before_source_instance_rekey() -> None:
    source = Path(migrations.__file__).read_text(encoding="utf-8")

    assert source.index("migrate_source_record_lifecycle(client)") < source.index(
        "migrate_source_record_source_instances(client)"
    )
    assert "DROP CONSTRAINT source_record_identity_lock_unique IF EXISTS" in source
