"""Idempotent data migrations applied on ingestion startup.

Distinct from :mod:`src.graph.schema_init` (which applies constraints/indexes)
and :mod:`src.graph.bootstrap` (which seeds entity/source-system metadata):
these rewrite existing data so it matches the current domain model. Every
migration here must be safe to run repeatedly.
"""

from __future__ import annotations

import logging

from neo4j import ManagedTransaction

from src.graph import queries
from src.graph.client import Neo4jClient

logger = logging.getLogger(__name__)


MIGRATE_SOURCE_RECORD_LIFECYCLE = """
MERGE (migration:DataMigration {migration_key: 'source_record_lifecycle_v1'})
ON CREATE SET migration.created_at = datetime(), migration.lock_version = 0
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration
WHERE migration.completed_at IS NULL
CALL {
  MATCH (version:SourceRecord)
  SET version.source_version_key = NULL,
      version.legacy_repair_id = CASE
        WHEN version.source_record_pk IS NULL OR version.source_record_pk = ''
        THEN coalesce(version.legacy_repair_id, randomUUID())
        ELSE version.legacy_repair_id END
  RETURN count(version) AS cleared
}
WITH migration, cleared
CALL {
  MATCH (version:SourceRecord)
  OPTIONAL MATCH (version)-[:FROM_SOURCE]->(ss:SourceSystem)
  WITH version,
       [key IN collect(DISTINCT ss.source_key) WHERE key IS NOT NULL] AS source_keys
  WITH version,
       CASE WHEN version.source_record_pk IS NULL OR version.source_record_pk = ''
            THEN version.legacy_repair_id
            ELSE version.source_record_pk END AS stable_pk,
       source_keys
  WITH version,
       stable_pk,
       CASE WHEN size(source_keys) = 1 THEN head(source_keys)
            ELSE 'legacy-orphan:' + stable_pk END AS source_system,
       coalesce(version.source_record_id, 'legacy-pk:' + stable_pk)
         AS source_record_id,
       coalesce(
         toString(version.source_record_version),
         'legacy-pk:' + stable_pk
       ) AS source_record_version
  ORDER BY stable_pk
  WITH source_system,
       source_record_id,
       source_record_version,
       collect(version) AS duplicate_versions
  UNWIND range(0, size(duplicate_versions) - 1) AS duplicate_index
  WITH source_system,
       source_record_id,
       source_record_version,
       duplicate_versions[duplicate_index] AS version,
       duplicate_index,
       CASE
         WHEN duplicate_versions[duplicate_index].source_record_pk IS NULL
           OR duplicate_versions[duplicate_index].source_record_pk = ''
         THEN duplicate_versions[duplicate_index].legacy_repair_id
         ELSE duplicate_versions[duplicate_index].source_record_pk
       END AS stable_pk
  WITH source_system,
       source_record_id,
       source_record_version,
       version,
       CASE WHEN duplicate_index = 0 THEN '' ELSE stable_pk END
         AS duplicate_discriminator
  SET version.source_version_key =
        'sv1:' +
        toString(size(source_system)) + ':' + source_system +
        toString(size(source_record_id)) + ':' + source_record_id +
        toString(size(source_record_version)) + ':' + source_record_version +
        toString(size(duplicate_discriminator)) + ':' + duplicate_discriminator
  RETURN count(version) AS keyed
}
WITH migration, cleared, keyed
CALL {
MATCH (version:SourceRecord)
OPTIONAL MATCH (version)-[:FROM_SOURCE]->(ss:SourceSystem)
WITH version,
     [key IN collect(DISTINCT ss.source_key) WHERE key IS NOT NULL] AS source_keys
WITH version,
     CASE WHEN version.source_record_pk IS NULL OR version.source_record_pk = ''
          THEN version.legacy_repair_id
          ELSE version.source_record_pk END AS stable_pk,
     source_keys
WITH version,
     stable_pk,
     CASE WHEN size(source_keys) = 1 THEN head(source_keys)
          ELSE 'legacy-orphan:' + stable_pk END AS source_system,
     coalesce(version.source_record_id, 'legacy-pk:' + stable_pk)
       AS source_record_id
ORDER BY source_system,
         source_record_id,
         coalesce(toInteger(version.source_record_version), -1),
         version.ingested_at,
         stable_pk
WITH source_system, source_record_id, collect(version) AS versions
WITH source_system,
     source_record_id,
     versions,
     [version IN versions
      WHERE NOT coalesce(
        version.lifecycle_status IN ['pending_review', 'rejected', 'link_failed'], false
      )
      AND NOT coalesce(
        version.lifecycle_status IS NULL AND version.link_status = 'pending_review', false
      )] AS accepted_versions
WITH source_system,
     source_record_id,
     versions,
     [version IN accepted_versions
      WHERE version.is_latest = true OR version.lifecycle_status = 'active']
       AS anchor_versions,
     accepted_versions
WITH source_system,
     source_record_id,
     versions,
     CASE WHEN size(anchor_versions) > 0 THEN last(anchor_versions)
          WHEN size(accepted_versions) > 0 THEN last(accepted_versions)
          ELSE NULL END
       AS active_version
UNWIND versions AS version
WITH source_system,
     source_record_id,
     version,
     active_version
SET version.lifecycle_status = CASE
      WHEN version.lifecycle_status IN ['pending_review', 'rejected', 'link_failed']
        THEN version.lifecycle_status
      WHEN version.lifecycle_status IS NULL AND version.link_status = 'pending_review'
        THEN 'pending_review'
      WHEN version = active_version THEN 'active'
      ELSE 'superseded'
    END,
    version.is_latest = (version = active_version)
RETURN count(version) AS updated
}
SET migration.completed_at = datetime(),
    migration.updated_records = updated
RETURN updated
"""


