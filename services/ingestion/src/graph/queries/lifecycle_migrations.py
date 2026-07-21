"""Bounded queries for the legacy SourceRecord lifecycle migration."""

ACQUIRE_SOURCE_RECORD_LIFECYCLE_MIGRATION = """
MERGE (migration:DataMigration {migration_key: $migration_key})
ON CREATE SET migration.created_at = datetime(),
              migration.phase = 'prepare',
              migration.updated_records = 0,
              migration.prepared_records = 0,
              migration.cleaned_records = 0,
              migration.lock_version = 0
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration, datetime() AS now
WITH migration,
     now,
     migration.completed_at IS NOT NULL AS completed,
     migration.owner_id IS NULL
       OR migration.owner_id = $owner_id
       OR migration.lease_expires_at IS NULL
       OR migration.lease_expires_at < now AS available
FOREACH (_ IN CASE WHEN NOT completed AND available THEN [1] ELSE [] END |
  SET migration.owner_id = $owner_id,
      migration.phase = coalesce(migration.phase, 'prepare'),
      migration.lease_expires_at = now + duration({seconds: $lease_seconds}),
      migration.heartbeat_at = now)
RETURN completed,
       migration.owner_id = $owner_id AND NOT completed AS acquired,
       coalesce(migration.phase, 'prepare') AS phase,
       coalesce(migration.total_records, 0) AS total_records
"""


INITIALIZE_SOURCE_RECORD_LIFECYCLE_MIGRATION = """
MATCH (migration:DataMigration {migration_key: $migration_key})
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration, datetime() AS now
WHERE migration.completed_at IS NULL
  AND migration.owner_id = $owner_id
SET migration.lease_expires_at = now + duration({seconds: $lease_seconds}),
    migration.heartbeat_at = now
WITH migration
CALL {
  MATCH (version:SourceRecord)
  RETURN count(version) AS total_records
}
SET migration.total_records = coalesce(migration.total_records, total_records)
RETURN coalesce(migration.phase, 'prepare') AS phase,
       migration.total_records AS total_records
"""


PREPARE_SOURCE_RECORD_LIFECYCLE_BATCH = """
MATCH (migration:DataMigration {migration_key: $migration_key})
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration, datetime() AS now
WHERE migration.completed_at IS NULL
  AND migration.owner_id = $owner_id
  AND migration.phase = 'prepare'
SET migration.lease_expires_at = now + duration({seconds: $lease_seconds}),
    migration.heartbeat_at = now
WITH migration
CALL (migration) {
  MATCH (version:SourceRecord)
  WHERE version.source_record_pk > coalesce(migration.prepare_cursor, '')
  WITH version
  ORDER BY version.source_record_pk
  LIMIT $batch_size
  OPTIONAL MATCH (version)-[:FROM_SOURCE]->(ss:SourceSystem)
  WITH version,
       [key IN collect(DISTINCT ss.source_key) WHERE key IS NOT NULL] AS source_keys
  WITH version,
       version.source_record_pk AS stable_pk,
       source_keys
  WITH version,
       stable_pk,
       CASE WHEN size(source_keys) = 1 THEN head(source_keys)
            ELSE 'legacy-orphan:' + stable_pk END AS source_system,
       coalesce(version.source_record_id, 'legacy-pk:' + stable_pk)
         AS source_record_id,
       coalesce(toString(version.source_record_version), 'legacy-pk:' + stable_pk)
         AS source_record_version
  SET version.migration_prepared_for = $migration_key,
      version.source_version_key = NULL,
      version.migration_stable_pk = stable_pk,
      version.migration_source_system = source_system,
      version.migration_source_record_id = source_record_id,
      version.migration_source_record_version = source_record_version,
      version.migration_identity_key =
        'si1:' +
        toString(size(source_system)) + ':' + source_system +
        toString(size(source_record_id)) + ':' + source_record_id
  RETURN count(version) AS processed,
         max(stable_pk) AS next_cursor
}
FOREACH (_ IN CASE WHEN next_cursor IS NULL THEN [] ELSE [1] END |
  SET migration.prepare_cursor = next_cursor)
SET migration.prepared_records =
      coalesce(migration.prepared_records, 0) + processed
RETURN processed
"""


