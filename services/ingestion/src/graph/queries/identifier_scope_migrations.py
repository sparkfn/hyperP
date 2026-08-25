"""Batched migration queries for instance-scoped CRM identifier identity."""

from __future__ import annotations

_RELATIONSHIP_MERGE = """
ON CREATE SET scoped_rel = properties(legacy_rel)
ON MATCH SET
  scoped_rel.is_active = coalesce(scoped_rel.is_active, false)
    OR coalesce(legacy_rel.is_active, false),
  scoped_rel.is_verified = coalesce(scoped_rel.is_verified, false)
    OR coalesce(legacy_rel.is_verified, false),
  scoped_rel.verification_method = coalesce(
    scoped_rel.verification_method, legacy_rel.verification_method
  ),
  scoped_rel.quality_flag = CASE
    WHEN scoped_rel.quality_flag = 'valid' OR legacy_rel.quality_flag = 'valid' THEN 'valid'
    WHEN scoped_rel.quality_flag = 'partial_parse'
      OR legacy_rel.quality_flag = 'partial_parse' THEN 'partial_parse'
    ELSE coalesce(scoped_rel.quality_flag, legacy_rel.quality_flag)
  END,
  scoped_rel.first_seen_at = CASE
    WHEN scoped_rel.first_seen_at IS NULL THEN legacy_rel.first_seen_at
    WHEN legacy_rel.first_seen_at IS NULL THEN scoped_rel.first_seen_at
    WHEN scoped_rel.first_seen_at <= legacy_rel.first_seen_at THEN scoped_rel.first_seen_at
    ELSE legacy_rel.first_seen_at
  END,
  scoped_rel.last_seen_at = CASE
    WHEN scoped_rel.last_seen_at IS NULL THEN legacy_rel.last_seen_at
    WHEN legacy_rel.last_seen_at IS NULL THEN scoped_rel.last_seen_at
    WHEN scoped_rel.last_seen_at >= legacy_rel.last_seen_at THEN scoped_rel.last_seen_at
    ELSE legacy_rel.last_seen_at
  END,
  scoped_rel.last_confirmed_at = CASE
    WHEN scoped_rel.last_confirmed_at IS NULL THEN legacy_rel.last_confirmed_at
    WHEN legacy_rel.last_confirmed_at IS NULL THEN scoped_rel.last_confirmed_at
    WHEN scoped_rel.last_confirmed_at >= legacy_rel.last_confirmed_at
      THEN scoped_rel.last_confirmed_at
    ELSE legacy_rel.last_confirmed_at
  END
"""


def _migration_lock(body: str) -> str:
    return f"""
MERGE (migration:DataMigration {{migration_key: $migration_key}})
ON CREATE SET migration.created_at = datetime(), migration.lock_version = 0
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration
{body}
"""


MIGRATE_CRM_IDENTIFIER_RELATIONSHIPS_BATCH = _migration_lock(
    f"""
CALL {{
  MATCH (legacy:Identifier)<-[legacy_rel:IDENTIFIED_BY]-(person:Person)
  WHERE legacy.identifier_type IN $crm_identifier_types
    AND (legacy.identifier_scope IS NULL
      OR legacy.identifier_scope = $legacy_source_instance_id)
  OPTIONAL MATCH (source:SourceRecord {{source_record_pk: legacy_rel.source_record_pk}})
  OPTIONAL MATCH (source)-[:FROM_SOURCE]->(source_system:SourceSystem)
  WITH legacy, legacy_rel, person, source,
       [key IN collect(DISTINCT source_system.source_key) WHERE key IS NOT NULL]
         AS source_system_keys
  WITH legacy, legacy_rel, person,
       CASE
         WHEN size(source_system_keys) = 1
           AND head(source_system_keys) = $bitrix_source_system_key
           AND coalesce(source.source_instance_id, $legacy_source_instance_id)
             = $legacy_source_instance_id
           AND $bitrix_source_instance_id IS NOT NULL
         THEN $bitrix_source_instance_id
         ELSE coalesce(source.source_instance_id, $legacy_source_instance_id)
       END AS identifier_scope
  WHERE legacy.identifier_scope IS NULL
    OR legacy.identifier_scope <> identifier_scope
  ORDER BY elementId(legacy_rel)
  LIMIT $batch_size
  RETURN legacy, legacy_rel, person, identifier_scope
}}
MERGE (scoped:Identifier {{
  identifier_type: legacy.identifier_type,
  identifier_scope: identifier_scope,
  normalized_value: legacy.normalized_value
}})
ON CREATE SET
  scoped.identifier_id = randomUUID(),
  scoped.source_instance_id = identifier_scope,
  scoped.created_at = coalesce(legacy.created_at, datetime())
CALL (person, legacy_rel, scoped) {{
  WITH person, legacy_rel, scoped
  WHERE legacy_rel.source_system_key IS NOT NULL
    AND legacy_rel.source_record_pk IS NOT NULL
  MERGE (person)-[scoped_rel:IDENTIFIED_BY {{
    source_system_key: legacy_rel.source_system_key,
    source_record_pk: legacy_rel.source_record_pk
  }}]->(scoped)
  {_RELATIONSHIP_MERGE}
  RETURN 1 AS migrated
  UNION
  WITH person, legacy_rel, scoped
  WHERE legacy_rel.source_system_key IS NULL OR legacy_rel.source_record_pk IS NULL
  CREATE (person)-[scoped_rel:IDENTIFIED_BY]->(scoped)
  SET scoped_rel = properties(legacy_rel)
  RETURN 1 AS migrated
}}
DELETE legacy_rel
RETURN count(legacy_rel) AS updated
"""
)