MIGRATE_PROJECTION_RELATIONSHIP_LIFECYCLE = """
MERGE (migration:DataMigration {migration_key: 'projection_relationship_lifecycle_v1'})
ON CREATE SET migration.created_at = datetime(), migration.lock_version = 0
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration
WHERE migration.completed_at IS NULL
CALL {
  MATCH ()-[relationship:IDENTIFIED_BY|LIVES_AT|KNOWS|HAS_FACT]->()
  WHERE relationship.is_active IS NULL
  OPTIONAL MATCH (source:SourceRecord)
  WHERE source.source_record_pk = relationship.source_record_pk
    AND (source.lifecycle_status = 'active'
      OR (source.lifecycle_status IS NULL AND source.is_latest = true))
  WITH relationship,
       relationship.source_record_pk IS NULL OR count(source) > 0 AS is_active
  SET relationship.is_active = is_active
  FOREACH (_ IN CASE WHEN is_active THEN [1] ELSE [] END |
    SET relationship.activated_at = coalesce(relationship.activated_at, datetime()))
  FOREACH (_ IN CASE WHEN is_active THEN [] ELSE [1] END |
    SET relationship.retired_at = coalesce(relationship.retired_at, datetime()))
  RETURN count(relationship) AS updated
}
SET migration.completed_at = datetime(),
    migration.updated_relationships = updated
RETURN updated
"""


