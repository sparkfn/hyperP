"""One-time graph migration from legacy Fundbox source names."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import cast

from neo4j import ManagedTransaction

from src.graph.client import Neo4jClient

logger = logging.getLogger(__name__)

SOURCE_KEY_MAPPINGS: tuple[tuple[str, str], ...] = (
    ("fundbox_consumer_backend", "fundbox"),
    ("fundbox_consumer_backend:contacts", "fundbox:contacts"),
    ("fundbox_consumer_backend:legacy", "fundbox:legacy"),
    ("fundbox_consumer_backend:merged", "fundbox:merged"),
    ("fundbox_consumer_backend:sales", "fundbox:sales"),
)

START_MIGRATION = """
MERGE (migration:DataMigration {migration_key: 'fundbox_source_keys_v1'})
ON CREATE SET migration.created_at = datetime()
RETURN migration.completed_at AS completed_at
"""


REHOME_SOURCE_LINKS = """
MATCH (canonical:SourceSystem {source_key: $canonical_key})
OPTIONAL MATCH (legacy:SourceSystem {source_key: $legacy_key})
FOREACH (_ IN CASE WHEN legacy IS NULL THEN [] ELSE [1] END |
  SET canonical.source_system_id = coalesce(
        legacy.source_system_id, canonical.source_system_id
      ),
      canonical.created_at = coalesce(legacy.created_at, canonical.created_at),
      canonical.updated_at = datetime()
)
CALL {
  WITH canonical, legacy
  OPTIONAL MATCH (record:SourceRecord)-[legacy_link:FROM_SOURCE]->(legacy)
  WITH canonical, record, legacy_link
  WHERE record IS NOT NULL
  MERGE (record)-[:FROM_SOURCE]->(canonical)
  DELETE legacy_link
  RETURN count(record) AS records
}
CALL {
  WITH canonical, legacy
  OPTIONAL MATCH (run:IngestRun)-[legacy_link:FROM_SOURCE]->(legacy)
  WITH canonical, run, legacy_link
  WHERE run IS NOT NULL
  MERGE (run)-[:FROM_SOURCE]->(canonical)
  DELETE legacy_link
  RETURN count(run) AS runs
}
CALL {
  WITH canonical, legacy
  OPTIONAL MATCH (order:Order)-[legacy_link:SOLD_THROUGH]->(legacy)
  WITH canonical, order, legacy_link
  WHERE order IS NOT NULL
  MERGE (order)-[:SOLD_THROUGH]->(canonical)
  DELETE legacy_link
  RETURN count(order) AS orders
}
RETURN records + runs + orders AS updated
"""


REWRITE_SOURCE_PROVENANCE = """
CALL {
  MATCH (node)
  WHERE node.source_system_key = $legacy_key
  SET node.source_system_key = $canonical_key
  RETURN count(node) AS nodes
}
CALL {
  MATCH ()-[relationship]->()
  WHERE relationship.source_system_key = $legacy_key
  SET relationship.source_system_key = $canonical_key
  RETURN count(relationship) AS relationships
}
CALL {
  MATCH (run:IngestRun)
  WHERE run.source_key = $legacy_key
  SET run.source_key = $canonical_key
  RETURN count(run) AS runs
}
CALL {
  MATCH (lock:SourceRecordIdentityLock)
  WHERE lock.source_system = $legacy_key
  SET lock.source_system = $canonical_key
  RETURN count(lock) AS locks
}
CALL {
  MATCH (version:SourceRecord)
  WHERE version.source_system = $legacy_key
     OR version.migration_source_system = $legacy_key
  FOREACH (_ IN CASE WHEN version.source_system = $legacy_key THEN [1] ELSE [] END |
    SET version.source_system = $canonical_key)
  FOREACH (_ IN CASE WHEN version.migration_source_system = $legacy_key THEN [1] ELSE [] END |
    SET version.migration_source_system = $canonical_key)
  RETURN count(version) AS versions
}
CALL {
  MATCH (migration:DataMigration)
  WHERE migration.current_source_system = $legacy_key
  SET migration.current_source_system = $canonical_key
  RETURN count(migration) AS migrations
}
CALL {
  MATCH (vehicle:Vehicle)
  WHERE $legacy_key IN coalesce(vehicle.source_systems, [])
  SET vehicle.source_systems = [source_key IN vehicle.source_systems |
    CASE WHEN source_key = $legacy_key THEN $canonical_key ELSE source_key END]
  RETURN count(vehicle) AS vehicles
}
RETURN nodes + relationships + runs + locks + versions + migrations + vehicles AS updated
"""


REWRITE_SOURCE_RECORD_REFERENCES = """
CALL {
  MATCH (node)
  WHERE node.source_record_id STARTS WITH 'fundbox_consumer_backend-'
  SET node.source_record_id = replace(
    node.source_record_id, 'fundbox_consumer_backend-', 'fundbox-'
  )
  RETURN count(node) AS nodes
}
CALL {
  MATCH ()-[relationship]->()
  WHERE relationship.source_record_id STARTS WITH 'fundbox_consumer_backend-'
  SET relationship.source_record_id = replace(
    relationship.source_record_id, 'fundbox_consumer_backend-', 'fundbox-'
  )
  RETURN count(relationship) AS relationships
}
CALL {
  MATCH (version:SourceRecord)
  WHERE version.migration_source_record_id STARTS WITH 'fundbox_consumer_backend-'
  SET version.migration_source_record_id = replace(
    version.migration_source_record_id, 'fundbox_consumer_backend-', 'fundbox-'
  )
  RETURN count(version) AS versions
}
CALL {
  MATCH (migration:DataMigration)
  WHERE migration.current_source_record_id STARTS WITH 'fundbox_consumer_backend-'
  SET migration.current_source_record_id = replace(
    migration.current_source_record_id, 'fundbox_consumer_backend-', 'fundbox-'
  )
  RETURN count(migration) AS migrations
}
CALL {
  MATCH (record:SourceRecord)-[:FROM_SOURCE]->(source:SourceSystem)
  WHERE source.source_key = 'fundbox' OR source.source_key STARTS WITH 'fundbox:'
  SET record.source_version_key = NULL,
      record.raw_payload = replace(
        record.raw_payload, 'fundbox_consumer_backend', 'fundbox'
      ),
      record.normalized_payload = replace(
        record.normalized_payload, 'fundbox_consumer_backend', 'fundbox'
      )
  RETURN count(record) AS records
}
RETURN nodes + relationships + versions + migrations + records AS updated
"""


REMOVE_LEGACY_OWNERSHIP = """
MATCH (legacy:SourceSystem)
WHERE legacy.source_key = 'fundbox_consumer_backend'
   OR legacy.source_key STARTS WITH 'fundbox_consumer_backend:'
