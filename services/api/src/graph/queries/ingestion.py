"""Cypher constants for the public ingest endpoints (source-system check, runs, records)."""

from __future__ import annotations

CREATE_INGEST_RUN_IDEMPOTENCY_CONSTRAINT = """CREATE CONSTRAINT ingest_run_source_idempotency_unique IF NOT EXISTS
FOR (ir:IngestRun)
REQUIRE (ir.source_key, ir.idempotency_key) IS UNIQUE"""

CHECK_SOURCE_SYSTEM = """
MATCH (ss:SourceSystem {source_key: $source_key, is_active: true})
RETURN ss.source_system_id AS id
"""

CREATE_INGEST_RUN_INLINE = """
MATCH (ss:SourceSystem {source_key: $source_key})
CREATE (ir:IngestRun {
  ingest_run_id: randomUUID(),
  run_type: $ingest_type,
  status: 'started',
  started_at: datetime(),
  record_count: 0,
  rejected_count: 0,
  metadata: {}
})
CREATE (ir)-[:FROM_SOURCE]->(ss)
RETURN ir.ingest_run_id AS ingest_run_id
"""

CREATE_SOURCE_RECORD = """
MATCH (ss:SourceSystem {source_key: $source_key})
MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id})
OPTIONAL MATCH (entity:Entity {entity_key: $entity_key})
WITH ss, ir, entity
WHERE $entity_key IS NULL OR entity IS NOT NULL
CREATE (sr:SourceRecord {
  source_record_pk: randomUUID(),
  source_record_id: $source_record_id,
  entity_key: $entity_key,
  source_record_version: $source_record_version,
  record_type: $record_type,
  extraction_confidence: $extraction_confidence,
  extraction_method: $extraction_method,
  conversation_ref: $conversation_ref,
  link_status: 'pending_review',
  observed_at: datetime($observed_at),
  ingested_at: datetime(),
  record_hash: $record_hash,
  raw_payload: $raw_payload,
  normalized_payload: $attributes,
  metadata: {},
  retention_expires_at: null
})
CREATE (sr)-[:FROM_SOURCE]->(ss)
CREATE (sr)-[:PART_OF_RUN]->(ir)
FOREACH (_ IN CASE WHEN entity IS NULL THEN [] ELSE [1] END |
  CREATE (sr)-[:OWNED_BY]->(entity)
)
RETURN sr.source_record_pk AS source_record_pk
"""

UPDATE_INGEST_RUN_COUNTERS = """
MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id})
SET ir.record_count = ir.record_count + $accepted,
    ir.rejected_count = ir.rejected_count + $rejected
"""

CREATE_INGEST_RUN = """
MATCH (ss:SourceSystem {source_key: $source_key, is_active: true})
MERGE (ir:IngestRun {
  source_key: $source_key,
  idempotency_key: $idempotency_key
})
ON CREATE SET
  ir.ingest_run_id = randomUUID(),
  ir.run_type = $run_type,
  ir.mode = $mode,
  ir.dump_path = $dump_path,
  ir.status = 'started',
  ir.started_at = datetime(),
  ir.finished_at = null,
  ir.record_count = 0,
  ir.rejected_count = 0,
  ir.metadata = $metadata,
  ir.creation_token = $creation_token
WITH ss, ir, ir.creation_token = $creation_token AS created
MERGE (ir)-[:FROM_SOURCE]->(ss)
REMOVE ir.creation_token
RETURN ir.ingest_run_id AS ingest_run_id,
       ir.status AS status,
       ir.mode AS mode,
       ir.dump_path AS dump_path,
       toString(ir.started_at) AS started_at,
       created AS created
"""

UPDATE_INGEST_RUN = """
MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id})-[:FROM_SOURCE]->(ss:SourceSystem {source_key: $source_key})
SET ir.status = $status,
    ir.finished_at = CASE WHEN $finished_at IS NOT NULL THEN datetime($finished_at) ELSE ir.finished_at END,
    ir.metadata = CASE WHEN $metadata IS NOT NULL THEN $metadata ELSE ir.metadata END
RETURN ir.ingest_run_id AS ingest_run_id,
       ir.status AS status,
       ir.mode AS mode,
       ir.dump_path AS dump_path,
       toString(ir.finished_at) AS finished_at
"""

GET_INGEST_RUN = """
MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id})
OPTIONAL MATCH (ir)-[:FROM_SOURCE]->(ss:SourceSystem)
RETURN ir {
  .ingest_run_id, .run_type, .mode, .dump_path, .status,
  .record_count, .rejected_count, .metadata
} AS run,
toString(ir.started_at) AS started_at,
toString(ir.finished_at) AS finished_at,
ss.source_key AS source_key
"""
