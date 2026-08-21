"""Atomic Person profile-analysis invalidation queries."""

from __future__ import annotations

MARK_PROFILE_ANALYSIS_DIRTY = """
WITH $source_record_pks AS source_record_pks,
     $person_ids AS direct_person_ids
CALL (source_record_pks, direct_person_ids) {
  WITH direct_person_ids
  UNWIND direct_person_ids AS person_id
  RETURN person_id
  UNION
  WITH source_record_pks
  UNWIND source_record_pks AS source_record_pk
  MATCH (source:SourceRecord {source_record_pk: source_record_pk})
  MATCH (source)-[:LINKED_TO]->(direct:Person)
  RETURN direct.person_id AS person_id
  UNION
  WITH source_record_pks
  UNWIND source_record_pks AS source_record_pk
  MATCH (person:Person)-[projection:IDENTIFIED_BY]->()
  WHERE projection.source_record_pk = source_record_pk
  RETURN person.person_id AS person_id
  UNION
  WITH source_record_pks
  UNWIND source_record_pks AS source_record_pk
  MATCH (person:Person)-[projection:LIVES_AT]->()
  WHERE projection.source_record_pk = source_record_pk
  RETURN person.person_id AS person_id
  UNION
  WITH source_record_pks
  UNWIND source_record_pks AS source_record_pk
  MATCH (person:Person)-[projection:HAS_FACT]->()
  WHERE projection.source_record_pk = source_record_pk
  RETURN person.person_id AS person_id
  UNION
  WITH source_record_pks
  UNWIND source_record_pks AS source_record_pk
  MATCH (person:Person)-[context:PURCHASED]->()
  WHERE context.source_record_pk = source_record_pk
  RETURN person.person_id AS person_id
  UNION
  WITH source_record_pks
  UNWIND source_record_pks AS source_record_pk
  MATCH (person:Person)-[context:BOUGHT_VEHICLE]->()
  WHERE context.source_record_pk = source_record_pk
  RETURN person.person_id AS person_id
  UNION
  WITH source_record_pks
  UNWIND source_record_pks AS source_record_pk
  MATCH (person:Person)-[context:OWNS_VEHICLE]->()
  WHERE context.source_record_pk = source_record_pk
  RETURN person.person_id AS person_id
  UNION
  WITH source_record_pks
  UNWIND source_record_pks AS source_record_pk
  MATCH (source:SourceRecord {source_record_pk: source_record_pk})
        -[:FOR_CUSTOMER_RECORD]->(:SourceRecord)-[:LINKED_TO]->(customer:Person)
  RETURN customer.person_id AS person_id
  UNION
  WITH source_record_pks
  UNWIND source_record_pks AS source_record_pk
  MATCH (left:Person)-[knows:KNOWS]->(right:Person)
  WHERE knows.source_record_pk = source_record_pk
  UNWIND [left.person_id, right.person_id] AS person_id
  RETURN person_id
}
WITH DISTINCT person_id
MATCH (person:Person {person_id: person_id, status: 'active'})
SET person.analysis_input_revision = coalesce(person.analysis_input_revision, 0) + 1,
    person.analysis_dirty_at = datetime()
RETURN person.person_id AS person_id
ORDER BY person_id
"""


FIND_PROFILE_ANALYSIS_MERGE_AFFECTED_PERSON_IDS = """
MATCH (absorbed:Person {person_id: $absorbed_id})
OPTIONAL MATCH (absorbed)-[knows:KNOWS]-(neighbor:Person {status: 'active'})
WHERE coalesce(knows.is_active, true) = true
RETURN [person_id IN collect(DISTINCT neighbor.person_id)
        WHERE person_id IS NOT NULL] + [$survivor_id] AS person_ids
"""


RETIRE_SOURCE_EVIDENCE = """
MERGE (lock:SourceRecordIdentityLock {
    source_system: $source_system,
    source_record_id: $source_record_id
})
SET lock.locked_at = datetime()
WITH lock
MATCH (sr:SourceRecord {source_record_id: $source_record_id})
      -[:FROM_SOURCE]->(:SourceSystem {source_key: $source_system})
WHERE sr.lifecycle_status IN ['active', 'pending_review']
   OR (sr.lifecycle_status IS NULL AND sr.is_latest = true)
WITH collect(sr) AS records
WHERE NOT any(record IN records WHERE
    (record.ingested_at IS NOT NULL
        AND record.ingested_at > datetime($reconciliation_snapshot_at))
    OR (record.activated_at IS NOT NULL
        AND record.activated_at > datetime($reconciliation_snapshot_at))
)
CALL (records) {
  WITH records
  WITH [source IN records WHERE source.lifecycle_status = 'active'
        OR (source.lifecycle_status IS NULL AND source.is_latest = true)]
       AS accepted_records
  UNWIND accepted_records AS source
  OPTIONAL MATCH (source)-[:LINKED_TO]->(direct:Person)
  OPTIONAL MATCH (person:Person)-[projection]->()
  WHERE projection.source_record_pk = source.source_record_pk
  OPTIONAL MATCH (knows_from:Person)-[knows:KNOWS]->(knows_to:Person)
  WHERE knows.source_record_pk = source.source_record_pk
  OPTIONAL MATCH (source)-[:FOR_CUSTOMER_RECORD]->(:SourceRecord)
        -[:LINKED_TO]->(customer:Person)
  RETURN collect(DISTINCT direct.person_id)
       + collect(DISTINCT person.person_id)
       + collect(DISTINCT knows_from.person_id)
       + collect(DISTINCT knows_to.person_id)
       + collect(DISTINCT customer.person_id) AS affected_person_ids
}
FOREACH (sr IN records |
  SET sr.lifecycle_status = 'superseded', sr.is_latest = false,
      sr.retired_at = datetime($retired_at), sr.updated_at = datetime()
)
WITH records, affected_person_ids,
     [sr IN records | sr.source_record_pk] AS source_record_pks
OPTIONAL MATCH ()-[rel]->()
WHERE rel.source_record_pk IN source_record_pks
SET rel.is_active = false, rel.updated_at = datetime()
WITH records, affected_person_ids, source_record_pks
CALL (affected_person_ids) {
  WITH affected_person_ids
  UNWIND affected_person_ids AS person_id
  WITH DISTINCT person_id
  MATCH (person:Person {person_id: person_id, status: 'active'})
  SET person.analysis_input_revision = coalesce(person.analysis_input_revision, 0) + 1,
      person.analysis_dirty_at = datetime()
  RETURN count(person) AS dirtied_count
}
RETURN size(source_record_pks) AS retired_count, dirtied_count, source_record_pks
"""