PREPARE_LEGACY_SOURCE_RECORD_LIFECYCLE_BATCH = """
MATCH (migration:DataMigration {migration_key: $migration_key})
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration, datetime() AS now
WHERE migration.completed_at IS NULL
  AND migration.owner_id = $owner_id
  AND migration.phase = 'prepare'
SET migration.lease_expires_at = now + duration({seconds: $lease_seconds}),
    migration.heartbeat_at = now
WITH migration
CALL (migration) {
  MATCH (version:SourceRecord)
  WHERE (version.source_record_pk IS NULL OR version.source_record_pk = '')
    AND version.migration_prepared_for IS NULL
  WITH version
  LIMIT $batch_size
  SET version.legacy_repair_id = coalesce(version.legacy_repair_id, randomUUID())
  WITH version
  OPTIONAL MATCH (version)-[:FROM_SOURCE]->(ss:SourceSystem)
  WITH version,
       [key IN collect(DISTINCT ss.source_key) WHERE key IS NOT NULL] AS source_keys
  WITH version,
       version.legacy_repair_id AS stable_pk,
       source_keys
  WITH version,
       stable_pk,
       CASE WHEN size(source_keys) = 1 THEN head(source_keys)
            ELSE 'legacy-orphan:' + stable_pk END AS source_system,
       coalesce(version.source_record_id, 'legacy-pk:' + stable_pk)
         AS source_record_id,
       coalesce(toString(version.source_record_version), 'legacy-pk:' + stable_pk)
         AS source_record_version
  SET version.migration_prepared_for = $migration_key,
      version.source_version_key = NULL,
      version.migration_stable_pk = stable_pk,
      version.migration_source_system = source_system,
      version.migration_source_record_id = source_record_id,
      version.migration_source_record_version = source_record_version,
      version.migration_identity_key =
        'si1:' +
        toString(size(source_system)) + ':' + source_system +
        toString(size(source_record_id)) + ':' + source_record_id
  RETURN count(version) AS processed
}
SET migration.prepared_records =
      coalesce(migration.prepared_records, 0) + processed
RETURN processed
"""


CLAIM_SOURCE_RECORD_LIFECYCLE_IDENTITY = """
MATCH (migration:DataMigration {migration_key: $migration_key})
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration, datetime() AS now
WHERE migration.completed_at IS NULL
  AND migration.owner_id = $owner_id
  AND migration.phase = 'migrate'
SET migration.lease_expires_at = now + duration({seconds: $lease_seconds}),
    migration.heartbeat_at = now
WITH migration
CALL (migration) {
  WITH migration
  WHERE migration.current_identity_key IS NULL
  MATCH (candidate:SourceRecord)
  WHERE candidate.migration_prepared_for = $migration_key
    AND candidate.migration_identity_key > coalesce(migration.identity_cursor, '')
  WITH candidate.migration_identity_key AS identity_key,
       candidate.migration_source_system AS source_system,
       candidate.migration_source_record_id AS source_record_id
  ORDER BY identity_key
  LIMIT 1
  RETURN head(collect({
    identity_key: identity_key,
    source_system: source_system,
    source_record_id: source_record_id
  })) AS next_identity
}
FOREACH (_ IN CASE
  WHEN migration.current_identity_key IS NULL AND next_identity IS NOT NULL THEN [1]
  ELSE [] END |
  SET migration.current_identity_key = next_identity.identity_key,
      migration.current_source_system = next_identity.source_system,
      migration.current_source_record_id = next_identity.source_record_id,
      migration.current_version_cursor = '')
RETURN migration.current_identity_key IS NOT NULL AS claimed
"""


