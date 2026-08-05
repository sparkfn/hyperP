"""Restart-safe entity ownership backfill for scoped Bitrix CRM source records."""

from __future__ import annotations

import json
import logging
from collections.abc import Collection, Mapping
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
WITH migration, migration.scope_signature AS previous_scope_signature
SET migration.completed_at = CASE
        WHEN previous_scope_signature IS NULL OR previous_scope_signature <> $scope_signature
        THEN NULL
        ELSE migration.completed_at
    END,
    migration.last_source_record_pk = CASE
        WHEN previous_scope_signature IS NULL OR previous_scope_signature <> $scope_signature
        THEN NULL
        ELSE migration.last_source_record_pk
    END,
    migration.scope_signature = $scope_signature
RETURN migration.completed_at AS completed_at,
       migration.last_source_record_pk AS last_source_record_pk
"""

COMPLETE_MIGRATION = """
MATCH (migration:DataMigration {migration_key: $migration_key})
WHERE migration.completed_at IS NULL
  AND migration.scope_signature = $scope_signature
SET migration.completed_at = datetime(),
    migration.last_source_record_pk = NULL
RETURN migration.completed_at AS completed_at
"""

ADVANCE_MIGRATION_CURSOR = """
MATCH (migration:DataMigration {migration_key: $migration_key})
WHERE migration.completed_at IS NULL
  AND migration.scope_signature = $scope_signature
SET migration.last_source_record_pk = $last_source_record_pk,
    migration.updated_at = datetime()
RETURN migration.last_source_record_pk AS last_source_record_pk
"""

LIST_RECORDS_FOR_BACKFILL = """
MATCH (record:SourceRecord {record_type: 'crm_deal'})
      -[:FROM_SOURCE]->(:SourceSystem {source_key: 'bitrix_chat'})
WHERE $after_source_record_pk IS NULL OR record.source_record_pk > $after_source_record_pk
RETURN record.source_record_pk AS source_record_pk,
       record.source_record_id AS source_record_id,
       record.raw_payload AS raw_payload
ORDER BY record.source_record_pk
LIMIT $batch_size
"""

VALIDATE_MAPPED_ENTITIES = """
UNWIND $entity_keys AS entity_key
OPTIONAL MATCH (entity:Entity {entity_key: entity_key})
WITH entity_key, count(entity) AS matches
WHERE matches <> 1
RETURN entity_key
ORDER BY entity_key
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


def _deal_category_id(record: Mapping[str, object]) -> str | None:
    payload = decode_raw_payload(record.get("raw_payload"))
    category_id = payload.get("category_id") if payload is not None else None
    if isinstance(category_id, bool) or not isinstance(category_id, (str, int)):
        return None
    normalized_category_id = str(category_id).strip()
    return normalized_category_id if normalized_category_id.isdigit() else None


