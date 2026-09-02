"""Stored CRM deal-count projection queries."""

from __future__ import annotations

CRM_DEAL_COUNT_INDEX_NAME = "idx_person_crm_deal_count"
CRM_DEAL_COUNT_INDEX_CYPHER = (
    "CREATE INDEX idx_person_crm_deal_count IF NOT EXISTS FOR (p:Person) ON (p.crm_deal_count)"
)

_AUTHORITY_MATCH = """
OPTIONAL MATCH (deal:SourceRecord {record_type: 'crm_deal'})-[link:LINKED_TO]->(person)
WHERE coalesce(link.is_active, true) = true
  AND (deal.history_family IS NULL OR deal.history_family = 'activity')
  AND (deal.lifecycle_status = 'active'
    OR (deal.lifecycle_status IS NULL AND deal.is_latest = true))
  AND EXISTS {
    MATCH (deal)-[:FROM_SOURCE]->(:SourceSystem {source_key: 'bitrix_chat'})
  }
"""

RECOMPUTE_PERSON_CRM_DEAL_COUNTS = f"""
UNWIND $person_ids AS person_id
MATCH (person:Person {{person_id: person_id}})
WITH DISTINCT person
ORDER BY person.person_id
SET person.crm_deal_count_lock_version =
    coalesce(person.crm_deal_count_lock_version, 0) + 1
WITH person
CALL (person) {{
  {_AUTHORITY_MATCH}
  RETURN count(DISTINCT deal) AS authoritative_count
}}
SET person.crm_deal_count = authoritative_count,
    person.crm_deal_count_updated_at = datetime()
RETURN person.person_id AS person_id, authoritative_count
ORDER BY person_id
"""

RECOMPUTE_SOURCE_PERSON_CRM_DEAL_COUNTS = f"""
UNWIND $source_record_pks AS source_record_pk
MATCH (:SourceRecord {{source_record_pk: source_record_pk, record_type: 'crm_deal'}})
      -[link:LINKED_TO]->(person:Person)
WHERE coalesce(link.is_active, true) = true
WITH DISTINCT person
ORDER BY person.person_id
SET person.crm_deal_count_lock_version =
    coalesce(person.crm_deal_count_lock_version, 0) + 1
WITH person
CALL (person) {{
  {_AUTHORITY_MATCH}
  RETURN count(DISTINCT deal) AS authoritative_count
}}
SET person.crm_deal_count = authoritative_count,
    person.crm_deal_count_updated_at = datetime()
RETURN person.person_id AS person_id, authoritative_count
ORDER BY person_id
"""

START_CRM_DEAL_COUNT_MIGRATION = """
MERGE (migration:DataMigration {migration_key: $migration_key})
ON CREATE SET migration.created_at = datetime(), migration.status = 'pending'
SET migration.completed_at = CASE WHEN $force THEN NULL ELSE migration.completed_at END,
    migration.last_person_id = CASE WHEN $force THEN NULL ELSE migration.last_person_id END,
    migration.status = CASE WHEN $force OR migration.completed_at IS NULL
      THEN 'running' ELSE migration.status END,
    migration.updated_at = datetime()
RETURN migration.completed_at IS NOT NULL AS completed
"""

BACKFILL_CRM_DEAL_COUNTS_BATCH = f"""
MATCH (migration:DataMigration {{migration_key: $migration_key}})
WHERE migration.completed_at IS NULL
CALL (migration) {{
  MATCH (person:Person)
  WHERE person.person_id > coalesce(migration.last_person_id, '')
  WITH person
  ORDER BY person.person_id
  LIMIT $batch_size
  RETURN collect(person) AS batch
}}
UNWIND batch AS person
WITH migration, person
ORDER BY person.person_id
SET person.crm_deal_count_lock_version =
    coalesce(person.crm_deal_count_lock_version, 0) + 1
WITH migration, person
CALL (person) {{
  {_AUTHORITY_MATCH}
  RETURN count(DISTINCT deal) AS authoritative_count
}}
SET person.crm_deal_count = authoritative_count,
    person.crm_deal_count_updated_at = datetime()
WITH migration, count(person) AS updated_count, max(person.person_id) AS last_person_id
SET migration.last_person_id = coalesce(last_person_id, migration.last_person_id),
    migration.updated_at = datetime()
RETURN updated_count
"""

CRM_DEAL_COUNT_INVARIANT_COUNTS = f"""
MATCH (person:Person)
CALL (person) {{
  {_AUTHORITY_MATCH}
  RETURN count(DISTINCT deal) AS authoritative_count
}}
WITH person, authoritative_count,
  CASE
    WHEN person.crm_deal_count IS NULL THEN true
    WHEN NOT valueType(person.crm_deal_count) STARTS WITH 'INTEGER' THEN true
    WHEN person.crm_deal_count < 0 THEN true
    ELSE false
  END AS invalid
RETURN count(CASE WHEN invalid THEN 1 END) AS invalid_person_count,
       count(CASE WHEN NOT invalid AND person.crm_deal_count <> authoritative_count
         THEN 1 END) AS drifted_person_count
"""

COMPLETE_CRM_DEAL_COUNT_MIGRATION = f"""
MATCH (migration:DataMigration {{migration_key: $migration_key}})
CALL (migration) {{
  {CRM_DEAL_COUNT_INVARIANT_COUNTS}
}}
SET migration.last_person_id = NULL,
    migration.status = CASE
      WHEN invalid_person_count = 0 AND drifted_person_count = 0 THEN 'complete'
      ELSE 'verification_failed'
    END,
    migration.updated_at = datetime()
FOREACH (_ IN CASE WHEN invalid_person_count = 0 AND drifted_person_count = 0
  THEN [1] ELSE [] END |
  SET migration.completed_at = coalesce(migration.completed_at, datetime())
)
RETURN invalid_person_count, drifted_person_count,
       migration.completed_at IS NOT NULL AS completed
"""

SHOW_CRM_DEAL_COUNT_INDEX = """
SHOW INDEXES YIELD name, type, entityType, labelsOrTypes, properties, state, failureMessage
WHERE name = $index_name
RETURN name, type, entityType, labelsOrTypes, properties, state, failureMessage
"""
