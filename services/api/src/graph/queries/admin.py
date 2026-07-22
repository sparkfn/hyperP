"""Cypher constants for admin endpoints (source-system listing, field-trust config)."""

from __future__ import annotations

LIST_SOURCE_SYSTEMS = """
MATCH (ss:SourceSystem)
OPTIONAL MATCH (ss)-[:OPERATED_BY]->(e:Entity)
OPTIONAL MATCH (run:IngestRun)-[:FROM_SOURCE]->(ss)
WITH ss, e, run
ORDER BY run.started_at DESC
WITH ss, e, head(collect(run)) AS latest_run
OPTIONAL MATCH (failure:IngestRun)-[:FROM_SOURCE]->(ss)
WHERE failure.status = 'failed' OR failure.failure_category IS NOT NULL
WITH ss, e, latest_run, failure
ORDER BY failure.started_at DESC
WITH ss, e, latest_run, head(collect(failure)) AS latest_failure
RETURN ss {
  .source_system_id, .source_key, .display_name,
  .system_type, .is_active, .field_trust,
  .created_at, .updated_at
} AS source_system,
e.entity_key AS entity_key,
latest_run {
  .ingest_run_id, .status, .started_at, .finished_at
} AS latest_run,
latest_failure {
  .ingest_run_id, .started_at,
  .failure_category, .failure_exception_class, .failure_message,
  .failure_mode, .failure_task_id, .failure_checkpoint
} AS latest_failure
ORDER BY ss.source_key
"""

GET_FIELD_TRUST = """
MATCH (ss:SourceSystem {source_key: $source_key})
RETURN ss.field_trust AS field_trust,
       ss.source_key AS source_key,
       ss.display_name AS display_name
"""

UPDATE_FIELD_TRUST = """
MATCH (ss:SourceSystem {source_key: $source_key})
SET ss.field_trust = $field_trust,
    ss.updated_at = datetime()
"""
