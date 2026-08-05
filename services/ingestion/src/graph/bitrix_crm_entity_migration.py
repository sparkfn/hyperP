"""Restart-safe entity ownership backfill for Bitrix CRM source records."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import cast

from neo4j import ManagedTransaction

from src.graph.client import Neo4jClient
from src.raw_payload import decode_raw_payload

logger = logging.getLogger(__name__)

MIGRATION_KEY = "bitrix_crm_entity_v1"
MIGRATION_BATCH_SIZE = 500

START_MIGRATION = """
MERGE (migration:DataMigration {migration_key: $migration_key})
ON CREATE SET migration.created_at = datetime()
RETURN migration.completed_at AS completed_at
"""

COMPLETE_MIGRATION = """
MATCH (migration:DataMigration {migration_key: $migration_key})
WHERE migration.completed_at IS NULL
SET migration.completed_at = datetime()
RETURN migration.completed_at AS completed_at
"""

LIST_RECORDS_FOR_BACKFILL = """
MATCH (record:SourceRecord {record_type: 'crm_deal'})
      -[:FROM_SOURCE]->(:SourceSystem {source_key: 'bitrix_chat'})
OPTIONAL MATCH (record)-[:OWNED_BY]->(owner:Entity)
WITH record, collect(owner.entity_key) AS owner_entity_keys
OPTIONAL MATCH (child:SourceRecord)-[:CHILD_OF*1..2]->(record)
WHERE child.record_type IN ['crm_history', 'call']
WITH record, owner_entity_keys, child
OPTIONAL MATCH (child)-[child_owner_relationship:OWNED_BY]->(child_owner:Entity)
WITH record,
     owner_entity_keys,
     child,
     collect(DISTINCT child_owner_relationship) AS child_owner_relationships,
     collect(DISTINCT child_owner.entity_key) AS child_owner_entity_keys
WITH record,
     owner_entity_keys,
     collect(
         CASE
             WHEN child IS NULL THEN false
             WHEN child.entity_key IS NULL
               OR trim(child.entity_key) = ''
               OR child.entity_key <> record.entity_key
               OR size(child_owner_relationships) <> 1
               OR head(child_owner_entity_keys) <> child.entity_key
             THEN true
             ELSE false
         END
     ) AS inconsistent_child_ownership
WHERE record.entity_key IS NULL
   OR trim(record.entity_key) = ''
   OR size(owner_entity_keys) <> 1
   OR head(owner_entity_keys) <> record.entity_key
   OR any(inconsistent IN inconsistent_child_ownership WHERE inconsistent)
RETURN record.source_record_pk AS source_record_pk,
       record.source_record_id AS source_record_id,
       record.raw_payload AS raw_payload
ORDER BY record.source_record_pk
LIMIT $batch_size
"""

LINK_DEAL_TO_ENTITY = """
MATCH (record:SourceRecord {source_record_pk: $source_record_pk, record_type: 'crm_deal'})
MATCH (entity:Entity {entity_key: $entity_key})
OPTIONAL MATCH (record)-[stale_owner:OWNED_BY]->(:Entity)
WITH record, entity, collect(stale_owner) AS stale_owners
FOREACH (stale_owner IN stale_owners | DELETE stale_owner)
MERGE (record)-[:OWNED_BY]->(entity)
SET record.entity_key = entity.entity_key,
    record.updated_at = datetime()
RETURN record.source_record_pk AS source_record_pk
"""

PROPAGATE_ENTITY_TO_CHILDREN = """
MATCH (deal:SourceRecord {source_record_pk: $deal_source_record_pk, record_type: 'crm_deal'})
MATCH (entity:Entity {entity_key: $entity_key})
MATCH (child:SourceRecord)-[:CHILD_OF*1..2]->(deal)
WHERE child.record_type IN ['crm_history', 'call']
OPTIONAL MATCH (child)-[stale_owner:OWNED_BY]->(:Entity)
WITH child, entity, collect(DISTINCT stale_owner) AS stale_owners
FOREACH (stale_owner IN stale_owners | DELETE stale_owner)
MERGE (child)-[:OWNED_BY]->(entity)
SET child.entity_key = entity.entity_key,
    child.updated_at = datetime()