RECONCILE_SOURCE_RECORD_LIFECYCLE = """
MATCH (migration:DataMigration {migration_key: 'source_record_lifecycle_v1'})
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration
CALL {
  MATCH (version:SourceRecord)
  WHERE version.source_version_key IS NULL OR version.lifecycle_status IS NULL
  SET version.legacy_repair_id = CASE
    WHEN version.source_record_pk IS NULL OR version.source_record_pk = ''
    THEN coalesce(version.legacy_repair_id, randomUUID())
    ELSE version.legacy_repair_id END
  RETURN count(version) AS candidates
}
CALL {
  MATCH (version:SourceRecord)
  WHERE version.source_version_key IS NULL
  OPTIONAL MATCH (version)-[:FROM_SOURCE]->(ss:SourceSystem)
  WITH version, [key IN collect(DISTINCT ss.source_key) WHERE key IS NOT NULL] AS source_keys
  WITH version,
       CASE WHEN version.source_record_pk IS NULL OR version.source_record_pk = ''
            THEN version.legacy_repair_id ELSE version.source_record_pk END AS stable_pk,
       source_keys
  WITH version, stable_pk,
       CASE WHEN size(source_keys) = 1 THEN head(source_keys)
            ELSE 'legacy-orphan:' + stable_pk END AS source_system,
       coalesce(version.source_record_id, 'legacy-pk:' + stable_pk) AS source_record_id,
       coalesce(toString(version.source_record_version), 'legacy-pk:' + stable_pk)
         AS source_record_version
  OPTIONAL MATCH (duplicate:SourceRecord)
  WHERE duplicate <> version
    AND coalesce(duplicate.source_record_id, 'legacy-pk:' +
      coalesce(duplicate.source_record_pk, duplicate.legacy_repair_id)) = source_record_id
    AND coalesce(toString(duplicate.source_record_version), 'legacy-pk:' +
      coalesce(duplicate.source_record_pk, duplicate.legacy_repair_id)) = source_record_version
  OPTIONAL MATCH (duplicate)-[:FROM_SOURCE]->(duplicate_source:SourceSystem)
  WITH version, stable_pk, source_system, source_record_id, source_record_version,
       count(CASE WHEN duplicate_source.source_key = source_system THEN 1 END) AS duplicates
  WITH version, source_system, source_record_id, source_record_version,
       CASE WHEN duplicates = 0 THEN '' ELSE stable_pk END AS duplicate_discriminator
  SET version.source_version_key =
        'sv1:' + toString(size(source_system)) + ':' + source_system +
        toString(size(source_record_id)) + ':' + source_record_id +
        toString(size(source_record_version)) + ':' + source_record_version +
        toString(size(duplicate_discriminator)) + ':' + duplicate_discriminator
  RETURN count(version) AS keyed
}
CALL {
  MATCH (version:SourceRecord)
  WHERE version.lifecycle_status IS NULL
  OPTIONAL MATCH (version)-[:FROM_SOURCE]->(ss:SourceSystem)
  WITH version, [key IN collect(DISTINCT ss.source_key) WHERE key IS NOT NULL] AS source_keys
  WITH version,
       CASE WHEN size(source_keys) = 1 THEN head(source_keys)
            ELSE 'legacy-orphan:' + coalesce(version.source_record_pk, version.legacy_repair_id)
       END AS source_system,
       coalesce(version.source_record_id, 'legacy-pk:' +
         coalesce(version.source_record_pk, version.legacy_repair_id)) AS source_record_id
  ORDER BY coalesce(toInteger(version.source_record_version), -1),
           version.ingested_at,
           coalesce(version.source_record_pk, version.legacy_repair_id)
  WITH source_system, source_record_id, collect(version) AS versions
  CALL (source_system, source_record_id) {
    MATCH (complete:SourceRecord)
    WHERE complete.source_version_key IS NOT NULL
      AND complete.lifecycle_status IS NOT NULL
      AND complete.lifecycle_status = 'active'
      AND complete.source_record_id = source_record_id
    OPTIONAL MATCH (complete)-[:FROM_SOURCE]->(complete_source:SourceSystem)
    WITH complete, source_system,
         [key IN collect(DISTINCT complete_source.source_key) WHERE key IS NOT NULL]
           AS complete_source_keys
    WITH complete, source_system,
         CASE WHEN size(complete_source_keys) = 1 THEN head(complete_source_keys)
              ELSE 'legacy-orphan:' +
                coalesce(complete.source_record_pk, complete.legacy_repair_id) END
           AS complete_source_system
    WHERE complete_source_system = source_system
    RETURN count(complete) AS active_count
  }
  WITH versions, active_count,
       [candidate IN versions
        WHERE coalesce(candidate.link_status, '') <> 'pending_review'] AS accepted
  WITH versions,
       CASE WHEN active_count = 0 AND size(accepted) > 0 THEN last(accepted)
            ELSE NULL END AS active_version
  UNWIND versions AS version
  SET version.lifecycle_status = CASE
        WHEN version.link_status = 'pending_review' THEN 'pending_review'
        WHEN version = active_version THEN 'active'
        ELSE 'superseded' END,
      version.is_latest = version = active_version
  RETURN count(version) AS updated
}
RETURN candidates, keyed, updated
"""


