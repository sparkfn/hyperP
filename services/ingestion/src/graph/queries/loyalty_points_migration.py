"""Restart-safe repair queries for PHPPOS Order loyalty-point properties."""

from __future__ import annotations

TARGET_LOYALTY_ORDER_SOURCES: tuple[str, ...] = ("eko_phppos", "speedzone_phppos")

_INVALID_POINTS_USED = """o.points_used IS NOT NULL
  AND NOT (valueType(o.points_used) STARTS WITH 'INTEGER')"""
_INVALID_POINTS_GAINED = """o.points_gained IS NOT NULL
  AND NOT (valueType(o.points_gained) STARTS WITH 'INTEGER')"""

COUNT_INVALID_LOYALTY_POINTS = f"""
MATCH (o:Order)
WHERE o.source_system_key IN $source_system_keys
  AND (({_INVALID_POINTS_USED}) OR ({_INVALID_POINTS_GAINED}))
RETURN count(o) AS invalid_order_count,
       count(CASE WHEN {_INVALID_POINTS_USED} THEN 1 END) AS invalid_points_used_count,
       count(CASE WHEN {_INVALID_POINTS_GAINED} THEN 1 END) AS invalid_points_gained_count
"""

ACQUIRE_LOYALTY_POINTS_MIGRATION = """
MERGE (migration:DataMigration {migration_key: $migration_key})
ON CREATE SET migration.created_at = datetime(), migration.status = 'pending'
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration, datetime() AS now
WITH migration, now,
     (migration.completed_at IS NULL OR $reopen_completed)
       AND (migration.owner_id IS NULL
         OR migration.owner_id = $owner_id
         OR migration.lease_expires_at IS NULL
         OR migration.lease_expires_at < now) AS available
FOREACH (_ IN CASE WHEN available THEN [1] ELSE [] END |
  SET migration.owner_id = $owner_id,
      migration.lease_expires_at = now + duration({seconds: $lease_seconds}),
      migration.status = 'running',
      migration.source_cursor = CASE
        WHEN $reopen_completed THEN ''
        ELSE coalesce(migration.source_cursor, '')
      END,
      migration.order_cursor = CASE
        WHEN $reopen_completed THEN ''
        ELSE coalesce(migration.order_cursor, '')
      END,
      migration.completed_at = CASE
        WHEN $reopen_completed THEN NULL ELSE migration.completed_at
      END,
      migration.updated_at = now
)
RETURN available AND migration.owner_id = $owner_id AS acquired,
       migration.completed_at IS NOT NULL AS completed
"""

FETCH_LOYALTY_POINTS_MIGRATION_BATCH = f"""
MATCH (migration:DataMigration {{migration_key: $migration_key}})
WHERE migration.completed_at IS NULL
  AND migration.owner_id = $owner_id
  AND migration.lease_expires_at >= datetime()
MATCH (o:Order)
WHERE o.source_system_key IN $source_system_keys
  AND (o.source_system_key > coalesce(migration.source_cursor, '')
    OR (o.source_system_key = coalesce(migration.source_cursor, '')
      AND o.source_order_id > coalesce(migration.order_cursor, '')))
  AND (({_INVALID_POINTS_USED}) OR ({_INVALID_POINTS_GAINED}))
RETURN o.source_system_key AS source_system_key,
       o.source_order_id AS source_order_id,
       o.points_used AS points_used,
       o.points_gained AS points_gained
ORDER BY source_system_key, source_order_id
LIMIT $batch_size
"""

APPLY_LOYALTY_POINTS_MIGRATION_BATCH = """
MATCH (migration:DataMigration {migration_key: $migration_key})
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration, datetime() AS now
WHERE migration.completed_at IS NULL
  AND migration.owner_id = $owner_id
  AND migration.lease_expires_at >= now
UNWIND $updates AS row
MATCH (o:Order {
  source_system_key: row.source_system_key,
  source_order_id: row.source_order_id
})
WITH migration, now, row, o,
     o.points_used IS NOT NULL
       AND NOT (valueType(o.points_used) STARTS WITH 'INTEGER')
       AND (o.points_used = row.expected_points_used
         OR (row.expected_points_used_is_nan
           AND valueType(o.points_used) STARTS WITH 'FLOAT'
           AND isNaN(toFloat(o.points_used)))) AS update_used,
     o.points_gained IS NOT NULL
       AND NOT (valueType(o.points_gained) STARTS WITH 'INTEGER')
       AND (o.points_gained = row.expected_points_gained
         OR (row.expected_points_gained_is_nan
           AND valueType(o.points_gained) STARTS WITH 'FLOAT'
           AND isNaN(toFloat(o.points_gained)))) AS update_gained
FOREACH (_ IN CASE WHEN update_used THEN [1] ELSE [] END |
  SET o.points_used = row.points_used)
FOREACH (_ IN CASE WHEN update_gained THEN [1] ELSE [] END |
  SET o.points_gained = row.points_gained)
WITH migration, now, row, update_used, update_gained
ORDER BY row.source_system_key DESC, row.source_order_id DESC
WITH migration, now, head(collect(row)) AS last_row,
     sum(CASE WHEN update_used THEN 1 ELSE 0 END)
       + sum(CASE WHEN update_gained THEN 1 ELSE 0 END) AS updated_field_count
SET migration.source_cursor = last_row.source_system_key,
    migration.order_cursor = last_row.source_order_id,
    migration.updated_field_count = coalesce(migration.updated_field_count, 0)
      + updated_field_count,
    migration.lease_expires_at = now + duration({seconds: $lease_seconds}),
    migration.updated_at = now
RETURN updated_field_count
"""

COMPLETE_LOYALTY_POINTS_MIGRATION = f"""
MATCH (migration:DataMigration {{migration_key: $migration_key}})
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration, datetime() AS now
WHERE migration.completed_at IS NULL
  AND migration.owner_id = $owner_id
  AND migration.lease_expires_at >= now
CALL () {{
  MATCH (o:Order)
  WHERE o.source_system_key IN $source_system_keys
    AND (({_INVALID_POINTS_USED}) OR ({_INVALID_POINTS_GAINED}))
  RETURN count(o) AS invalid_order_count
}}
FOREACH (_ IN CASE WHEN invalid_order_count = 0 THEN [1] ELSE [] END |
  SET migration.status = 'complete',
      migration.completed_at = now,
      migration.owner_id = NULL,
      migration.lease_expires_at = NULL,
      migration.source_cursor = NULL,
      migration.order_cursor = NULL,
      migration.updated_at = now
)
RETURN invalid_order_count, migration.completed_at IS NOT NULL AS completed
"""

RELEASE_LOYALTY_POINTS_MIGRATION = """
MATCH (migration:DataMigration {migration_key: $migration_key})
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration
WHERE migration.completed_at IS NULL AND migration.owner_id = $owner_id
SET migration.owner_id = NULL,
    migration.lease_expires_at = NULL,
    migration.status = 'pending',
    migration.updated_at = datetime()
RETURN true AS released
"""