RETURN count(child) AS updated
"""


def _deal_category_id(record: Mapping[str, object]) -> str:
    payload = decode_raw_payload(record.get("raw_payload"))
    category_id = payload.get("category_id") if payload is not None else None
    if isinstance(category_id, bool) or not isinstance(category_id, (str, int)):
        source_record_id = record.get("source_record_id")
        raise RuntimeError(f"Bitrix CRM deal {source_record_id!r} has no usable category ID")
    normalized_category_id = str(category_id).strip()
    if not normalized_category_id.isdigit():
        source_record_id = record.get("source_record_id")
        raise RuntimeError(f"Bitrix CRM deal {source_record_id!r} has no usable category ID")
    return normalized_category_id


def _normalize_mapping(category_entities: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for category_id, entity_key in category_entities.items():
        if not category_id.isdigit() or not entity_key.strip():
            raise ValueError("Bitrix CRM category entity mappings must use non-empty numeric IDs")
        normalized[category_id] = entity_key.strip()
    return normalized


def _migrate_batch(client: Neo4jClient, category_entities: Mapping[str, str]) -> int:
    def _work(tx: ManagedTransaction) -> int:
        records = list(tx.run(LIST_RECORDS_FOR_BACKFILL, batch_size=MIGRATION_BATCH_SIZE))
        for raw_record in records:
            record = cast("Mapping[str, object]", raw_record)
            source_record_pk = record.get("source_record_pk")
            if not isinstance(source_record_pk, str) or not source_record_pk:
                raise RuntimeError("Bitrix CRM deal is missing source_record_pk")
            category_id = _deal_category_id(record)
            entity_key = category_entities.get(category_id)
            if entity_key is None:
                source_record_id = record.get("source_record_id")
                raise RuntimeError(
                    f"Bitrix CRM deal {source_record_id!r} category {category_id!r} "
                    "has no entity mapping"
                )
            updated = tx.run(
                LINK_DEAL_TO_ENTITY,
                source_record_pk=source_record_pk,
                entity_key=entity_key,
            ).single()
            if updated is None:
                raise RuntimeError(
                    f"Bitrix CRM deal {source_record_pk!r} maps to unknown entity {entity_key!r}"
                )
            tx.run(
                PROPAGATE_ENTITY_TO_CHILDREN,
                deal_source_record_pk=source_record_pk,
                entity_key=entity_key,
            ).single()
        return len(records)

    return client.execute_write(_work)


def migrate_bitrix_crm_entities(
    client: Neo4jClient,
    category_entities: Mapping[str, str],
) -> int:
    """Backfill record-scoped ownership for existing Bitrix CRM hierarchies."""
    normalized_mapping = _normalize_mapping(category_entities)

    def _start(tx: ManagedTransaction) -> bool:
        raw_marker = tx.run(START_MIGRATION, migration_key=MIGRATION_KEY).single()
        if raw_marker is None:
            raise RuntimeError("Bitrix CRM entity migration marker could not be created")
        marker = cast("Mapping[str, object]", raw_marker)
        return marker.get("completed_at") is None

    if not client.execute_write(_start):
        return 0

    updated = 0
    while True:
        batch_size = _migrate_batch(client, normalized_mapping)
        updated += batch_size
        if batch_size < MIGRATION_BATCH_SIZE:
            break

    def _complete(tx: ManagedTransaction) -> None:
        completed = tx.run(COMPLETE_MIGRATION, migration_key=MIGRATION_KEY).single()
        if completed is None:
            raise RuntimeError("Bitrix CRM entity migration could not be marked complete")

    client.execute_write(_complete)
    if updated:
        logger.info("Backfilled record-scoped ownership on %d Bitrix CRM deals", updated)
    return updated
