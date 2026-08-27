"""Additive readiness marker for standalone CRM census control state."""

from __future__ import annotations

from neo4j import ManagedTransaction

from src.graph.client import Neo4jClient
from src.graph.ingestion_control_instance_migration import assert_ingestion_control_ready
from src.graph.standalone_crm_census_schema import assert_standalone_census_schema

STANDALONE_CRM_CENSUS_MIGRATION_KEY = "standalone_crm_census_control_v1"


def migrate_standalone_crm_census_control(client: Neo4jClient) -> None:
    """Mark #273 ready only after #272 and the complete #273 schema inventory pass."""
    assert_ingestion_control_ready(client)
    assert_standalone_census_schema(client)

    def work(tx: ManagedTransaction) -> None:
        record = tx.run(
            "MERGE (migration:DataMigration {migration_key: $migration_key}) "
            "ON CREATE SET migration.created_at = datetime() "
            "SET migration.completed_at = coalesce(migration.completed_at, datetime()) "
            "RETURN migration.completed_at IS NOT NULL AS ready",
            migration_key=STANDALONE_CRM_CENSUS_MIGRATION_KEY,
        ).single()
        if record is None or record["ready"] is not True:
            raise RuntimeError("standalone CRM census readiness marker was rejected")

    client.execute_write(work)


def assert_standalone_crm_census_ready(client: Neo4jClient) -> None:
    """Reject execution if #272, #273 schema, or #273 completion marker is stale."""
    assert_ingestion_control_ready(client)
    assert_standalone_census_schema(client)

    def read(tx: ManagedTransaction) -> bool:
        record = tx.run(
            "MATCH (migration:DataMigration {migration_key: $migration_key}) "
            "RETURN migration.completed_at IS NOT NULL AS ready",
            migration_key=STANDALONE_CRM_CENSUS_MIGRATION_KEY,
        ).single()
        return record is not None and record["ready"] is True

    if not client.execute_read(read):
        raise RuntimeError("standalone CRM census control migration is not complete")
