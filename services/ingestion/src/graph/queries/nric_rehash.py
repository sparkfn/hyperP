"""Cypher queries for NRIC rehash census, verification, and apply."""

from __future__ import annotations

NRIC_IDENTIFIER_CENSUS = """
MATCH (i:Identifier {identifier_type: 'nric'})
RETURN count(i) AS total,
       coalesce(sum(CASE
         WHEN i.normalized_value =~ '^[0-9a-f]{64}$' THEN 1 ELSE 0
       END), 0) AS digest_shaped,
       coalesce(sum(CASE
         WHEN i.normalized_value =~ '^[0-9a-f]{64}$' THEN 0 ELSE 1
       END), 0) AS non_digest,
       coalesce(sum(CASE WHEN i.identifier_id IS NULL THEN 1 ELSE 0 END), 0)
         AS missing_ids,
       coalesce(sum(CASE WHEN i.identifier_scope IS NULL THEN 1 ELSE 0 END), 0)
         AS missing_scopes,
       coalesce(sum(CASE
         WHEN i.identifier_scope IS NOT NULL AND i.identifier_scope <> 'global'
           THEN 1 ELSE 0
       END), 0) AS unexpected_scopes
"""

NRIC_IDENTIFIER_PAGE = """
MATCH (i:Identifier {identifier_type: 'nric'})
WHERE $after_id IS NULL OR i.identifier_id > $after_id
RETURN i.identifier_id AS identifier_id,
       i.identifier_scope AS identifier_scope,
       i.normalized_value AS normalized_value,
       i.hashed_value AS hashed_value,
       i.nric_masked AS nric_masked,
       i.checksum_valid AS checksum_valid,
       i.nric_key_version AS nric_key_version,
       i.nric_encrypted AS nric_encrypted
ORDER BY identifier_id
LIMIT $batch_size
"""

NRIC_VERIFY_IDENTIFIERS = """
MATCH (i:Identifier {identifier_type: 'nric'})
RETURN count(i) AS identifier_count,
       coalesce(sum(CASE
         WHEN i.normalized_value =~ '^[0-9a-f]{64}$' THEN 0 ELSE 1
       END), 0) AS non_digest_count,
       coalesce(sum(CASE
         WHEN i.nric_masked IS NULL OR i.checksum_valid IS NULL
           OR i.nric_key_version IS NULL OR i.nric_encrypted IS NULL
           OR i.nric_key_version <> $key_version
           OR NOT coalesce(i.hashed_value = i.normalized_value, false)
         THEN 1 ELSE 0
       END), 0) AS missing_metadata_count,
       coalesce(sum(CASE WHEN i.identifier_id IS NULL THEN 1 ELSE 0 END), 0)
         AS missing_id_count,
       // Counts NULL scope and any scope other than 'global'.
       coalesce(sum(CASE
         WHEN coalesce(i.identifier_scope = 'global', false) THEN 0 ELSE 1
       END), 0) AS unexpected_scope_count
"""

NRIC_VERIFY_DUPLICATE_PERSON_KEYS = """
MATCH (p:Person)
WHERE p.nric_key IS NOT NULL
WITH p.nric_key AS key, count(*) AS owners
WHERE owners > 1
RETURN count(*) AS duplicate_key_groups,
       coalesce(sum(owners - 1), 0) AS duplicate_extra_owners
"""

NRIC_VERIFY_DUPLICATE_SCOPED_TARGETS = """
MATCH (i:Identifier {identifier_type: 'nric'})
WHERE i.identifier_scope = 'global' OR i.identifier_scope IS NULL
WITH i.normalized_value AS normalized_value, count(i) AS identifiers
WHERE identifiers > 1
RETURN count(*) AS duplicate_scoped_targets
"""

# Read the active database name without creating or modifying graph state.
NRIC_REHASH_DATABASE_NAME = """
CALL db.info() YIELD name
RETURN name
"""

# Read the durable migration state without taking a write lock.
NRIC_REHASH_READ_STATE = """
OPTIONAL MATCH (migration:DataMigration {migration_key: $migration_key})
RETURN migration.migration_key IS NOT NULL AS exists,
       migration.phase AS phase,
       migration.cursor AS cursor,
       migration.key_version AS key_version,
       migration.key_check AS key_check,
       migration.baseline_total AS baseline_total,
       migration.baseline_non_digest AS baseline_non_digest,
       migration.baseline_digest_shaped AS baseline_digest_shaped,
       migration.baseline_revision AS baseline_revision,
       coalesce(migration.batches, 0) AS batches,
       coalesce(migration.converted_in_place, 0) AS converted_in_place,
       coalesce(migration.converted_consolidated, 0) AS converted_consolidated,
       coalesce(migration.removed_nodes, 0) AS removed_nodes
"""

# Read the revision counter without the existing write-producing counter query.
NRIC_REHASH_READ_REVISION = """
OPTIONAL MATCH (counter:IdentityLinkRevisionCounter {stream_key: $stream_key})
RETURN counter.current_revision AS current_revision
"""

