"""Cypher constants for the public ingest endpoints (source-system check, runs, records)."""

from __future__ import annotations

CHECK_SOURCE_SYSTEM = """
MATCH (ss:SourceSystem {source_key: $source_key, is_active: true})
OPTIONAL MATCH (migration:DataMigration {migration_key: 'bitrix_control_instance_v1'})
WITH ss, collect(DISTINCT migration) AS migrations
OPTIONAL MATCH (instance:BitrixSourceInstance {
  source_key: 'bitrix_chat', source_instance_id: 'legacy-default', status: 'active'
})
WITH ss, migrations, collect(DISTINCT instance) AS instances
WITH ss, migrations, instances,
     [instance IN instances |
       size([(instance)-[:INSTANCE_OF]->(:SourceSystem) | 1])
     ] AS relationship_counts,
     [instance IN instances |
       size([(instance)-[:INSTANCE_OF]->(:SourceSystem {
         source_key: 'bitrix_chat', is_active: true
       }) | 1])
     ] AS canonical_relationship_counts
OPTIONAL MATCH (dispatch:BitrixDispatchControl {
  source_key: 'bitrix_chat', control_instance_id: 'legacy-default'
})
WITH ss, migrations, instances, relationship_counts, canonical_relationship_counts,
     collect(DISTINCT dispatch) AS dispatches
WHERE $source_key <> 'bitrix_chat'
   OR (
     size(migrations) = 1
     AND migrations[0].completed_at IS NOT NULL
     AND size(instances) = 1
     AND relationship_counts = [1]
     AND canonical_relationship_counts = [1]
     AND size(dispatches) <= 1
     AND coalesce(dispatches[0].blocked, false) = false
   )
RETURN ss.source_system_id AS id
"""


CHECK_BITRIX_API_ADMISSION = """
OPTIONAL MATCH (migration:DataMigration {migration_key: 'bitrix_control_instance_v1'})
WITH collect(DISTINCT migration) AS migrations
OPTIONAL MATCH (instance:BitrixSourceInstance {
  source_key: 'bitrix_chat', source_instance_id: 'legacy-default', status: 'active'
})
WITH migrations, collect(DISTINCT instance) AS instances
WITH migrations, instances,
     [instance IN instances |
       size([(instance)-[:INSTANCE_OF]->(:SourceSystem) | 1])
     ] AS relationship_counts,
     [instance IN instances |
       size([(instance)-[:INSTANCE_OF]->(:SourceSystem {
         source_key: 'bitrix_chat', is_active: true
       }) | 1])
     ] AS canonical_relationship_counts
OPTIONAL MATCH (dispatch:BitrixDispatchControl {
  source_key: 'bitrix_chat', control_instance_id: 'legacy-default'
})
WITH migrations, instances, relationship_counts, canonical_relationship_counts,
     collect(DISTINCT dispatch) AS dispatches
WHERE size(migrations) = 1
  AND migrations[0].completed_at IS NOT NULL
  AND size(instances) = 1
  AND relationship_counts = [1]
  AND canonical_relationship_counts = [1]
  AND size(dispatches) <= 1
  AND coalesce(dispatches[0].blocked, false) = false
RETURN instances[0].source_instance_id AS control_instance_id
"""


CREATE_INGEST_RUN_INLINE = """
MATCH (ss:SourceSystem {source_key: $source_key})
CREATE (ir:IngestRun {
  ingest_run_id: randomUUID(),
  control_instance_id: 'legacy-default',
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
MATCH (ir:IngestRun {
  ingest_run_id: $ingest_run_id, control_instance_id: 'legacy-default'
})
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
MATCH (ir:IngestRun {
  ingest_run_id: $ingest_run_id, control_instance_id: 'legacy-default'
})
SET ir.record_count = ir.record_count + $accepted,
    ir.rejected_count = ir.rejected_count + $rejected
"""

CREATE_INGEST_RUN = """
MATCH (ss:SourceSystem {source_key: $source_key, is_active: true})
OPTIONAL MATCH (migration:DataMigration {migration_key: 'bitrix_control_instance_v1'})
WITH ss, collect(DISTINCT migration) AS migrations
OPTIONAL MATCH (instance:BitrixSourceInstance {
  source_key: 'bitrix_chat', source_instance_id: 'legacy-default', status: 'active'
})
WITH ss, migrations, collect(DISTINCT instance) AS instances
WITH ss, migrations, instances,
     [instance IN instances |
       size([(instance)-[:INSTANCE_OF]->(:SourceSystem) | 1])
     ] AS relationship_counts,
     [instance IN instances |
       size([(instance)-[:INSTANCE_OF]->(:SourceSystem {
         source_key: 'bitrix_chat', is_active: true
       }) | 1])
     ] AS canonical_relationship_counts
OPTIONAL MATCH (dispatch:BitrixDispatchControl {
  source_key: 'bitrix_chat', control_instance_id: 'legacy-default'
})
WITH ss, migrations, instances, relationship_counts, canonical_relationship_counts,
     collect(DISTINCT dispatch) AS dispatches
WHERE $source_key <> 'bitrix_chat'
   OR (
     size(migrations) = 1
     AND migrations[0].completed_at IS NOT NULL
     AND size(instances) = 1
     AND relationship_counts = [1]
     AND canonical_relationship_counts = [1]
     AND size(dispatches) <= 1
     AND coalesce(dispatches[0].blocked, false) = false
   )
MERGE (ir:IngestRun {
  source_key: $source_key,
  control_instance_id: 'legacy-default',
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
MATCH (ir:IngestRun {
  ingest_run_id: $ingest_run_id, control_instance_id: 'legacy-default'
})-[:FROM_SOURCE]->(ss:SourceSystem {source_key: $source_key})
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
MATCH (ir:IngestRun {
  ingest_run_id: $ingest_run_id, control_instance_id: 'legacy-default'
})
OPTIONAL MATCH (ir)-[:FROM_SOURCE]->(ss:SourceSystem)
RETURN ir {
  .ingest_run_id, .run_type, .mode, .dump_path, .status,
  .record_count, .rejected_count, .metadata,
  .failure_category, .failure_exception_class, .failure_message,
  .failure_task_id, .failure_checkpoint
} AS run,
toString(ir.started_at) AS started_at,
toString(ir.finished_at) AS finished_at,
ss.source_key AS source_key
"""