MIGRATE_SOURCE_RECORD_LIFECYCLE_BATCH = """
MATCH (migration:DataMigration {migration_key: $migration_key})
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration, datetime() AS now
WHERE migration.completed_at IS NULL
  AND migration.owner_id = $owner_id
  AND migration.phase = 'migrate'
  AND migration.current_identity_key IS NOT NULL
SET migration.lease_expires_at = now + duration({seconds: $lease_seconds}),
    migration.heartbeat_at = now
WITH migration,
     migration.current_identity_key AS identity_key,
     migration.current_source_system AS source_system,
     migration.current_source_record_id AS source_record_id
MERGE (identity_lock:SourceRecordIdentityLock {
  source_system: source_system,
  source_record_id: source_record_id
})
SET identity_lock.locked_at = datetime()
WITH migration, identity_key, source_system, source_record_id
CALL (identity_key, source_system, source_record_id) {
  CALL (identity_key, source_system, source_record_id) {
    MATCH (accepted:SourceRecord {migration_identity_key: identity_key})
    WHERE accepted.migration_prepared_for = $migration_key
      AND NOT EXISTS {
        MATCH (accepted)-[:FROM_SOURCE]->(:SourceSystem {source_key: source_system})
      }
    RETURN accepted
    UNION ALL
    MATCH (accepted:SourceRecord {source_record_id: source_record_id})
    WHERE EXISTS {
      MATCH (accepted)-[:FROM_SOURCE]->(:SourceSystem {source_key: source_system})
    }
    RETURN accepted
  }
  WITH accepted
  WHERE NOT coalesce(
    accepted.lifecycle_status IN ['pending_review', 'rejected', 'link_failed'], false
  )
    AND NOT coalesce(
      accepted.lifecycle_status IS NULL AND accepted.link_status = 'pending_review', false
    )
  WITH accepted
  ORDER BY CASE
             WHEN accepted.is_latest = true OR accepted.lifecycle_status = 'active'
             THEN 1 ELSE 0
           END DESC,
           coalesce(toInteger(accepted.source_record_version), -1) DESC,
           accepted.ingested_at DESC,
           coalesce(accepted.source_record_pk, accepted.legacy_repair_id) DESC
  LIMIT 1
  RETURN head(collect(accepted)) AS active_version
}
CALL (migration, identity_key, source_system, source_record_id, active_version) {
  CALL (identity_key, source_system, source_record_id) {
    MATCH (prepared:SourceRecord {migration_identity_key: identity_key})
    WHERE prepared.migration_prepared_for = $migration_key
      AND NOT EXISTS {
        MATCH (prepared)-[:FROM_SOURCE]->(:SourceSystem {source_key: source_system})
      }
    RETURN prepared AS version
    UNION ALL
    MATCH (live:SourceRecord {source_record_id: source_record_id})
    WHERE EXISTS {
      MATCH (live)-[:FROM_SOURCE]->(:SourceSystem {source_key: source_system})
    }
    RETURN live AS version
  }
  WITH version,
       CASE WHEN version.source_record_pk IS NULL OR version.source_record_pk = ''
            THEN version.legacy_repair_id ELSE version.source_record_pk END AS stable_pk,
       migration,
       identity_key,
       source_system,
       source_record_id,
       active_version
  WHERE stable_pk > coalesce(migration.current_version_cursor, '')
  ORDER BY stable_pk
  LIMIT $batch_size
  SET version.migration_prepared_for = $migration_key,
      version.migration_stable_pk = stable_pk,
      version.migration_source_system = source_system,
      version.migration_source_record_id = source_record_id,
      version.migration_source_record_version = coalesce(
        toString(version.source_record_version), 'legacy-pk:' + stable_pk
      ),
      version.migration_identity_key = identity_key
  WITH version,
       stable_pk,
       identity_key,
       source_system,
       source_record_id,
       active_version,
       'sv1:' +
       toString(size(version.migration_source_system)) + ':' +
         version.migration_source_system +
       toString(size(version.migration_source_record_id)) + ':' +
         version.migration_source_record_id +
       toString(size(version.migration_source_record_version)) + ':' +
         version.migration_source_record_version +
       '0:' AS canonical_key
  WITH version, stable_pk, identity_key, active_version, canonical_key,
       CASE WHEN EXISTS {
         MATCH (duplicate:SourceRecord {
           migration_identity_key: identity_key,
           migration_source_record_version: version.migration_source_record_version
         })
         WHERE duplicate.migration_stable_pk < stable_pk
       } OR EXISTS {
         MATCH (duplicate:SourceRecord {source_record_id: source_record_id})
         WHERE duplicate <> version
           AND coalesce(
             toString(duplicate.source_record_version),
             'legacy-pk:' + coalesce(
               duplicate.source_record_pk, duplicate.legacy_repair_id
             )
           ) = version.migration_source_record_version
           AND EXISTS {
             MATCH (duplicate)-[:FROM_SOURCE]->(
               :SourceSystem {source_key: source_system}
             )
           }
           AND coalesce(duplicate.source_record_pk, duplicate.legacy_repair_id) < stable_pk
       } OR EXISTS {
         MATCH (key_owner:SourceRecord {source_version_key: canonical_key})
         WHERE key_owner <> version
       } THEN stable_pk ELSE '' END AS duplicate_discriminator
  SET version.source_version_key =
        'sv1:' +
        toString(size(version.migration_source_system)) + ':' +
          version.migration_source_system +
        toString(size(version.migration_source_record_id)) + ':' +
          version.migration_source_record_id +
        toString(size(version.migration_source_record_version)) + ':' +
          version.migration_source_record_version +
        toString(size(duplicate_discriminator)) + ':' + duplicate_discriminator,
      version.lifecycle_status = CASE
        WHEN version.lifecycle_status IN ['pending_review', 'rejected', 'link_failed']
          THEN version.lifecycle_status
        WHEN version.lifecycle_status IS NULL AND version.link_status = 'pending_review'
          THEN 'pending_review'
        WHEN version = active_version THEN 'active'
        ELSE 'superseded' END,
      version.is_latest = coalesce(version = active_version, false),
      version.migration_applied_for = $migration_key
  RETURN count(version) AS updated,
         max(stable_pk) AS next_cursor
}
FOREACH (_ IN CASE WHEN next_cursor IS NULL THEN [] ELSE [1] END |
  SET migration.current_version_cursor = next_cursor)
SET migration.updated_records = coalesce(migration.updated_records, 0) + updated
RETURN updated
"""


