"""One-off maintenance / migration Cypher run at ingestion startup.

These statements are idempotent so they can be applied on every run (after the
schema and entity/source bootstrap) without harm.
"""

from __future__ import annotations

#: Reclassify legacy ``record_type = 'system'`` and intermediate
#: ``record_type = 'public_record'`` SourceRecords into the current subtypes
#: (identity / bankruptcy / rental_flat / relationship). Keyed on
#: ``source_system`` so the mapping matches what the connectors now emit.
#: Idempotent: after the first pass no ``'system'`` / ``'public_record'`` rows
#: remain, so a re-run updates 0.
BACKFILL_RECORD_TYPE_SUBTYPES = """
MATCH (sr:SourceRecord)
WHERE sr.record_type IN ['system', 'public_record']
SET sr.record_type = CASE
    WHEN sr.source_system ENDS WITH ':contacts' THEN 'relationship'
    WHEN sr.source_system = 'sgbankruptcy'  THEN 'bankruptcy'
    WHEN sr.source_system = 'sgrentalflats' THEN 'rental_flat'
    ELSE 'identity'
END
RETURN count(sr) AS updated
"""

# Backfill the stored completeness score required by the indexed default Person
# listing. The score is a pure derivative of the five persisted golden-profile
# fields, so this repair must not trigger a full survivorship recomputation.
# Numeric values must be finite and within the domain score range.
_PERSON_COMPLETENESS_INVALID = """CASE
  WHEN p.profile_completeness_score IS NULL THEN true
  WHEN valueType(p.profile_completeness_score) STARTS WITH 'INTEGER'
    OR valueType(p.profile_completeness_score) STARTS WITH 'FLOAT' THEN
    isNaN(toFloat(p.profile_completeness_score))
    OR p.profile_completeness_score < 0.0
    OR p.profile_completeness_score > 1.0
  ELSE true
END"""


COUNT_MISSING_PERSON_COMPLETENESS_SCORES = f"""
MATCH (p:Person)
WHERE p.status <> 'merged'
  AND ({_PERSON_COMPLETENESS_INVALID})
RETURN count(p) AS missing_count
"""


START_PERSON_COMPLETENESS_MIGRATION = """
MERGE (migration:DataMigration {migration_key: $migration_key})
ON CREATE SET migration.created_at = datetime()
WITH migration
SET migration.completed_at = CASE WHEN $force THEN NULL ELSE migration.completed_at END,
    migration.last_person_id = CASE WHEN $force THEN NULL ELSE migration.last_person_id END
RETURN migration.completed_at IS NOT NULL AS completed
"""


BACKFILL_MISSING_PERSON_COMPLETENESS_SCORES_BATCH = f"""
MATCH (migration:DataMigration {{migration_key: $migration_key}})
WHERE migration.completed_at IS NULL
CALL (migration) {{
  MATCH (p:Person)
  USING INDEX p:Person(person_id)
  WHERE p.status <> 'merged'
    AND p.person_id > coalesce(migration.last_person_id, '')
    AND ({_PERSON_COMPLETENESS_INVALID})
  WITH p
  ORDER BY p.person_id
  LIMIT $batch_size
  RETURN collect(p) AS batch
}}
FOREACH (p IN batch |
  SET p.profile_completeness_score = toFloat(
    CASE WHEN p.preferred_full_name IS NULL THEN 0 ELSE 1 END +
    CASE WHEN p.preferred_phone IS NULL THEN 0 ELSE 1 END +
    CASE WHEN p.preferred_email IS NULL THEN 0 ELSE 1 END +
    CASE WHEN p.preferred_dob IS NULL THEN 0 ELSE 1 END +
    CASE WHEN p.preferred_address_id IS NULL THEN 0 ELSE 1 END
  ) / 5.0
)
SET migration.last_person_id = CASE
      WHEN size(batch) = 0 THEN migration.last_person_id
      ELSE last(batch).person_id
    END,
    migration.updated_at = datetime()
RETURN size(batch) AS updated
"""

COMPLETE_PERSON_COMPLETENESS_MIGRATION = f"""
MATCH (migration:DataMigration {{migration_key: $migration_key}})
CALL (migration) {{
  MATCH (p:Person)
  WHERE p.status <> 'merged'
    AND ({_PERSON_COMPLETENESS_INVALID})
  RETURN count(p) AS missing_count
}}
SET migration.last_person_id = NULL
FOREACH (_ IN CASE WHEN missing_count = 0 THEN [1] ELSE [] END |
  SET migration.completed_at = coalesce(migration.completed_at, datetime()),
      migration.updated_at = datetime()
)
RETURN missing_count, migration.completed_at IS NOT NULL AS completed
"""
