"""Restart-safe, fail-closed schema/data migration queries for #272."""

from __future__ import annotations

from src.graph.queries.ingestion_control_instance_validation import (
    COUNT_CONTROL_RELATIONSHIP_MISMATCHES,
    COUNT_INVALID_CONTROL_ROWS,
    COUNT_SOURCE_AMBIGUITIES,
    PROSPECTIVE_COLLISIONS,
)

__all__ = (
    "COUNT_CONTROL_RELATIONSHIP_MISMATCHES",
    "COUNT_INVALID_CONTROL_ROWS",
    "COUNT_SOURCE_AMBIGUITIES",
    "PROSPECTIVE_COLLISIONS",
)

MIGRATION_KEY = "bitrix_control_instance_v1"
LEGACY_CONTROL_INSTANCE_ID = "legacy-default"

PHASES: tuple[str, ...] = (
    "block_dispatch",
    "inventory",
    "backfill_ingest_runs",
    "backfill_logical_runs",
    "backfill_checkpoints",
    "backfill_bitrix_streams_and_fences",
    "backfill_dispatch_generations_publications",
    "validate_rows_and_future_identities",
    "drop_verified_legacy_constraints",
    "create_instance_constraints",
    "postvalidate",
    "complete",
)

# name, label, ordered properties. These are compared to SHOW CONSTRAINTS exactly.
LEGACY_CONSTRAINT_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ingest_run_worker_task_id_unique", "IngestRun", ("worker_task_id",)),
    ("ingest_run_source_idempotency_unique", "IngestRun", ("source_key", "idempotency_key")),
    ("ingestion_checkpoint_key_unique", "IngestionCheckpoint", ("checkpoint_key",)),
    ("ingestion_checkpoint_identity_unique", "IngestionCheckpoint", ("logical_run_id", "phase")),
    (
        "ingestion_logical_run_source_idempotency_unique",
        "IngestionLogicalRun",
        ("source_key", "idempotency_key"),
    ),
    (
        "bitrix_ingestion_stream_identity_unique",
        "BitrixIngestionStream",
        ("source_key", "stream_key"),
    ),
    ("bitrix_backfill_generation_id_unique", "BitrixBackfillGeneration", ("generation_id",)),
    (
        "bitrix_known_owner_set_id_unique",
        "BitrixKnownOwnerRefreshSet",
        ("generation_id", "membership_set_id"),
    ),
    (
        "bitrix_known_owner_member_unique",
        "BitrixKnownOwnerRefreshMember",
        ("generation_id", "membership_set_id", "deal_id"),
    ),
    (
        "bitrix_backfill_coverage_identity_unique",
        "BitrixBackfillCoverage",
        ("generation_id", "stream_key", "source_identity", "source_boundary"),
    ),
    ("bitrix_dispatch_control_source_unique", "BitrixDispatchControl", ("source_key",)),
    ("stage_history_review_command_id_unique", "StageHistoryReviewCommand", ("command_id",)),
    (
        "bitrix_dispatch_outbox_successor_unique",
        "BitrixBackfillDispatchOutbox",
        ("successor_generation_id",),
    ),
)

