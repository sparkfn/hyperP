"""Cypher constants for the public ingest endpoints (source-system check, runs, records)."""

from __future__ import annotations

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
CREATE (sr:SourceRecord {
  source_record_pk: randomUUID(),
  source_record_id: $source_record_id,
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
"""

UPDATE_INGEST_RUN_COUNTERS = """
MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id})
SET ir.record_count = ir.record_count + $accepted,
    ir.rejected_count = ir.rejected_count + $rejected
"""

CREATE_INGEST_RUN = """
MATCH (ss:SourceSystem {source_key: $source_key, is_active: true})
CREATE (ir:IngestRun {
  ingest_run_id: randomUUID(),
  run_type: $run_type,
  status: 'started',
  started_at: datetime(),
  finished_at: null,
  record_count: 0,
  rejected_count: 0,
  metadata: $metadata
})
CREATE (ir)-[:FROM_SOURCE]->(ss)
RETURN ir.ingest_run_id AS ingest_run_id,
       ir.status AS status,
       toString(ir.started_at) AS started_at
"""

UPDATE_INGEST_RUN = """
MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id})-[:FROM_SOURCE]->(ss:SourceSystem {source_key: $source_key})
SET ir.status = $status,
    ir.finished_at = CASE WHEN $finished_at IS NOT NULL THEN datetime($finished_at) ELSE ir.finished_at END,
    ir.metadata = CASE WHEN $metadata IS NOT NULL THEN $metadata ELSE ir.metadata END
RETURN ir.ingest_run_id AS ingest_run_id,
       ir.status AS status,
       toString(ir.finished_at) AS finished_at
"""

GET_INGEST_RUN = """
MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id})
OPTIONAL MATCH (ir)-[:FROM_SOURCE]->(ss:SourceSystem)
RETURN ir {
  .ingest_run_id, .run_type, .status,
  .record_count, .rejected_count, .metadata
} AS run,
toString(ir.started_at) AS started_at,
toString(ir.finished_at) AS finished_at,
ss.source_key AS source_key
"""