OPTIONAL MATCH (legacy)-[ownership:OPERATED_BY]->(:Entity)
DELETE ownership
RETURN count(ownership) AS removed
"""


CHECK_LEGACY_SOURCE_LINKS = """
MATCH (legacy:SourceSystem)
WHERE legacy.source_key = 'fundbox_consumer_backend'
   OR legacy.source_key STARTS WITH 'fundbox_consumer_backend:'
OPTIONAL MATCH (legacy)-[relationship]-()
RETURN count(relationship) AS remaining
"""


DELETE_LEGACY_SOURCES = """
MATCH (legacy:SourceSystem)
WHERE legacy.source_key = 'fundbox_consumer_backend'
   OR legacy.source_key STARTS WITH 'fundbox_consumer_backend:'
DELETE legacy
RETURN count(legacy) AS removed
"""


COMPLETE_MIGRATION = """
MATCH (migration:DataMigration {migration_key: 'fundbox_source_keys_v1'})
WHERE migration.completed_at IS NULL
SET migration.completed_at = datetime()
RETURN migration.completed_at AS completed_at
"""


def migrate_fundbox_source_keys(client: Neo4jClient) -> int:
    """Rename legacy Fundbox source keys and their denormalized provenance."""

    def _work(tx: ManagedTransaction) -> int:
        raw_marker = tx.run(START_MIGRATION).single()
        if raw_marker is None:
            raise RuntimeError("Fundbox source migration marker could not be created")
        marker = cast("Mapping[str, object]", raw_marker)
        if marker.get("completed_at") is not None:
            return 0

        updated = 0
        for legacy_key, canonical_key in SOURCE_KEY_MAPPINGS:
            link_result = tx.run(
                REHOME_SOURCE_LINKS,
                legacy_key=legacy_key,
                canonical_key=canonical_key,
            ).single()
            provenance_result = tx.run(
                REWRITE_SOURCE_PROVENANCE,
                legacy_key=legacy_key,
                canonical_key=canonical_key,
            ).single()
            if link_result is not None:
                updated += int(link_result["updated"])
            if provenance_result is not None:
                updated += int(provenance_result["updated"])

        reference_result = tx.run(REWRITE_SOURCE_RECORD_REFERENCES).single()
        if reference_result is not None:
            updated += int(reference_result["updated"])
        tx.run(REMOVE_LEGACY_OWNERSHIP).single()
        link_check = tx.run(CHECK_LEGACY_SOURCE_LINKS).single()
        if link_check is None or int(link_check["remaining"]) != 0:
            raise RuntimeError("Fundbox source migration left unexpected legacy relationships")
        tx.run(DELETE_LEGACY_SOURCES).single()
        if tx.run(COMPLETE_MIGRATION).single() is None:
            raise RuntimeError("Fundbox source migration could not be marked complete")
        return updated

    updated = client.execute_write(_work)
    if updated:
        logger.info("Migrated %d Fundbox source references", updated)
    return updated