NEW_CONSTRAINT_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "ingest_run_worker_task_control_unique",
        "IngestRun",
        ("control_instance_id", "worker_task_id"),
    ),
    (
        "ingest_run_source_control_idempotency_unique",
        "IngestRun",
        ("source_key", "control_instance_id", "idempotency_key"),
    ),
    (
        "ingestion_logical_run_source_control_idempotency_unique",
        "IngestionLogicalRun",
        ("source_key", "control_instance_id", "idempotency_key"),
    ),
    (
        "ingestion_checkpoint_control_logical_phase_unique",
        "IngestionCheckpoint",
        ("control_instance_id", "logical_run_id", "phase"),
    ),
    (
        "incremental_checkpoint_control_identity_unique",
        "IngestionCheckpoint",
        ("control_instance_id", "checkpoint_key"),
    ),
    (
        "bitrix_ingestion_stream_control_identity_unique",
        "BitrixIngestionStream",
        ("source_key", "control_instance_id", "stream_key"),
    ),
    (
        "bitrix_known_owner_set_control_unique",
        "BitrixKnownOwnerRefreshSet",
        ("control_instance_id", "generation_id", "membership_set_id"),
    ),
    (
        "bitrix_known_owner_member_control_unique",
        "BitrixKnownOwnerRefreshMember",
        ("control_instance_id", "generation_id", "membership_set_id", "deal_id"),
    ),
    (
        "bitrix_backfill_coverage_control_identity_unique",
        "BitrixBackfillCoverage",
        (
            "control_instance_id",
            "generation_id",
            "stream_key",
            "source_identity",
            "source_boundary",
        ),
    ),
    (
        "bitrix_dispatch_control_control_unique",
        "BitrixDispatchControl",
        ("source_key", "control_instance_id"),
    ),
    (
        "bitrix_backfill_generation_control_unique",
        "BitrixBackfillGeneration",
        ("control_instance_id", "generation_id"),
    ),
    (
        "bitrix_dispatch_outbox_control_successor_unique",
        "BitrixBackfillDispatchOutbox",
        ("control_instance_id", "successor_generation_id"),
    ),
    (
        "stage_history_review_command_control_id_unique",
        "StageHistoryReviewCommand",
        ("control_instance_id", "command_id"),
    ),
    (
        "bitrix_execution_source_binding_control_unique",
        "BitrixExecutionSourceBinding",
        ("source_key", "control_instance_id"),
    ),
)

AFFECTED_LABELS: tuple[str, ...] = (
    "IngestRun",
    "IngestionLogicalRun",
    "IngestionCheckpoint",
    "BitrixIngestionStream",
    "BitrixDispatchControl",
    "BitrixBackfillGeneration",
    "BitrixKnownOwnerRefreshSet",
    "BitrixKnownOwnerRefreshMember",
    "BitrixBackfillCoverage",
    "BitrixActivityOwnerRetry",
    "BitrixBackfillDispatchOutbox",
    "StageHistoryUnit",
    "StageHistoryOccurrence",
    "StageHistoryRetry",
    "StageHistoryReviewCommand",
    "StageHistoryUnitAccounting",
)

ACQUIRE_MIGRATION_LEASE = """
MERGE (migration:DataMigration {migration_key: $migration_key})
ON CREATE SET migration.phase = 'block_dispatch', migration.cursor = '', migration.progress_count = 0,
              migration.created_at = datetime(), migration.updated_at = datetime()
WITH migration, datetime() AS now
WITH migration, now,
     migration.completed_at IS NULL
       AND (migration.lease_owner IS NULL OR migration.lease_until IS NULL
            OR migration.lease_until < now OR migration.lease_owner = $owner_id) AS acquired
FOREACH (_ IN CASE WHEN acquired THEN [1] ELSE [] END |
  SET migration.lease_owner = $owner_id,
      migration.lease_until = now + duration({seconds: $lease_seconds}),
      migration.updated_at = now
)
RETURN migration.phase AS phase, coalesce(migration.cursor, '') AS cursor,
       coalesce(migration.active_label, '') AS active_label,
       coalesce(migration.progress_count, 0) AS progress_count, acquired AS acquired
"""

RENEW_MIGRATION_LEASE = """
MATCH (migration:DataMigration {migration_key: $migration_key, lease_owner: $owner_id})
WHERE migration.completed_at IS NULL AND migration.lease_until >= datetime()
SET migration.lease_until = datetime() + duration({seconds: $lease_seconds}), migration.updated_at = datetime()
RETURN true AS renewed
"""

RELEASE_MIGRATION_LEASE = """
MATCH (migration:DataMigration {migration_key: $migration_key, lease_owner: $owner_id})
REMOVE migration.lease_owner, migration.lease_until
SET migration.updated_at = datetime()
RETURN true AS released
"""