DELETE_EMPTY_UNSCOPED_CRM_IDENTIFIERS_BATCH = _migration_lock(
    """
CALL {
  MATCH (legacy:Identifier)
  WHERE (legacy.identifier_scope IS NULL
      OR legacy.identifier_scope = $legacy_source_instance_id)
    AND legacy.identifier_type IN $crm_identifier_types
    AND NOT (legacy)--()
  WITH legacy
  ORDER BY elementId(legacy)
  LIMIT $batch_size
  RETURN legacy
}
DELETE legacy
RETURN count(legacy) AS deleted
"""
)


BACKFILL_IDENTIFIER_SCOPES_BATCH = _migration_lock(
    """
CALL {
  MATCH (id:Identifier)
  WHERE id.identifier_scope IS NULL
  WITH id
  ORDER BY elementId(id)
  LIMIT $batch_size
  RETURN id
}
SET id.identifier_scope = CASE
      WHEN id.identifier_type IN $crm_identifier_types THEN $legacy_source_instance_id
      ELSE $global_identifier_scope
    END,
    id.source_instance_id = CASE
      WHEN id.identifier_type IN $crm_identifier_types THEN $legacy_source_instance_id
      ELSE NULL
    END
RETURN count(id) AS updated
"""
)


CONSOLIDATE_SCOPED_IDENTIFIER_DUPLICATES_BATCH = _migration_lock(
    f"""
CALL {{
  MATCH (id:Identifier)
  WHERE id.identifier_scope IS NOT NULL
  WITH id.identifier_type AS identifier_type,
       id.identifier_scope AS identifier_scope,
       id.normalized_value AS normalized_value,
       id
  ORDER BY elementId(id)
  WITH identifier_type, identifier_scope, normalized_value, collect(id) AS identifiers
  WHERE size(identifiers) > 1
  UNWIND tail(identifiers) AS duplicate
  WITH head(identifiers) AS survivor, duplicate
  OPTIONAL MATCH (other)-[unexpected]-(duplicate)
  WHERE NOT (other:Person AND type(unexpected) = 'IDENTIFIED_BY'
    AND endNode(unexpected) = duplicate)
  WITH survivor, duplicate, count(unexpected) AS unexpected_count
  WHERE unexpected_count = 0
  LIMIT $batch_size
  RETURN survivor, duplicate
}}
OPTIONAL MATCH (person:Person)-[legacy_rel:IDENTIFIED_BY]->(duplicate)
CALL (person, legacy_rel, survivor) {{
  WITH person, legacy_rel, survivor
  WHERE legacy_rel IS NOT NULL
    AND legacy_rel.source_system_key IS NOT NULL
    AND legacy_rel.source_record_pk IS NOT NULL
  MERGE (person)-[scoped_rel:IDENTIFIED_BY {{
    source_system_key: legacy_rel.source_system_key,
    source_record_pk: legacy_rel.source_record_pk
  }}]->(survivor)
  {_RELATIONSHIP_MERGE}
  RETURN 1 AS migrated
  UNION
  WITH person, legacy_rel, survivor
  WHERE legacy_rel IS NOT NULL
    AND (legacy_rel.source_system_key IS NULL OR legacy_rel.source_record_pk IS NULL)
  CREATE (person)-[scoped_rel:IDENTIFIED_BY]->(survivor)
  SET scoped_rel = properties(legacy_rel)
  RETURN 1 AS migrated
  UNION
  WITH legacy_rel
  WHERE legacy_rel IS NULL
  RETURN 0 AS migrated
}}
WITH DISTINCT duplicate
DETACH DELETE duplicate
RETURN count(duplicate) AS consolidated
"""
)