def _normalize_mapping(category_entities: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for category_id, entity_key in category_entities.items():
        if not category_id.isdigit() or not entity_key.strip():
            raise ValueError("Bitrix CRM category entity mappings must use non-empty numeric IDs")
        normalized[category_id] = entity_key.strip()
    return normalized


def _normalize_included_categories(category_ids: Collection[str]) -> frozenset[str]:
    normalized: set[str] = set()
    for category_id in category_ids:
        if not category_id.isdigit():
            raise ValueError("Included Bitrix CRM category IDs must be numeric")
        normalized.add(category_id)
    return frozenset(normalized)


def _validate_included_category_mappings(
    included_category_ids: Collection[str],
    category_entities: Mapping[str, str],
) -> frozenset[str]:
    included_categories = _normalize_included_categories(included_category_ids)
    missing_category_ids = sorted(included_categories.difference(category_entities))
    if missing_category_ids:
        formatted_ids = ", ".join(missing_category_ids)
        raise ValueError(f"Included Bitrix CRM categories have no entity mapping: {formatted_ids}")
    return included_categories


def _validate_mapped_entities(
    client: Neo4jClient,
    included_categories: Collection[str],
    category_entities: Mapping[str, str],
) -> None:
    entity_keys = sorted({category_entities[category_id] for category_id in included_categories})
    if not entity_keys:
        return

    def _work(tx: ManagedTransaction) -> list[str]:
        records = tx.run(VALIDATE_MAPPED_ENTITIES, entity_keys=entity_keys)
        missing: list[str] = []
        for raw_record in records:
            record = cast("Mapping[str, object]", raw_record)
            entity_key = record.get("entity_key")
            if not isinstance(entity_key, str) or not entity_key:
                raise RuntimeError("Bitrix CRM entity validation returned an invalid entity key")
            missing.append(entity_key)
        return missing

    missing_entity_keys = client.execute_read(_work)
    if missing_entity_keys:
        categories_by_entity: dict[str, list[str]] = {}
        for category_id in sorted(included_categories):
            entity_key = category_entities[category_id]
            categories_by_entity.setdefault(entity_key, []).append(category_id)
        details = ", ".join(
            f"{entity_key} (categories {', '.join(categories_by_entity[entity_key])})"
            for entity_key in missing_entity_keys
        )
        raise RuntimeError(f"Included Bitrix CRM categories map to unknown entities: {details}")


def _scope_signature(
    included_categories: Collection[str],
    category_entities: Mapping[str, str],
) -> str:
    scoped_mapping = [
        [category_id, category_entities[category_id]]
        for category_id in sorted(included_categories)
    ]
    return json.dumps(scoped_mapping, separators=(",", ":"))


def _migrate_record(
    tx: ManagedTransaction,
    record: Mapping[str, object],
    included_categories: Collection[str],
    category_entities: Mapping[str, str],
) -> tuple[str, bool]:
    source_record_pk = record.get("source_record_pk")
    if not isinstance(source_record_pk, str) or not source_record_pk:
        raise RuntimeError("Bitrix CRM deal is missing source_record_pk")
    category_id = _deal_category_id(record)
    if category_id is None or category_id not in included_categories:
        return source_record_pk, False
    entity_key = category_entities[category_id]
    linked = tx.run(
        LINK_DEAL_TO_ENTITY,
        source_record_pk=source_record_pk,
        entity_key=entity_key,
    ).single()
    if linked is None:
        raise RuntimeError(
            f"Bitrix CRM deal {source_record_pk!r} maps to unknown entity {entity_key!r}"
        )
    tx.run(
        PROPAGATE_ENTITY_TO_CHILDREN,
        deal_source_record_pk=source_record_pk,
        entity_key=entity_key,
    ).single()
    return source_record_pk, True


def _migrate_batch(
    client: Neo4jClient,
    *,
    after_source_record_pk: str | None,
    included_categories: Collection[str],
    category_entities: Mapping[str, str],
    scope_signature: str,
) -> tuple[int, int, str | None]:
    def _work(tx: ManagedTransaction) -> tuple[int, int, str | None]:
        records = list(
            tx.run(
                LIST_RECORDS_FOR_BACKFILL,
                after_source_record_pk=after_source_record_pk,
                batch_size=MIGRATION_BATCH_SIZE,
            )
        )
        if not records:
            return 0, 0, None
        updated = 0
        last_source_record_pk: str | None = None
        for raw_record in records:
            record = cast("Mapping[str, object]", raw_record)
            last_source_record_pk, record_updated = _migrate_record(
                tx,
                record,
                included_categories,
                category_entities,
            )
            updated += int(record_updated)
        if last_source_record_pk is None:
            raise RuntimeError("Bitrix CRM migration batch has no source_record_pk")
        advanced = tx.run(
            ADVANCE_MIGRATION_CURSOR,
            migration_key=MIGRATION_KEY,
            scope_signature=scope_signature,
            last_source_record_pk=last_source_record_pk,
        ).single()
        if advanced is None:
            raise RuntimeError("Bitrix CRM entity migration cursor could not be advanced")
        return len(records), updated, last_source_record_pk

    scanned, updated, cursor = client.execute_write(_work)
    if scanned == 0:
        return scanned, updated, None
    if cursor is None:
        raise RuntimeError("Bitrix CRM migration batch has no source_record_pk")
    return scanned, updated, cursor


def _start_migration(client: Neo4jClient, scope_signature: str) -> str | None | bool:
    def _work(tx: ManagedTransaction) -> str | None | bool:
        raw_marker = tx.run(
            START_MIGRATION,
            migration_key=MIGRATION_KEY,
            scope_signature=scope_signature,
        ).single()
        if raw_marker is None:
            raise RuntimeError("Bitrix CRM entity migration marker could not be created")
        marker = cast("Mapping[str, object]", raw_marker)
        if marker.get("completed_at") is not None:
            return False
        cursor = marker.get("last_source_record_pk")
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise RuntimeError("Bitrix CRM entity migration marker has an invalid cursor")
        return cursor

    return client.execute_write(_work)


def _complete_migration(client: Neo4jClient, scope_signature: str) -> None:
    def _work(tx: ManagedTransaction) -> None:
        completed = tx.run(
            COMPLETE_MIGRATION,
            migration_key=MIGRATION_KEY,
            scope_signature=scope_signature,
        ).single()
        if completed is None:
            raise RuntimeError("Bitrix CRM entity migration could not be marked complete")

    client.execute_write(_work)


def migrate_bitrix_crm_entities(
    client: Neo4jClient,
    category_entities: Mapping[str, str],
    included_category_ids: Collection[str],
) -> int:
    """Backfill ownership for explicitly included Bitrix CRM deal categories."""
    normalized_mapping = _normalize_mapping(category_entities)
    included_categories = _validate_included_category_mappings(
        included_category_ids, normalized_mapping
    )
    if not included_categories:
        return 0
    _validate_mapped_entities(client, included_categories, normalized_mapping)
    scope_signature = _scope_signature(included_categories, normalized_mapping)

    cursor = _start_migration(client, scope_signature)
    if cursor is False:
        return 0
    if cursor is not None and not isinstance(cursor, str):
        raise RuntimeError("Bitrix CRM entity migration marker has an invalid cursor")

    updated = 0
    while True:
        scanned, batch_updated, next_cursor = _migrate_batch(
            client,
            after_source_record_pk=cursor,
            included_categories=included_categories,
            category_entities=normalized_mapping,
            scope_signature=scope_signature,
        )
        updated += batch_updated
        if scanned == 0:
            break
        # Cursor is persisted atomically with the batch, and the last source-record
        # primary key is deterministic because the query orders by that key.
        cursor = next_cursor

    _complete_migration(client, scope_signature)
    if updated:
        logger.info("Backfilled record-scoped ownership on %d Bitrix CRM deals", updated)
    return updated