BLOCK_LEGACY_DISPATCH = """
MATCH (migration:DataMigration {migration_key: $migration_key, lease_owner: $owner_id})
WHERE migration.lease_until >= datetime()
OPTIONAL MATCH (existing:BitrixDispatchControl {source_key: 'bitrix_chat'})
WITH migration, collect(existing) AS existing_controls
WHERE size(existing_controls) <= 1
FOREACH (_ IN CASE WHEN size(existing_controls) = 0 THEN [1] ELSE [] END |
  CREATE (:BitrixDispatchControl {
    source_key: 'bitrix_chat',
    control_instance_id: 'legacy-default',
    created_at: datetime(),
    migration_owned_block: true
  })
)
WITH migration
MATCH (control:BitrixDispatchControl {source_key: 'bitrix_chat'})
WITH migration, collect(control) AS controls
WHERE size(controls) = 1
WITH migration, controls[0] AS control, coalesce(controls[0].blocked, false) AS was_blocked
WHERE control.control_instance_id IS NULL
   OR control.control_instance_id = 'legacy-default'
SET control.control_instance_id = 'legacy-default',
    control.blocked = true,
    control.block_reason = CASE
      WHEN was_blocked THEN control.block_reason
      ELSE 'control_instance_migration'
    END,
    control.migration_owned_block = coalesce(control.migration_owned_block, false) OR NOT was_blocked,
    control.updated_at = datetime(), migration.phase = 'inventory', migration.cursor = '',
    migration.progress_count = 0, migration.updated_at = datetime()
RETURN true AS advanced
"""


PREPARE_BACKFILL_LABEL = """
MATCH (migration:DataMigration {migration_key: $migration_key, lease_owner: $owner_id})
WHERE migration.lease_until >= datetime() AND migration.phase = $phase
  AND (migration.active_label IS NULL OR migration.active_label = ''
       OR migration.active_label = $label)
SET migration.active_label = $label, migration.cursor = coalesce(migration.cursor, ''),
    migration.updated_at = datetime()
RETURN migration.cursor AS cursor
"""

FINISH_BACKFILL_LABEL = """
MATCH (migration:DataMigration {migration_key: $migration_key, lease_owner: $owner_id})
WHERE migration.lease_until >= datetime() AND migration.phase = $phase
  AND migration.active_label = $label
SET migration.active_label = NULL, migration.cursor = '', migration.updated_at = datetime()
RETURN true AS finished
"""


def backfill_label_query(label: str) -> str:
    """Return a static-label keyset backfill query; labels cannot be parameters."""
    if label not in AFFECTED_LABELS:
        raise ValueError("unsupported control-instance migration label")
    return f"""
MATCH (migration:DataMigration {{migration_key: $migration_key, lease_owner: $owner_id}})
WHERE migration.lease_until >= datetime() AND migration.phase = $phase
CALL {{
  WITH migration
  OPTIONAL MATCH (node:{label})
  WHERE elementId(node) > $cursor AND node.control_instance_id IS NULL
  WITH node ORDER BY elementId(node) LIMIT $batch_size
  WITH [candidate IN collect(node) WHERE candidate IS NOT NULL] AS nodes
  FOREACH (node IN nodes |
    SET node.control_instance_id = 'legacy-default', node.updated_at = datetime()
  )
  RETURN nodes
}}
WITH migration, [node IN nodes | elementId(node)] AS ids
WITH migration, ids, CASE WHEN size(ids) = 0 THEN '' ELSE ids[-1] END AS next_cursor
SET migration.cursor = next_cursor, migration.progress_count = coalesce(migration.progress_count, 0) + size(ids),
    migration.updated_at = datetime()
RETURN size(ids) AS updated, next_cursor AS next_cursor
"""


ADVANCE_PHASE = """
MATCH (migration:DataMigration {migration_key: $migration_key, lease_owner: $owner_id})
WHERE migration.lease_until >= datetime() AND migration.phase = $expected_phase
SET migration.phase = $next_phase, migration.cursor = '', migration.active_label = NULL, migration.progress_count = 0,
    migration.updated_at = datetime()
RETURN true AS advanced
"""

MARK_COMPLETE = """
MATCH (migration:DataMigration {migration_key: $migration_key, lease_owner: $owner_id})
WHERE migration.lease_until >= datetime() AND migration.phase = 'complete'
SET migration.completed_at = coalesce(migration.completed_at, datetime()), migration.updated_at = datetime()
WITH migration
OPTIONAL MATCH (control:BitrixDispatchControl {
  source_key: 'bitrix_chat', control_instance_id: 'legacy-default',
  migration_owned_block: true, blocked: true, block_reason: 'control_instance_migration'
})
FOREACH (_ IN CASE WHEN control IS NULL THEN [] ELSE [1] END |
  SET control.blocked = false,
      control.block_reason = NULL,
      control.migration_owned_block = false,
      control.updated_at = datetime()
)
RETURN migration.completed_at AS completed_at
"""
