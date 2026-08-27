"""Cypher for durable incremental-ingestion checkpoint state."""

from __future__ import annotations

LOAD_INCREMENTAL_CHECKPOINT = """
MATCH (checkpoint:IngestionCheckpoint {control_instance_id: $control_instance_id, checkpoint_key: $checkpoint_key})
RETURN checkpoint.value AS value
"""

UPSERT_INCREMENTAL_CHECKPOINT = """
MATCH (source:SourceSystem {source_key: $source_key})
OPTIONAL MATCH (new_run:IngestRun {
  control_instance_id: $control_instance_id
})
WHERE $ingest_run_id IS NOT NULL
  AND new_run.ingest_run_id = $ingest_run_id
MERGE (checkpoint:IngestionCheckpoint {control_instance_id: $control_instance_id, checkpoint_key: $checkpoint_key})
ON CREATE SET checkpoint.created_at = datetime()
WITH source, new_run, checkpoint
WHERE $ingest_run_id IS NULL OR new_run IS NOT NULL
OPTIONAL MATCH (checkpoint)-[prior_relation:PRODUCED_BY]->(prior_run:IngestRun {
  control_instance_id: $control_instance_id
})
WITH source, new_run, checkpoint,
     max(prior_run.started_at) AS prior_started_at,
     collect(prior_relation) AS prior_relations
WHERE prior_started_at IS NULL
   OR $ingest_run_id IS NULL
   OR new_run.started_at >= prior_started_at
FOREACH (relation IN prior_relations | DELETE relation)
SET checkpoint.source_key = $source_key,
    checkpoint.control_instance_id = $control_instance_id,
    checkpoint.value = $value,
    checkpoint.status = $status,
    checkpoint.producing_run_id = $ingest_run_id,
    checkpoint.updated_at = datetime()
MERGE (checkpoint)-[:CHECKPOINT_FOR]->(source)
FOREACH (_ IN CASE WHEN new_run IS NULL THEN [] ELSE [1] END |
  MERGE (checkpoint)-[:PRODUCED_BY]->(new_run)
)
"""

DELETE_INCREMENTAL_CHECKPOINT = """
MATCH (checkpoint:IngestionCheckpoint {control_instance_id: $control_instance_id, checkpoint_key: $checkpoint_key})
OPTIONAL MATCH (checkpoint)-[:PRODUCED_BY]->(prior_run:IngestRun {
  control_instance_id: $control_instance_id
})
OPTIONAL MATCH (deleting_run:IngestRun {control_instance_id: $control_instance_id})
WHERE $ingest_run_id IS NOT NULL
  AND deleting_run.ingest_run_id = $ingest_run_id
WITH checkpoint, max(prior_run.started_at) AS prior_started_at, deleting_run
WHERE $ingest_run_id IS NULL OR deleting_run IS NOT NULL
WITH checkpoint, prior_started_at, deleting_run
WHERE prior_started_at IS NULL
   OR $ingest_run_id IS NULL
   OR deleting_run.started_at >= prior_started_at
DETACH DELETE checkpoint
"""