COMPLETE_SOURCE_RECORD_LIFECYCLE_IDENTITY = """
MATCH (migration:DataMigration {migration_key: $migration_key})
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration, datetime() AS now
WHERE migration.completed_at IS NULL
  AND migration.owner_id = $owner_id
  AND migration.phase = 'migrate'
  AND migration.current_identity_key IS NOT NULL
SET migration.identity_cursor = migration.current_identity_key,
    migration.updated_identities = coalesce(migration.updated_identities, 0) + 1,
    migration.lease_expires_at = now + duration({seconds: $lease_seconds}),
    migration.heartbeat_at = now
REMOVE migration.current_identity_key,
       migration.current_source_system,
       migration.current_source_record_id,
       migration.current_version_cursor
RETURN true AS completed_identity
"""


CLEAN_SOURCE_RECORD_LIFECYCLE_BATCH = """
MATCH (migration:DataMigration {migration_key: $migration_key})
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration, datetime() AS now
WHERE migration.completed_at IS NULL
  AND migration.owner_id = $owner_id
  AND migration.phase = 'cleanup'
SET migration.lease_expires_at = now + duration({seconds: $lease_seconds}),
    migration.heartbeat_at = now
WITH migration
CALL (migration) {
  MATCH (version:SourceRecord)
  WHERE version.migration_prepared_for = $migration_key
    AND version.source_record_pk > coalesce(migration.cleanup_cursor, '')
  WITH version
  ORDER BY version.source_record_pk
  LIMIT $batch_size
  REMOVE version.migration_prepared_for,
         version.migration_applied_for,
         version.migration_stable_pk,
         version.migration_source_system,
         version.migration_source_record_id,
         version.migration_source_record_version,
         version.migration_identity_key
  RETURN count(version) AS processed,
         max(version.source_record_pk) AS next_cursor
}
FOREACH (_ IN CASE WHEN next_cursor IS NULL THEN [] ELSE [1] END |
  SET migration.cleanup_cursor = next_cursor)
SET migration.cleaned_records = coalesce(migration.cleaned_records, 0) + processed
RETURN processed
"""


CLEAN_LEGACY_SOURCE_RECORD_LIFECYCLE_BATCH = """
MATCH (migration:DataMigration {migration_key: $migration_key})
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration, datetime() AS now
WHERE migration.completed_at IS NULL
  AND migration.owner_id = $owner_id
  AND migration.phase = 'cleanup'
SET migration.lease_expires_at = now + duration({seconds: $lease_seconds}),
    migration.heartbeat_at = now
WITH migration
CALL (migration) {
  MATCH (version:SourceRecord)
  WHERE version.migration_prepared_for = $migration_key
    AND (version.source_record_pk IS NULL OR version.source_record_pk = '')
  WITH version
  LIMIT $batch_size
  REMOVE version.migration_prepared_for,
         version.migration_applied_for,
         version.migration_stable_pk,
         version.migration_source_system,
         version.migration_source_record_id,
         version.migration_source_record_version,
         version.migration_identity_key
  RETURN count(version) AS processed
}
SET migration.cleaned_records = coalesce(migration.cleaned_records, 0) + processed
RETURN processed
"""


ADVANCE_SOURCE_RECORD_LIFECYCLE_MIGRATION = """
MATCH (migration:DataMigration {migration_key: $migration_key})
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration, datetime() AS now
WHERE migration.completed_at IS NULL
  AND migration.owner_id = $owner_id
  AND migration.phase = $expected_phase
SET migration.phase = $next_phase,
    migration.lease_expires_at = now + duration({seconds: $lease_seconds}),
    migration.heartbeat_at = now
RETURN migration.phase AS phase
"""


COMPLETE_SOURCE_RECORD_LIFECYCLE_MIGRATION = """
MATCH (migration:DataMigration {migration_key: $migration_key})
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration
WHERE migration.completed_at IS NULL
  AND migration.owner_id = $owner_id
  AND migration.phase = 'cleanup'
  AND NOT EXISTS {
    MATCH (version:SourceRecord)
    WHERE version.migration_prepared_for = $migration_key
  }
SET migration.phase = 'complete',
    migration.completed_at = datetime(),
    migration.owner_id = null,
    migration.lease_expires_at = null,
    migration.heartbeat_at = datetime()
RETURN coalesce(migration.updated_records, 0) AS updated_records
"""


RELEASE_SOURCE_RECORD_LIFECYCLE_MIGRATION = """
MATCH (migration:DataMigration {migration_key: $migration_key})
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration
WHERE migration.completed_at IS NULL
  AND migration.owner_id = $owner_id
SET migration.owner_id = null,
    migration.lease_expires_at = null,
    migration.heartbeat_at = datetime()
RETURN true AS released
"""