RECONCILE_PROJECTION_RELATIONSHIP_LIFECYCLE = """
MATCH (migration:DataMigration {migration_key: 'projection_relationship_lifecycle_v1'})
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration
MATCH ()-[relationship:IDENTIFIED_BY|LIVES_AT|KNOWS|HAS_FACT]->()
OPTIONAL MATCH (source:SourceRecord)
WHERE source.source_record_pk = relationship.source_record_pk
  AND (source.lifecycle_status = 'active'
    OR (source.lifecycle_status IS NULL AND source.is_latest = true))
WITH relationship,
     CASE WHEN relationship.source_record_pk IS NULL
          THEN coalesce(relationship.is_active, true)
          ELSE count(source) > 0 END AS expected_is_active
WHERE relationship.is_active IS NULL
   OR relationship.is_active <> expected_is_active
SET relationship.is_active = expected_is_active
FOREACH (_ IN CASE WHEN expected_is_active THEN [1] ELSE [] END |
  SET relationship.activated_at = coalesce(relationship.activated_at, datetime()),
      relationship.retired_at = null)
FOREACH (_ IN CASE WHEN expected_is_active THEN [] ELSE [1] END |
  SET relationship.retired_at = coalesce(relationship.retired_at, datetime()))
RETURN count(relationship) AS updated
"""


def backfill_record_type_subtypes(client: Neo4jClient) -> int:
    """Reclassify legacy ``system`` / ``public_record`` records into subtypes.

    Maps existing SourceRecords with ``record_type`` of ``system`` or the
    intermediate ``public_record`` by ``source_system`` to the current subtypes
    (identity / bankruptcy / rental_flat / relationship). Returns the number of
    records updated; ``0`` once the backfill has already run.
    """

    def _work(tx: ManagedTransaction) -> int:
        record = tx.run(queries.BACKFILL_RECORD_TYPE_SUBTYPES).single()
        return int(record["updated"]) if record is not None else 0

    updated = client.execute_write(_work)
    if updated:
        logger.info("Backfilled record_type on %d legacy 'system' source records", updated)
    return updated


def migrate_source_record_lifecycle(client: Neo4jClient) -> int:
    """Backfill immutable lifecycle state for every legacy source identity."""

    def _work(tx: ManagedTransaction) -> int:
        record = tx.run(MIGRATE_SOURCE_RECORD_LIFECYCLE).single()
        return int(record["updated"]) if record is not None else 0

    updated = client.execute_write(_work)
    if updated:
        logger.info("Migrated lifecycle state on %d source records", updated)
    return updated


def migrate_projection_relationship_lifecycle(client: Neo4jClient) -> int:
    """Backfill active state on legacy projection relationships."""

    def _work(tx: ManagedTransaction) -> int:
        record = tx.run(MIGRATE_PROJECTION_RELATIONSHIP_LIFECYCLE).single()
        return int(record["updated"]) if record is not None else 0

    updated = client.execute_write(_work)
    if updated:
        logger.info("Migrated lifecycle state on %d projection relationships", updated)
    return updated


def reconcile_source_record_lifecycle(client: Neo4jClient) -> int:
    """Repair lifecycle fields on records arriving after the marker migration."""

    def _work(tx: ManagedTransaction) -> int:
        record = tx.run(RECONCILE_SOURCE_RECORD_LIFECYCLE).single()
        return int(record["updated"]) if record is not None else 0

    return client.execute_write(_work)


def reconcile_projection_relationship_lifecycle(client: Neo4jClient) -> int:
    """Repair active state on projections arriving after the marker migration."""

    def _work(tx: ManagedTransaction) -> int:
        record = tx.run(RECONCILE_PROJECTION_RELATIONSHIP_LIFECYCLE).single()
        return int(record["updated"]) if record is not None else 0

    return client.execute_write(_work)


def apply_data_migrations(client: Neo4jClient) -> None:
    """Run every idempotent data migration in order."""
    backfill_record_type_subtypes(client)
    migrate_source_record_lifecycle(client)
    migrate_projection_relationship_lifecycle(client)
    reconcile_source_record_lifecycle(client)
    reconcile_projection_relationship_lifecycle(client)