# Create the one-time baseline and acquire the lease after first locking the state node.
NRIC_REHASH_ACQUIRE_LEASE = """
MERGE (migration:DataMigration {migration_key: $migration_key})
ON CREATE SET migration.phase = 'convert',
              migration.key_version = $key_version,
              migration.key_check = $key_check,
              migration.baseline_total = $baseline_total,
              migration.baseline_non_digest = $baseline_non_digest,
              migration.baseline_digest_shaped = $baseline_digest_shaped,
              migration.baseline_revision = $baseline_revision,
              migration.batches = 0,
              migration.converted_in_place = 0,
              migration.converted_consolidated = 0,
              migration.removed_nodes = 0,
              migration.created_at = datetime(),
              migration.updated_at = datetime(),
              migration.lock_version = 0
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration, datetime() AS now
WHERE migration.lease_owner IS NULL OR migration.lease_owner = $owner
   OR migration.lease_until IS NULL OR migration.lease_until < now
WITH migration, now,
     $restart_completed AND migration.phase = 'convert_complete' AS restart_completed
SET migration.lease_owner = $owner,
    migration.lease_until = now + duration({seconds: $lease_seconds}),
    migration.offline_acknowledged_at = coalesce(migration.offline_acknowledged_at, now),
    migration.phase = CASE WHEN restart_completed THEN 'convert' ELSE migration.phase END,
    migration.cursor = CASE WHEN restart_completed THEN null ELSE migration.cursor END,
    migration.updated_at = now
RETURN true AS acquired,
       migration.key_version = $key_version AND migration.key_check = $key_check
         AS key_matches,
       migration.phase AS phase,
       migration.cursor AS cursor,
       migration.key_version AS key_version,
       migration.key_check AS key_check,
       migration.baseline_total AS baseline_total,
       migration.baseline_non_digest AS baseline_non_digest,
       migration.baseline_digest_shaped AS baseline_digest_shaped,
       migration.baseline_revision AS baseline_revision,
       coalesce(migration.batches, 0) AS batches,
       coalesce(migration.converted_in_place, 0) AS converted_in_place,
       coalesce(migration.converted_consolidated, 0) AS converted_consolidated,
       coalesce(migration.removed_nodes, 0) AS removed_nodes
"""

# Re-fence a batch by locking the state node before checking its owner and expiry.
NRIC_REHASH_FENCE_BATCH = """
MATCH (migration:DataMigration {migration_key: $migration_key})
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration, datetime() AS now
WHERE migration.lease_owner = $owner AND migration.lease_until >= now
SET migration.lease_until = now + duration({seconds: $lease_seconds}),
    migration.updated_at = now
RETURN true AS fenced
"""

# Read and value-preserving-lock the next ordered Identifier page in one statement.
NRIC_REHASH_PAGE_AND_LOCK_IDENTIFIERS = """
MATCH (i:Identifier {identifier_type: 'nric'})
WHERE $cursor IS NULL OR i.identifier_id > $cursor
WITH i
ORDER BY i.identifier_id
LIMIT $batch_size
SET i.identifier_id = i.identifier_id
RETURN i.identifier_id AS identifier_id,
       i.identifier_scope AS identifier_scope,
       i.normalized_value AS normalized_value,
       i.hashed_value AS hashed_value,
       i.nric_masked AS nric_masked,
       i.checksum_valid AS checksum_valid,
       i.nric_key_version AS nric_key_version,
       i.nric_encrypted AS nric_encrypted,
       elementId(i) AS element_id
ORDER BY identifier_id
"""

# Find only an exact-global digest collision candidate using the scoped identity index.
NRIC_REHASH_FIND_DIGEST_TARGET = """
MATCH (target:Identifier {
  identifier_type: 'nric',
  identifier_scope: 'global',
  normalized_value: $digest
})
RETURN target.identifier_id AS identifier_id
LIMIT 1
"""

# Guard and update one exact-global Identifier in place without sending plaintext to Cypher.
NRIC_REHASH_UPDATE_IDENTIFIER = """
MATCH (i:Identifier)
WHERE elementId(i) = $element_id
  AND i.identifier_type = 'nric'
  AND i.identifier_scope = 'global'
  AND NOT coalesce(i.normalized_value =~ '^[0-9a-f]{64}$', false)
  AND NOT EXISTS {
    MATCH (target:Identifier {
      identifier_type: 'nric',
      identifier_scope: 'global',
      normalized_value: $digest
    })
    WHERE target <> i
  }
SET i.normalized_value = $digest,
    i.hashed_value = $hashed_value,
    i.nric_key_version = $key_version,
    i.nric_masked = $masked,
    i.checksum_valid = $checksum_valid,
    i.nric_encrypted = $ciphertext
RETURN count(i) AS updated
"""

# Persist the page cursor and aggregate progress under the still-valid lease.
NRIC_REHASH_ADVANCE_BATCH = """
MATCH (migration:DataMigration {migration_key: $migration_key})
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration, datetime() AS now
WHERE migration.lease_owner = $owner AND migration.lease_until >= now
SET migration.cursor = $cursor,
    migration.batches = coalesce(migration.batches, 0) + 1,
    migration.converted_in_place = coalesce(migration.converted_in_place, 0)
      + $converted_in_place,
    migration.updated_at = now,
    migration.lease_until = now + duration({seconds: $lease_seconds})
RETURN migration.batches AS batches,
       migration.converted_in_place AS converted_in_place
"""

# Mark conversion complete and release the owned lease in one fenced write.
NRIC_REHASH_COMPLETE_AND_RELEASE = """
MATCH (migration:DataMigration {migration_key: $migration_key})
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration, datetime() AS now
WHERE migration.lease_owner = $owner AND migration.lease_until >= now
SET migration.phase = 'convert_complete',
    migration.cursor = null,
    migration.updated_at = now
REMOVE migration.lease_owner, migration.lease_until
RETURN true AS completed
"""

# Release only the same owner after locking the state node; a lost lease is untouched.
NRIC_REHASH_RELEASE_LEASE = """
MATCH (migration:DataMigration {migration_key: $migration_key})
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration, datetime() AS now
WHERE migration.lease_owner = $owner
REMOVE migration.lease_owner, migration.lease_until
SET migration.updated_at = now
RETURN true AS released
"""
