"""Cypher constants for SourceRecord and IngestRun lifecycle (idempotency, create, link)."""

from __future__ import annotations

CHECK_SOURCE_RECORD_EXISTS = """
MATCH (sr:SourceRecord {
    source_record_id: $source_record_id
})
WHERE sr.record_hash = $record_hash
  AND coalesce(sr.is_latest, true) = true
MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem {source_key: $source_system})
RETURN sr.source_record_pk AS source_record_pk
LIMIT 1
"""

GET_LATEST_SOURCE_RECORD = """
MATCH (sr:SourceRecord {source_record_id: $source_record_id})-[:FROM_SOURCE]->(:SourceSystem {source_key: $source_system})
WHERE coalesce(sr.is_latest, true) = true
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
CREATE (sr:SourceRecord {
    source_record_pk:      randomUUID(),
    source_record_id:      $source_record_id,
    source_record_version: $source_record_version,
    record_type:           $record_type,
    extraction_confidence: $extraction_confidence,
    extraction_method:     $extraction_method,
    conversation_ref:      $conversation_ref,
    link_status:           $link_status,
    observed_at:           datetime($observed_at),
    ingested_at:           datetime(),
    record_hash:           $record_hash,
    raw_payload:           $raw_payload,
    normalized_payload:    $normalized_payload,
    is_latest:             true,
    retention_expires_at:  null
})-[:FROM_SOURCE]->(ss)
RETURN sr.source_record_pk AS source_record_pk
"""

LINK_SOURCE_RECORD_TO_PERSON = """
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
MATCH (p:Person {person_id: $person_id})
CREATE (sr)-[:LINKED_TO {linked_at: datetime()}]->(p)
"""

CREATE_INGEST_RUN = """
MATCH (ss:SourceSystem {source_key: $source_key})
CREATE (ir:IngestRun {
    ingest_run_id: randomUUID(),
    run_type: $run_type,
    status: 'started',
    started_at: datetime(),
    finished_at: null,
    record_count: 0,
    rejected_count: 0,
    metadata: '{}'
})-[:FROM_SOURCE]->(ss)
RETURN ir.ingest_run_id AS ingest_run_id
"""

UPDATE_INGEST_RUN = """
MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id})
SET ir.status = $status,
    ir.finished_at = datetime(),
    ir.record_count = $record_count,
    ir.rejected_count = $rejected_count
"""

LINK_SOURCE_RECORD_TO_RUN = """
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id})
CREATE (sr)-[:PART_OF_RUN]->(ir)
"""
