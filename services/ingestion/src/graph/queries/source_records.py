"""Cypher constants for SourceRecord and IngestRun lifecycle (idempotency, create, link)."""

from __future__ import annotations

LOCK_AND_GET_SOURCE_STATE = """
MERGE (lock:SourceRecordIdentityLock {
    source_system: $source_system,
    source_record_id: $source_record_id
})
SET lock.locked_at = datetime()
WITH lock
MATCH (ss:SourceSystem {source_key: $source_system})
OPTIONAL MATCH (history:SourceRecord {source_record_id: $source_record_id})-[:FROM_SOURCE]->(ss)
WITH lock, ss,
     max(toInteger(history.source_record_version)) AS max_source_record_version
OPTIONAL MATCH (sr:SourceRecord {source_record_id: $source_record_id})-[:FROM_SOURCE]->(ss)
// Rollout compatibility: remove the NULL/is_latest branch only after the
// lifecycle backfill is guaranteed complete in every deployed graph.
WHERE sr.lifecycle_status IN ['active', 'pending_review']
   OR (sr.lifecycle_status IS NULL AND sr.is_latest = true)
OPTIONAL MATCH (sr)-[:LINKED_TO]->(person:Person)
RETURN sr.source_record_pk AS source_record_pk,
       toInteger(sr.source_record_version) AS source_record_version,
       sr.record_hash AS record_hash,
       CASE
           WHEN sr IS NULL THEN null
           WHEN sr.lifecycle_status IS NULL THEN 'active'
           ELSE sr.lifecycle_status
       END AS lifecycle_status,
       collect(DISTINCT person.person_id) AS linked_person_ids,
       max_source_record_version
ORDER BY toInteger(sr.source_record_version) DESC
"""

ACTIVATE_SOURCE_RECORD_VERSION = """
MATCH (old:SourceRecord {source_record_pk: $old_source_record_pk})
      -[:FROM_SOURCE]->(source:SourceSystem)
MATCH (new:SourceRecord {
    source_record_pk: $new_source_record_pk,
    lifecycle_status: 'pending_review'
})-[:FROM_SOURCE]->(source)
WHERE old.source_record_id = new.source_record_id
  AND new.expected_active_source_record_pk = old.source_record_pk
  AND (
      old.lifecycle_status = 'active'
      OR (old.lifecycle_status IS NULL AND old.is_latest = true)
  )
SET old.lifecycle_status = 'superseded',
    old.is_latest = false,
    old.superseded_at = datetime(),
    old.updated_at = datetime(),
    new.lifecycle_status = 'active',
    new.is_latest = true,
    new.activated_at = datetime(),
    new.updated_at = datetime()
MERGE (old)-[:PREVIOUS_VERSION_OF]->(new)
RETURN new.source_record_pk AS source_record_pk
"""

ACTIVATE_FIRST_SOURCE_RECORD_VERSION = """
MATCH (pending:SourceRecord {
    source_record_pk: $source_record_pk,
    lifecycle_status: 'pending_review'
})-[:FROM_SOURCE]->(ss:SourceSystem {source_key: $source_system})
WHERE pending.source_record_id = $source_record_id
  AND pending.expected_active_source_record_pk IS NULL
  AND NOT EXISTS {
      MATCH (active:SourceRecord {source_record_id: $source_record_id})-[:FROM_SOURCE]->(ss)
      WHERE active.lifecycle_status = 'active'
         OR (active.lifecycle_status IS NULL AND active.is_latest = true)
  }
SET pending.lifecycle_status = 'active',
    pending.is_latest = true,
    pending.activated_at = datetime(),
    pending.updated_at = datetime()
RETURN pending.source_record_pk AS source_record_pk
"""

REJECT_PENDING_SOURCE_RECORD = """
MATCH (pending:SourceRecord {
    source_record_pk: $source_record_pk,
    lifecycle_status: 'pending_review'
})
SET pending.lifecycle_status = 'rejected',
    pending.rejection_reason = $reason,
    pending.rejected_at = datetime(),
    pending.updated_at = datetime()
WITH pending
OPTIONAL MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
               -[:ABOUT_LEFT|ABOUT_RIGHT]->(pending)
FOREACH (stale_case IN CASE
    WHEN rc.queue_state IN ['open', 'assigned', 'deferred'] THEN [rc]
    ELSE []
END |
    SET stale_case.queue_state = 'cancelled',
        stale_case.resolution = 'cancelled_superseded',
        stale_case.resolution_reason = $reason,
        stale_case.resolved_at = datetime(),
        stale_case.updated_at = datetime()
)
RETURN pending.source_record_pk AS source_record_pk
"""

