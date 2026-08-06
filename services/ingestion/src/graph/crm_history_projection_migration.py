"""Opt-in, reversible projection migration for legacy generic CRM activities.

The coordinator is deliberately not imported by ``apply_data_migrations``.
It can only be registered after #145 makes generic activity projection metadata
native at creation time and exposes its stable fenced-run interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from neo4j import ManagedTransaction

from src.graph.client import Neo4jClient

MIGRATION_KEY = "crm_history_activity_projection_v1"
_LEASE_SECONDS = 300


@dataclass(frozen=True)
class ProjectionMigrationPolicy:
    """Fixed guardrails for a future explicit migration invocation."""

    migration_key: str = MIGRATION_KEY
    history_family: str = "activity"
    history_kind: str = "generic_activity"
    projection_version: str = "crm-history-projection-v1"
    projection_source: str = "hyperp"


# Every property is accompanied by a migration marker.  A rollback must match
# both the marker and expected projected value before removing it, which keeps
# a later native producer's metadata intact.  ``activated_at`` is never read
# or synthesized: it has a distinct lifecycle meaning.
PROJECT_LEGACY_ACTIVITY_BATCH = """
MATCH (migration:DataMigration {migration_key: $migration_key})
WHERE migration.lease_owner = $lease_owner
  AND migration.lease_expires_at >= datetime()
SET migration.lease_expires_at = datetime() + duration({seconds: $lease_seconds}),
    migration.updated_at = datetime()
MATCH (record:SourceRecord {record_type: 'crm_history'})
WHERE record.history_family IS NULL
WITH record ORDER BY record.source_record_pk LIMIT $batch_size
SET record.history_family = $history_family,
    record.history_kind = $history_kind,
    record.history_source = $history_source,
    record.event_at = record.observed_at,
    record.history_projection_version = $projection_version,
    record.history_projection_source = $projection_source,
    record.history_projected_at = datetime(),
    record.crm_history_projection_migration = $migration_key
RETURN count(record) AS projected
"""

ROLLBACK_LEGACY_ACTIVITY_BATCH = """
MATCH (migration:DataMigration {migration_key: $migration_key})
WHERE migration.lease_owner = $lease_owner
  AND migration.lease_expires_at >= datetime()
SET migration.lease_expires_at = datetime() + duration({seconds: $lease_seconds}),
    migration.updated_at = datetime()
MATCH (record:SourceRecord {record_type: 'crm_history',
  crm_history_projection_migration: $migration_key})
WHERE record.history_family = $history_family
  AND record.history_kind = $history_kind
  AND record.history_source = $history_source
  AND record.event_at = record.observed_at
  AND record.history_projection_version = $projection_version
  AND record.history_projection_source = $projection_source
WITH record ORDER BY record.source_record_pk LIMIT $batch_size
REMOVE record.history_family,
       record.history_kind,
       record.history_source,
       record.event_at,
       record.history_projection_version,
       record.history_projection_source,
       record.history_projected_at,
       record.crm_history_projection_migration
RETURN count(record) AS rolled_back
"""

ACQUIRE_PROJECTION_MIGRATION = """
MERGE (migration:DataMigration {migration_key: $migration_key})
ON CREATE SET migration.created_at = datetime()
WITH migration, datetime() AS now
WHERE migration.lease_owner IS NULL
   OR migration.lease_expires_at IS NULL
   OR migration.lease_expires_at < now
SET migration.lease_owner = $lease_owner,
    migration.lease_expires_at = now + duration({seconds: $lease_seconds}),
    migration.updated_at = now
RETURN true AS acquired
"""

RELEASE_PROJECTION_MIGRATION = """
MATCH (migration:DataMigration {migration_key: $migration_key})
WHERE migration.lease_owner = $lease_owner
SET migration.lease_owner = null,
    migration.lease_expires_at = null,
    migration.updated_at = datetime()
RETURN true AS released
"""


def project_legacy_generic_activities(
    client: Neo4jClient,
    *,
    history_source: str,
    batch_size: int = 500,
    policy: ProjectionMigrationPolicy | None = None,
) -> int:
    """Explicitly project legacy activity rows; never called from startup.

    The lease check appears in the same transaction as every batch. A process
    that loses the lease can perform no further write, and a later invocation
    resumes from rows that remain untyped.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    effective_policy = policy or ProjectionMigrationPolicy()
    lease_owner = str(uuid4())

    def _acquire(tx: ManagedTransaction) -> bool:
        row = tx.run(
            ACQUIRE_PROJECTION_MIGRATION,
            migration_key=effective_policy.migration_key,
            lease_owner=lease_owner,
            lease_seconds=_LEASE_SECONDS,
        ).single()
        return row is not None and bool(row["acquired"])

    if not client.execute_write(_acquire):
        return 0
    total = 0
    try:
        while True:

            def _batch(tx: ManagedTransaction) -> int:
                row = tx.run(
                    PROJECT_LEGACY_ACTIVITY_BATCH,
                    migration_key=effective_policy.migration_key,
                    lease_owner=lease_owner,
                    lease_seconds=_LEASE_SECONDS,
                    batch_size=batch_size,
                    history_source=history_source,
                    history_family=effective_policy.history_family,
                    history_kind=effective_policy.history_kind,
                    projection_version=effective_policy.projection_version,
                    projection_source=effective_policy.projection_source,
                ).single()
                return 0 if row is None else int(row["projected"])

            updated = client.execute_write(_batch)
            total += updated
            if updated == 0:
                return total
    finally:
        client.execute_write(
            lambda tx: tx.run(
                RELEASE_PROJECTION_MIGRATION,
                migration_key=effective_policy.migration_key,
                lease_owner=lease_owner,
            ).single()
        )


def rollback_legacy_generic_activities(
    client: Neo4jClient,
    *,
    history_source: str,
    batch_size: int = 500,
    policy: ProjectionMigrationPolicy | None = None,
) -> int:
    """Remove only metadata still demonstrably added by this migration."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    effective_policy = policy or ProjectionMigrationPolicy()
    lease_owner = str(uuid4())

    def _acquire(tx: ManagedTransaction) -> bool:
        row = tx.run(
            ACQUIRE_PROJECTION_MIGRATION,
            migration_key=effective_policy.migration_key,
            lease_owner=lease_owner,
            lease_seconds=_LEASE_SECONDS,
        ).single()
        return row is not None and bool(row["acquired"])

    if not client.execute_write(_acquire):
        return 0
    total = 0
    try:
        while True:

            def _batch(tx: ManagedTransaction) -> int:
                row = tx.run(
                    ROLLBACK_LEGACY_ACTIVITY_BATCH,
                    migration_key=effective_policy.migration_key,
                    lease_owner=lease_owner,
                    lease_seconds=_LEASE_SECONDS,
                    batch_size=batch_size,
                    history_source=history_source,
                    history_family=effective_policy.history_family,
                    history_kind=effective_policy.history_kind,
                    projection_version=effective_policy.projection_version,
                    projection_source=effective_policy.projection_source,
                ).single()
                return 0 if row is None else int(row["rolled_back"])

            removed = client.execute_write(_batch)
            total += removed
            if removed == 0:
                return total
    finally:
        client.execute_write(
            lambda tx: tx.run(
                RELEASE_PROJECTION_MIGRATION,
                migration_key=effective_policy.migration_key,
                lease_owner=lease_owner,
            ).single()
        )
