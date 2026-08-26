"""Bounded migration queries for explicit SourceRecord source instances."""

from __future__ import annotations

MIGRATE_SOURCE_RECORD_IDENTITY_LOCKS = """
MATCH (lock:SourceRecordIdentityLock)
WHERE lock.source_instance_id IS NULL
WITH lock
LIMIT $batch_size
SET lock.source_instance_id = $legacy_source_instance_id,
    lock.updated_at = datetime()
RETURN count(lock) AS updated
"""

MIGRATE_SOURCE_RECORD_SOURCE_INSTANCES_BATCH = """
MATCH (version:SourceRecord)
WHERE version.source_instance_id IS NULL
   OR version.source_version_key STARTS WITH 'sv1:'
WITH version
ORDER BY coalesce(version.source_record_pk, version.legacy_repair_id)
LIMIT $batch_size
SET version.legacy_repair_id = CASE
  WHEN version.source_record_pk IS NULL OR version.source_record_pk = ''
  THEN coalesce(version.legacy_repair_id, randomUUID())
  ELSE version.legacy_repair_id END
WITH version
OPTIONAL MATCH (version)-[:FROM_SOURCE]->(source:SourceSystem)
WITH version, [key IN collect(DISTINCT source.source_key) WHERE key IS NOT NULL] AS source_keys
WITH version,
     coalesce(version.source_instance_id, $legacy_source_instance_id)
       AS target_source_instance_id,
     CASE WHEN size(source_keys) = 1 THEN head(source_keys)
          ELSE 'legacy-orphan:' + coalesce(version.source_record_pk, version.legacy_repair_id) END
       AS source_system,
     coalesce(version.source_record_pk, version.legacy_repair_id) AS stable_pk,
     coalesce(version.source_record_id, 'legacy-pk:' +
       coalesce(version.source_record_pk, version.legacy_repair_id)) AS source_record_id,
     coalesce(toString(version.source_record_version), 'legacy-pk:' +
       coalesce(version.source_record_pk, version.legacy_repair_id)) AS source_record_version
SET version.source_instance_id = target_source_instance_id,
    version.parent_source_instance_id = CASE
      WHEN version.parent_source_system IS NULL THEN version.parent_source_instance_id
      ELSE coalesce(version.parent_source_instance_id, $legacy_source_instance_id) END,
    version.source_version_key = CASE
      WHEN version.source_version_key STARTS WITH 'sv1:' THEN
        'sv2:' +
        toString(size(source_system)) + ':' + source_system +
        toString(size(target_source_instance_id)) + ':' + target_source_instance_id +
        toString(size(source_record_id)) + ':' + source_record_id +
        toString(size(source_record_version)) + ':' + source_record_version +
        toString(size(stable_pk)) + ':' + stable_pk
      ELSE version.source_version_key END,
    version.source_instance_migration_key = $migration_key,
    version.updated_at = datetime()
RETURN count(version) AS updated
"""

COMPLETE_SOURCE_RECORD_SOURCE_INSTANCE_MIGRATION = """
MERGE (migration:DataMigration {migration_key: $migration_key})
ON CREATE SET migration.created_at = datetime()
SET migration.completed_at = datetime(),
    migration.updated_at = datetime()
RETURN migration.completed_at AS completed_at
"""