MARK_SOURCE_RECORD_LINK_FAILED = """
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
WHERE sr.lifecycle_status IN ['pending_review', 'active']
SET sr.lifecycle_status = 'link_failed',
    sr.link_failure_reason = $reason,
    sr.link_failed_at = datetime(),
    sr.updated_at = datetime()
RETURN sr.source_record_pk AS source_record_pk
"""

CHECK_SOURCE_RECORD_EXISTS = """
MATCH (sr:SourceRecord {
    source_record_id: $source_record_id
})
WHERE sr.record_hash = $record_hash
  AND sr.is_latest = true
MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem {source_key: $source_system})
RETURN sr.source_record_pk AS source_record_pk
LIMIT 1
"""

GET_LATEST_SOURCE_RECORD = """
MATCH (sr:SourceRecord {source_record_id: $source_record_id})-[:FROM_SOURCE]->(:SourceSystem {source_key: $source_system})
WHERE sr.is_latest = true
RETURN sr.source_record_pk AS source_record_pk,
       sr.record_hash AS record_hash,
       coalesce(sr.source_record_version, '1') AS source_record_version
ORDER BY toInteger(coalesce(sr.source_record_version, '1')) DESC
LIMIT 1
"""

SUPERSEDE_SOURCE_RECORD = """
MATCH (old:SourceRecord {source_record_pk: $old_source_record_pk})
MATCH (new:SourceRecord {source_record_pk: $new_source_record_pk})
SET old.is_latest = false,
    old.superseded_at = datetime(),
    old.updated_at = datetime(),
    new.is_latest = true,
    new.updated_at = datetime()
MERGE (old)-[:SUPERSEDED_BY]->(new)
WITH old
OPTIONAL MATCH (:Person)-[ident:IDENTIFIED_BY {source_record_pk: old.source_record_pk}]->(:Identifier)
SET ident.is_active = false,
    ident.updated_at = datetime()
WITH old
OPTIONAL MATCH (:Person)-[addr:LIVES_AT {source_record_pk: old.source_record_pk}]->(:Address)
SET addr.is_active = false,
    addr.updated_at = datetime()
WITH old
OPTIONAL MATCH (:Person)-[fact:HAS_FACT {source_record_pk: old.source_record_pk}]->(old)
SET fact.is_active = false,
    fact.updated_at = datetime()
"""

CREATE_SOURCE_RECORD = """
MATCH (ss:SourceSystem {source_key: $source_system})
OPTIONAL MATCH (entity:Entity {entity_key: $entity_key})
WITH ss, entity, $entity_key AS requested_entity_key
WHERE requested_entity_key IS NULL OR entity IS NOT NULL
CREATE (sr:SourceRecord {
    source_record_pk:      randomUUID(),
    source_record_id:      $source_record_id,
    source_record_version: $source_record_version,
    source_version_key:   $source_version_key,
    entity_key:           $entity_key,
    expected_active_source_record_pk: $expected_active_source_record_pk,
    lifecycle_status:     $lifecycle_status,
    record_type:           $record_type,
    extraction_confidence: $extraction_confidence,
    extraction_method:     $extraction_method,
    conversation_ref:      $conversation_ref,
    parent_source_system:  $parent_source_system,
    parent_source_record_id: $parent_source_record_id,
    parent_record_type:    $parent_record_type,
    link_status:           $link_status,
    observed_at:           datetime($observed_at),
    ingested_at:           datetime(),
    record_hash:           $record_hash,
    raw_payload:           $raw_payload,
    normalized_payload:    $normalized_payload,
    // Compatibility only: lifecycle_status is authoritative. Staged identity
    // records pass false so legacy readers cannot mistake them for active.
    is_latest:             $is_latest,
    retention_expires_at:  null
})-[:FROM_SOURCE]->(ss)
FOREACH (_ IN CASE WHEN entity IS NULL THEN [] ELSE [1] END |
    MERGE (sr)-[:OWNED_BY]->(entity)
)
RETURN sr.source_record_pk AS source_record_pk
"""

RETIRE_IDENTITY_PROJECTIONS = """
MATCH (source:SourceRecord {source_record_pk: $source_record_pk})
CALL {
    WITH source
    OPTIONAL MATCH (person:Person)-[rel:IDENTIFIED_BY]->(:Identifier)
    WHERE rel.source_record_pk = source.source_record_pk
    SET rel.is_active = false, rel.updated_at = datetime()
    RETURN collect(DISTINCT person.person_id) AS identified_owners
}
CALL {
    WITH source
    OPTIONAL MATCH (person:Person)-[rel:LIVES_AT]->(:Address)
    WHERE rel.source_record_pk = source.source_record_pk
    SET rel.is_active = false, rel.updated_at = datetime()
    RETURN collect(DISTINCT person.person_id) AS address_owners
}
CALL {
    WITH source
    OPTIONAL MATCH (person:Person)-[rel:HAS_FACT]->(source)
    WHERE rel.source_record_pk = source.source_record_pk
    SET rel.is_active = false, rel.updated_at = datetime()
    RETURN collect(DISTINCT person.person_id) AS fact_owners
}
WITH identified_owners + address_owners + fact_owners AS all_owners
UNWIND all_owners AS person_id
RETURN DISTINCT person_id
"""

RETIRE_ADDRESS_PROJECTION = """
MATCH (source:SourceRecord {source_record_pk: $source_record_pk})
OPTIONAL MATCH (source)-[rel:DESCRIBES_ADDRESS]->(:Address)
SET rel.is_active = false, rel.updated_at = datetime()
RETURN count(rel) AS retired_count
"""

LINK_SOURCE_RECORD_TO_PERSON = """
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
MATCH (p:Person {person_id: $person_id})
CREATE (sr)-[:LINKED_TO {linked_at: datetime()}]->(p)
"""

CREATE_INGEST_RUN = """
MATCH (ss:SourceSystem {source_key: $source_key})
OPTIONAL MATCH (dispatch:BitrixDispatchControl {source_key: $source_key})
WITH ss, dispatch
WHERE coalesce(dispatch.blocked, false) = false
CREATE (ir:IngestRun {
    ingest_run_id: randomUUID(),
    run_type: $run_type,
    mode: $mode,
    status: 'started',
    started_at: datetime(),
    finished_at: null,
    record_count: 0,
    rejected_count: 0,
    metadata: '{}'
})-[:FROM_SOURCE]->(ss)
RETURN ir.ingest_run_id AS ingest_run_id
"""

CREATE_OR_REUSE_WORKER_INGEST_RUN = """
MATCH (ss:SourceSystem {source_key: $source_key})
OPTIONAL MATCH (dispatch:BitrixDispatchControl {source_key: $source_key})
WITH ss, dispatch
WHERE coalesce(dispatch.blocked, false) = false
MERGE (ir:IngestRun {worker_task_id: $worker_task_id})
ON CREATE SET
    ir.ingest_run_id = randomUUID(),
    ir.source_key = $source_key,
    ir.run_type = $run_type,
    ir.mode = $mode,
    ir.status = 'started',
    ir.started_at = datetime(),
    ir.finished_at = null,
    ir.record_count = 0,
    ir.rejected_count = 0,
    ir.metadata = '{}',
    ir.creation_token = $creation_token
WITH ss, ir, coalesce(ir.creation_token = $creation_token, false) AS created,
     ir.source_key = $source_key AND ir.mode = $mode AS is_compatible
WHERE is_compatible
MERGE (ir)-[:FROM_SOURCE]->(ss)
REMOVE ir.creation_token
RETURN ir.ingest_run_id AS ingest_run_id,
       ir.status AS status,
       created AS created
"""

UPDATE_INGEST_RUN = """
MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id})
SET ir.status = $status,
    ir.finished_at = datetime(),
    ir.record_count = $record_count,
    ir.rejected_count = $rejected_count
"""

MARK_INGEST_RUN_FAILED = """
MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id})
SET ir.status = 'failed',
    ir.finished_at = datetime(),
    ir.record_count = $record_count,
    ir.rejected_count = $rejected_count,
    ir.failure_category = $failure_category,
    ir.failure_exception_class = $failure_exception_class,
    ir.failure_message = $failure_message,
    ir.failure_source = $failure_source,
    ir.failure_mode = $failure_mode,
    ir.failure_task_id = $failure_task_id,
    ir.failure_checkpoint = $failure_checkpoint
"""

GET_INGEST_RUN_STATUS = """
MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id})
RETURN ir.status AS status
"""

LINK_SOURCE_RECORD_TO_RUN = """
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id})
CREATE (sr)-[:PART_OF_RUN]->(ir)
"""
