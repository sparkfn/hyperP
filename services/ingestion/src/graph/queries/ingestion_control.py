"""Cypher for logical ingestion runs, immutable attempts, and checkpoints."""

from __future__ import annotations

CREATE_LOGICAL_RUN_CONSTRAINTS: tuple[str, ...] = (
    """CREATE CONSTRAINT ingestion_logical_run_id_unique IF NOT EXISTS
FOR (run:IngestionLogicalRun)
REQUIRE run.logical_run_id IS UNIQUE""",
    """CREATE CONSTRAINT ingestion_checkpoint_control_logical_phase_unique IF NOT EXISTS
FOR (checkpoint:IngestionCheckpoint)
REQUIRE (checkpoint.control_instance_id, checkpoint.logical_run_id, checkpoint.phase) IS UNIQUE""",
    """CREATE CONSTRAINT ingestion_logical_run_source_control_idempotency_unique IF NOT EXISTS
FOR (run:IngestionLogicalRun)
REQUIRE (run.source_key, run.control_instance_id, run.idempotency_key) IS UNIQUE""",
)

CREATE_BITRIX_INGESTION_STREAM_CONSTRAINTS: tuple[str, ...] = (
    """CREATE CONSTRAINT bitrix_ingestion_stream_control_identity_unique IF NOT EXISTS
FOR (stream:BitrixIngestionStream)
REQUIRE (stream.source_key, stream.control_instance_id, stream.stream_key) IS UNIQUE""",
)

LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE = """
MATCH (stream:BitrixIngestionStream {
  source_key: $source_key,
  control_instance_id: $control_instance_id,
  stream_key: $stream_key
})
SET stream.fence_lock_version = coalesce(stream.fence_lock_version, 0) + 1
WITH stream
WHERE stream.logical_run_id = $logical_run_id
  AND stream.ingest_run_id = $ingest_run_id
  AND stream.attempt_generation = $attempt_generation
  AND stream.stream_generation = $stream_generation
  AND stream.fencing_token = $fencing_token
  AND stream.status = 'active'
RETURN stream.fence_lock_version AS fence_lock_version
"""

PROBE_REJECTED_BITRIX_FENCE_ROLLBACK = """
MATCH (stream:BitrixIngestionStream {
  source_key: $source_key,
  control_instance_id: $control_instance_id,
  stream_key: $stream_key
})
SET stream.fence_lock_version = coalesce(stream.fence_lock_version, 0) + 1,
    stream.rollback_probe_token = $probe_token
RETURN stream.logical_run_id = $logical_run_id
  AND stream.ingest_run_id = $ingest_run_id
  AND stream.attempt_generation = $attempt_generation
  AND stream.stream_generation = $stream_generation
  AND stream.fencing_token = $fencing_token
  AND stream.status = 'active' AS fence_accepted,
  stream.rollback_probe_token AS rollback_probe_token
"""

FIND_BITRIX_FENCE_ROLLBACK_PROBE = """
MATCH (stream:BitrixIngestionStream {
  source_key: $source_key,
  control_instance_id: $control_instance_id,
  stream_key: $stream_key,
  rollback_probe_token: $probe_token
})
RETURN count(stream) AS persisted_probe_count
"""

SET_FENCED_BITRIX_STREAM_STATUS = """
MATCH (stream:BitrixIngestionStream {
  source_key: $source_key,
  control_instance_id: $control_instance_id,
  stream_key: $stream_key,
  logical_run_id: $logical_run_id,
  ingest_run_id: $ingest_run_id,
  attempt_generation: $attempt_generation,
  stream_generation: $stream_generation,
  fencing_token: $fencing_token
})
WHERE stream.status = 'active'
  AND $status IN ['draining', 'completed', 'terminated', 'superseded']
SET stream.status = $status,
    stream.updated_at = datetime(),
    stream.finished_at = CASE
      WHEN $status IN ['completed', 'terminated', 'superseded'] THEN datetime()
      ELSE stream.finished_at
    END
RETURN stream.status AS status
"""

# Admission establishes the identity consumed by same-transaction mutation fences.
ADMIT_OR_COALESCE_BITRIX_STREAM = """
MATCH (source:SourceSystem {source_key: $source_key, is_active: true})
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id, control_instance_id: $control_instance_id
})-[:FOR_SOURCE]->(source)
MATCH (logical)-[:ACTIVE_ATTEMPT]->(attempt:IngestRun {
  ingest_run_id: $ingest_run_id, control_instance_id: $control_instance_id
})
OPTIONAL MATCH (dispatch:BitrixDispatchControl {
  source_key: $source_key, control_instance_id: $control_instance_id
})
WHERE logical.active_generation = $attempt_generation
  AND attempt.generation = $attempt_generation
  AND logical.status IN ['queued', 'running', 'stop_requested']
  AND attempt.status IN ['queued', 'started']
  AND coalesce(dispatch.blocked, false) = false
MERGE (stream:BitrixIngestionStream {
  source_key: $source_key,
  control_instance_id: $control_instance_id,
  stream_key: $stream_key
})
ON CREATE SET
  stream.logical_run_id = $logical_run_id,
  stream.control_instance_id = $control_instance_id,
  stream.ingest_run_id = $ingest_run_id,
  stream.attempt_generation = $attempt_generation,
  stream.worker_task_id = $worker_task_id,
  stream.stream_generation = 1,
  stream.fencing_token = 1,
  stream.status = 'active',
  stream.created_at = datetime(),
  stream.updated_at = datetime(),
  stream.creation_token = $creation_token
MERGE (stream)-[:FOR_SOURCE]->(source)
WITH stream, logical, attempt, stream.creation_token = $creation_token AS created
REMOVE stream.creation_token
WITH stream, logical, attempt,
     created,
     stream.logical_run_id = $logical_run_id
  AND stream.control_instance_id = $control_instance_id
       AND stream.ingest_run_id = $ingest_run_id
       AND stream.attempt_generation = $attempt_generation AS same_attempt,
     stream.status IN ['completed', 'terminated', 'superseded'] AS terminal_stream,
     stream.stream_generation AS current_stream_generation,
     stream.fencing_token AS current_fencing_token
WHERE created OR same_attempt OR terminal_stream OR $replace_active
WITH stream, logical, attempt, created, same_attempt,
     terminal_stream OR $replace_active AS replace_existing,
     current_stream_generation, current_fencing_token
SET stream.logical_run_id = CASE
      WHEN created OR same_attempt THEN stream.logical_run_id
      WHEN replace_existing THEN $logical_run_id
      ELSE stream.logical_run_id
    END,
    stream.ingest_run_id = CASE
      WHEN created OR same_attempt THEN stream.ingest_run_id
      WHEN replace_existing THEN $ingest_run_id
      ELSE stream.ingest_run_id
    END,
    stream.attempt_generation = CASE
      WHEN created OR same_attempt THEN stream.attempt_generation
      WHEN replace_existing THEN $attempt_generation
      ELSE stream.attempt_generation
    END,
    stream.worker_task_id = CASE
      WHEN created OR same_attempt THEN stream.worker_task_id
      WHEN replace_existing THEN $worker_task_id
      ELSE stream.worker_task_id
    END,
    stream.stream_generation = CASE
      WHEN created OR same_attempt THEN current_stream_generation
      WHEN replace_existing THEN current_stream_generation + 1
      ELSE current_stream_generation
    END,
    stream.fencing_token = CASE
      WHEN created OR same_attempt THEN current_fencing_token
      WHEN replace_existing THEN current_fencing_token + 1
      ELSE current_fencing_token
    END,
    stream.status = 'active',
    stream.updated_at = datetime()
RETURN CASE
         WHEN created THEN 'admitted'
         WHEN same_attempt THEN 'coalesced'
         WHEN replace_existing THEN 'replaced'
         ELSE 'coalesced'
       END AS admission_outcome,
       stream.source_key AS source_key,
       stream.control_instance_id AS control_instance_id,
       stream.stream_key AS stream_key,
       stream.logical_run_id AS logical_run_id,
       stream.ingest_run_id AS ingest_run_id,
       stream.attempt_generation AS attempt_generation,
       stream.stream_generation AS stream_generation,
       stream.fencing_token AS fencing_token,
       stream.worker_task_id AS worker_task_id
"""

CREATE_LOGICAL_RUN_AND_ATTEMPT = """
MATCH (source:SourceSystem {source_key: $source_key, is_active: true})
OPTIONAL MATCH (dispatch:BitrixDispatchControl {
  source_key: $source_key, control_instance_id: $control_instance_id
})
OPTIONAL MATCH (existing:IngestionLogicalRun {
  source_key: $source_key,
  control_instance_id: $control_instance_id,
  idempotency_key: $idempotency_key
})
WITH source, dispatch, existing
WHERE coalesce(dispatch.blocked, false) = false
   OR (
     existing IS NOT NULL
     AND existing.status IN ['paused_with_checkpoint', 'failed']
   )
MERGE (logical:IngestionLogicalRun {
  source_key: $source_key,
  control_instance_id: $control_instance_id,
  idempotency_key: $idempotency_key
})
ON CREATE SET
  logical.logical_run_id = randomUUID(),
  logical.control_instance_id = $control_instance_id,
  logical.mode = $mode,
  logical.dump_path = $dump_path,
  logical.entity_key = $entity_key,
  logical.configuration_fingerprint = $configuration_fingerprint,
  logical.connector_version = $connector_version,
  logical.checkpoint_schema_version = $checkpoint_schema_version,
  logical.status = 'queued',
  logical.current_phase = $initial_phase,
  logical.active_generation = 1,
  logical.created_at = datetime(),
  logical.updated_at = datetime(),
  logical.creation_token = $creation_token
WITH source, logical, coalesce(logical.creation_token = $creation_token, false) AS created
MERGE (logical)-[:FOR_SOURCE]->(source)
REMOVE logical.creation_token
WITH source, logical, created
WHERE created OR (
  logical.mode = $mode
  AND coalesce(logical.dump_path, '') = coalesce($dump_path, '')
  AND coalesce(logical.entity_key, '') = coalesce($entity_key, '')
  AND logical.configuration_fingerprint = $configuration_fingerprint
  AND logical.connector_version = $connector_version
  AND logical.checkpoint_schema_version = $checkpoint_schema_version
)
FOREACH (_ IN CASE WHEN created THEN [1] ELSE [] END |
  CREATE (attempt:IngestRun {
    ingest_run_id: randomUUID(),
    control_instance_id: $control_instance_id,
    logical_run_id: logical.logical_run_id,
    generation: logical.active_generation,
    worker_task_id: $worker_task_id,
    run_type: $run_type,
    mode: $mode,
    dump_path: $dump_path,
    entity_key: $entity_key,
    status: 'queued',
    queued_at: datetime(),
    record_count: 0,
    rejected_count: 0,
    metadata: '{}'
  })
  CREATE (attempt)-[:FROM_SOURCE]->(source)
  CREATE (logical)-[:HAS_ATTEMPT]->(attempt)
  CREATE (logical)-[:ACTIVE_ATTEMPT]->(attempt)
  CREATE (checkpoint:IngestionCheckpoint {
    control_instance_id: $control_instance_id,
    logical_run_id: logical.logical_run_id,
    phase: $initial_phase,
    status: 'active',
    generation: logical.active_generation,
    cursor_json: $initial_cursor_json,
    source_window_json: $initial_source_window_json,
    connector_version: $connector_version,
    schema_version: $checkpoint_schema_version,
    replay_boundary: $replay_boundary,
    committed_count: 0,
    duplicate_count: 0,
    excluded_count: 0,
    retry_count: 0,
    created_at: datetime(),
    updated_at: datetime()
  })
  CREATE (checkpoint)-[:CHECKPOINT_FOR]->(logical)
  CREATE (checkpoint)-[:PRODUCED_BY]->(attempt)
)
WITH logical, created
OPTIONAL MATCH (logical)-[:ACTIVE_ATTEMPT]->(active_attempt:IngestRun {
  control_instance_id: $control_instance_id
})
OPTIONAL MATCH (logical)-[:HAS_ATTEMPT]->(historical_attempt:IngestRun {
  control_instance_id: $control_instance_id
})
WITH logical, created, active_attempt, historical_attempt
ORDER BY historical_attempt.queued_at DESC
WITH logical, created, active_attempt, collect(historical_attempt)[0] AS latest_attempt
WITH logical, created, coalesce(active_attempt, latest_attempt) AS attempt
RETURN logical.logical_run_id AS logical_run_id,
       logical.control_instance_id AS control_instance_id,
       logical.status AS logical_status,
       logical.active_generation AS generation,
       attempt.ingest_run_id AS ingest_run_id,
       attempt.worker_task_id AS worker_task_id,
       created AS created
"""

CLAIM_QUEUED_ATTEMPT = """
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id, control_instance_id: $control_instance_id
})-[:ACTIVE_ATTEMPT]->(attempt:IngestRun {
  ingest_run_id: $ingest_run_id, control_instance_id: $control_instance_id
})
WHERE logical.active_generation = $generation
  AND attempt.generation = $generation
  AND attempt.worker_task_id = $worker_task_id
  AND logical.status IN ['queued', 'running']
  AND attempt.status IN ['queued', 'started']
SET logical.status = 'running',
    logical.updated_at = datetime(),
    attempt.status = 'started',
    attempt.started_at = coalesce(attempt.started_at, datetime())
RETURN logical.status AS logical_status,
       attempt.status AS attempt_status,
       logical.stop_requested_at IS NOT NULL AS stop_requested
"""

REQUEST_LOGICAL_RUN_STOP = """
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id, control_instance_id: $control_instance_id
})
WHERE logical.status IN ['queued', 'running', 'stop_requested']
SET logical.status = 'stop_requested',
    logical.stop_requested_at = coalesce(logical.stop_requested_at, datetime()),
    logical.stop_requested_by = coalesce(logical.stop_requested_by, $requested_by),
    logical.stop_reason = coalesce(logical.stop_reason, $reason),
    logical.updated_at = datetime()
RETURN logical.logical_run_id AS logical_run_id,
       logical.status AS status,
       logical.active_generation AS generation
"""

GET_ACTIVE_LOGICAL_RUN = """
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id, control_instance_id: $control_instance_id
})
OPTIONAL MATCH (logical)-[:ACTIVE_ATTEMPT]->(active_attempt:IngestRun {
  control_instance_id: $control_instance_id
})
OPTIONAL MATCH (logical)-[:HAS_ATTEMPT]->(historical_attempt:IngestRun {
  control_instance_id: $control_instance_id
})
WITH logical, active_attempt, historical_attempt
ORDER BY historical_attempt.queued_at DESC
WITH logical, active_attempt, collect(historical_attempt)[0] AS latest_attempt
WITH logical, coalesce(active_attempt, latest_attempt) AS attempt
OPTIONAL MATCH (checkpoint:IngestionCheckpoint {
  control_instance_id: $control_instance_id, logical_run_id: logical.logical_run_id
})
WHERE checkpoint.generation = logical.active_generation
  AND checkpoint.phase = logical.current_phase
RETURN logical.logical_run_id AS logical_run_id,
       logical.status AS status,
       logical.active_generation AS generation,
       logical.source_key AS source_key,
       logical.control_instance_id AS control_instance_id,
       logical.mode AS mode,
       logical.dump_path AS dump_path,
       logical.entity_key AS entity_key,
       logical.stop_requested_at IS NOT NULL AS stop_requested,
       logical.stop_reason AS stop_reason,
       attempt.ingest_run_id AS ingest_run_id,
       checkpoint.phase AS phase,
       checkpoint.cursor_json AS cursor_json,
       coalesce(checkpoint.committed_count, 0) AS committed_count,
       coalesce(checkpoint.duplicate_count, 0) AS duplicate_count,
       coalesce(checkpoint.excluded_count, 0) AS excluded_count,
       coalesce(checkpoint.retry_count, 0) AS retry_count,
       toString(checkpoint.updated_at) AS checkpointed_at
ORDER BY checkpoint.updated_at DESC
LIMIT 1
"""

ADVANCE_LOGICAL_CHECKPOINT = """
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id, control_instance_id: $control_instance_id
})-[:ACTIVE_ATTEMPT]->(attempt:IngestRun {
  ingest_run_id: $ingest_run_id, control_instance_id: $control_instance_id
})
MATCH (checkpoint:IngestionCheckpoint {
  control_instance_id: $control_instance_id,
  logical_run_id: $logical_run_id,
  phase: $phase
})
WHERE logical.active_generation = $generation
  AND attempt.generation = $generation
  AND checkpoint.generation = $generation
  AND checkpoint.status = 'active'
  AND checkpoint.connector_version = $connector_version
  AND checkpoint.schema_version = $checkpoint_schema_version
  AND checkpoint.source_window_json = $source_window_json
  AND logical.status IN ['running', 'stop_requested']
SET checkpoint.cursor_json = $cursor_json,
    checkpoint.last_committed_record_id = $last_committed_record_id,
    checkpoint.committed_count = $committed_count,
    checkpoint.duplicate_count = $duplicate_count,
    checkpoint.excluded_count = $excluded_count,
    checkpoint.retry_count = $retry_count,
    checkpoint.updated_at = datetime(),
    logical.current_phase = $phase,
    logical.committed_count = $committed_count,
    logical.duplicate_count = $duplicate_count,
    logical.excluded_count = $excluded_count,
    logical.retry_count = $retry_count,
    logical.updated_at = datetime()
RETURN logical.stop_requested_at IS NOT NULL AS stop_requested
"""

ADVANCE_BITRIX_UNIT_CHECKPOINT = """
MATCH (stream:BitrixIngestionStream {
  source_key: $source_key,
  control_instance_id: $control_instance_id,
  stream_key: $stream_key,
  logical_run_id: $logical_run_id,
  ingest_run_id: $ingest_run_id,
  attempt_generation: $attempt_generation,
  stream_generation: $stream_generation,
  fencing_token: $fencing_token,
  status: 'active'
})
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id, control_instance_id: $control_instance_id
})-[:ACTIVE_ATTEMPT]->(attempt:IngestRun {
  ingest_run_id: $ingest_run_id, control_instance_id: $control_instance_id
})
MATCH (checkpoint:IngestionCheckpoint {
  control_instance_id: $control_instance_id,
  logical_run_id: $logical_run_id,
  phase: $phase
})
WHERE logical.active_generation = $attempt_generation
  AND attempt.generation = $attempt_generation
  AND checkpoint.generation = $attempt_generation
  AND checkpoint.status = 'active'
  AND checkpoint.connector_version = $connector_version
  AND checkpoint.schema_version = $checkpoint_schema_version
  AND checkpoint.source_window_json = $source_window_json
  AND logical.status IN ['running', 'stop_requested']
SET checkpoint.cursor_json = $cursor_json,
    checkpoint.last_committed_record_id = $last_committed_record_id,
    checkpoint.committed_count = coalesce(checkpoint.committed_count, 0) + $committed_delta,
    checkpoint.duplicate_count = coalesce(checkpoint.duplicate_count, 0) + $duplicate_delta,
    checkpoint.excluded_count = coalesce(checkpoint.excluded_count, 0) + $excluded_delta,
    checkpoint.retry_count = coalesce(checkpoint.retry_count, 0) + $retry_delta,
    checkpoint.updated_at = datetime(),
    logical.current_phase = $phase,
    logical.committed_count = checkpoint.committed_count,
    logical.duplicate_count = checkpoint.duplicate_count,
    logical.excluded_count = checkpoint.excluded_count,
    logical.retry_count = checkpoint.retry_count,
    logical.updated_at = datetime()
RETURN logical.stop_requested_at IS NOT NULL AS stop_requested
"""

PAUSE_LOGICAL_RUN = """
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id, control_instance_id: $control_instance_id
})-[:ACTIVE_ATTEMPT]->(attempt:IngestRun {
  ingest_run_id: $ingest_run_id, control_instance_id: $control_instance_id
})
MATCH (checkpoint:IngestionCheckpoint {
  control_instance_id: $control_instance_id,
  logical_run_id: $logical_run_id,
  phase: $phase
})
WHERE logical.active_generation = $generation
  AND attempt.generation = $generation
  AND checkpoint.generation = $generation
  AND checkpoint.status = 'active'
  AND logical.status = 'stop_requested'
SET logical.status = 'paused_with_checkpoint',
    logical.paused_at = datetime(),
    logical.updated_at = datetime(),
    attempt.status = 'paused_with_checkpoint',
    attempt.finished_at = datetime(),
    checkpoint.status = 'paused',
    checkpoint.updated_at = datetime()
RETURN logical.logical_run_id AS logical_run_id,
       attempt.ingest_run_id AS ingest_run_id
"""

CREATE_RESUME_ATTEMPT = """
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id, control_instance_id: $control_instance_id
})
MATCH (logical)-[:FOR_SOURCE]->(source:SourceSystem)
OPTIONAL MATCH (logical)-[active_relation:ACTIVE_ATTEMPT]->(active_prior:IngestRun {
  control_instance_id: $control_instance_id
})
OPTIONAL MATCH (logical)-[:HAS_ATTEMPT]->(historical_prior:IngestRun {
  control_instance_id: $control_instance_id
})
WITH logical, source, active_relation, active_prior, historical_prior
ORDER BY historical_prior.queued_at DESC
WITH logical, source, active_relation, active_prior,
     collect(historical_prior)[0] AS latest_prior
WITH logical, source, active_relation,
     coalesce(active_prior, latest_prior) AS prior
MATCH (checkpoint:IngestionCheckpoint {
  control_instance_id: $control_instance_id, logical_run_id: $logical_run_id
})
WHERE prior IS NOT NULL
  AND logical.status IN ['paused_with_checkpoint', 'failed']
  AND checkpoint.status IN ['paused', 'active']
  AND logical.active_generation = checkpoint.generation
  AND logical.configuration_fingerprint = $configuration_fingerprint
  AND logical.connector_version = $logical_connector_version
  AND logical.checkpoint_schema_version = $checkpoint_schema_version
  AND checkpoint.connector_version = $checkpoint_connector_version
  AND checkpoint.schema_version = $checkpoint_schema_version
WITH logical, source, active_relation, prior, checkpoint,
     logical.active_generation + 1 AS generation
SET logical.active_generation = generation,
    logical.status = 'queued',
    logical.stop_requested_at = NULL,
    logical.stop_requested_by = NULL,
    logical.stop_reason = NULL,
    logical.updated_at = datetime(),
    prior.status = CASE
      WHEN prior.status IN ['queued', 'started'] THEN 'superseded'
      ELSE prior.status
    END,
    checkpoint.status = 'active',
    checkpoint.generation = generation,
    checkpoint.updated_at = datetime()
CREATE (attempt:IngestRun {
  ingest_run_id: randomUUID(),
  control_instance_id: $control_instance_id,
  logical_run_id: logical.logical_run_id,
  generation: generation,
  worker_task_id: $worker_task_id,
  run_type: logical.mode,
  mode: logical.mode,
  dump_path: logical.dump_path,
  entity_key: logical.entity_key,
  status: 'queued',
  queued_at: datetime(),
  resumed_from_run_id: prior.ingest_run_id,
  record_count: 0,
  rejected_count: 0,
  metadata: '{}'
})
CREATE (attempt)-[:FROM_SOURCE]->(source)
CREATE (logical)-[:HAS_ATTEMPT]->(attempt)
FOREACH (relation IN CASE WHEN active_relation IS NULL THEN [] ELSE [active_relation] END |
  DELETE relation
)
CREATE (logical)-[:ACTIVE_ATTEMPT]->(attempt)
MERGE (checkpoint)-[:PRODUCED_BY]->(attempt)
RETURN logical.logical_run_id AS logical_run_id,
       logical.control_instance_id AS control_instance_id,
       attempt.ingest_run_id AS ingest_run_id,
       generation AS generation,
       attempt.worker_task_id AS worker_task_id
"""

FINALIZE_LOGICAL_RUN = """
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id, control_instance_id: $control_instance_id
})-[active_relation:ACTIVE_ATTEMPT]->(attempt:IngestRun {
  ingest_run_id: $ingest_run_id, control_instance_id: $control_instance_id
})
MATCH (checkpoint:IngestionCheckpoint {
  control_instance_id: $control_instance_id,
  logical_run_id: $logical_run_id,
  phase: $phase
})
WHERE logical.active_generation = $generation
  AND attempt.generation = $generation
  AND checkpoint.generation = $generation
  AND checkpoint.status = 'active'
  AND logical.status = 'running'
  AND logical.stop_requested_at IS NULL
  AND $status IN ['completed', 'completed_with_errors']
SET logical.status = $status,
    logical.finished_at = datetime(),
    logical.updated_at = datetime(),
    logical.committed_count = $committed_count,
    logical.duplicate_count = $duplicate_count,
    logical.excluded_count = $excluded_count,
    logical.retry_count = $retry_count,
    attempt.status = $status,
    attempt.finished_at = datetime(),
    attempt.record_count = $record_count,
    attempt.rejected_count = $rejected_count,
    checkpoint.status = 'completed',
    checkpoint.updated_at = datetime()
DELETE active_relation
RETURN logical.logical_run_id AS logical_run_id,
       attempt.ingest_run_id AS ingest_run_id
"""

TRANSITION_LOGICAL_PHASE = """
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id, control_instance_id: $control_instance_id
})-[:ACTIVE_ATTEMPT]->(attempt:IngestRun {
  ingest_run_id: $ingest_run_id, control_instance_id: $control_instance_id
})
MATCH (current:IngestionCheckpoint {
  control_instance_id: $control_instance_id,
  logical_run_id: $logical_run_id,
  phase: $current_phase
})
WHERE logical.active_generation = $generation
  AND attempt.generation = $generation
  AND current.generation = $generation
  AND current.status = 'active'
  AND current.connector_version = logical.connector_version
  AND current.schema_version = logical.checkpoint_schema_version
  AND logical.status IN ['running', 'stop_requested']
OPTIONAL MATCH (existing_next:IngestionCheckpoint {
  control_instance_id: $control_instance_id,
  logical_run_id: $logical_run_id,
  phase: $next_phase
})
WITH logical, attempt, current, existing_next
WHERE existing_next IS NULL OR (
  existing_next.generation = $generation
  AND existing_next.connector_version = $connector_version
  AND existing_next.schema_version = $checkpoint_schema_version
)
SET current.status = 'completed',
    current.updated_at = datetime()
MERGE (next:IngestionCheckpoint {
  control_instance_id: $control_instance_id,
  logical_run_id: $logical_run_id,
  phase: $next_phase
})
ON CREATE SET next.created_at = datetime()
SET next.status = 'active',
    next.generation = $generation,
    next.cursor_json = $cursor_json,
    next.source_window_json = $source_window_json,
    next.connector_version = $connector_version,
    next.schema_version = $checkpoint_schema_version,
    next.replay_boundary = $replay_boundary,
    next.committed_count = $committed_count,
    next.duplicate_count = $duplicate_count,
    next.excluded_count = $excluded_count,
    next.retry_count = $retry_count,
    next.updated_at = datetime(),
    logical.current_phase = $next_phase,
    logical.committed_count = $committed_count,
    logical.duplicate_count = $duplicate_count,
    logical.excluded_count = $excluded_count,
    logical.retry_count = $retry_count,
    logical.updated_at = datetime()
MERGE (next)-[:CHECKPOINT_FOR]->(logical)
MERGE (next)-[:PRODUCED_BY]->(attempt)
RETURN logical.stop_requested_at IS NOT NULL AS stop_requested
"""

FAIL_LOGICAL_RUN = """
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id, control_instance_id: $control_instance_id
})-[active_relation:ACTIVE_ATTEMPT]->(attempt:IngestRun {
  ingest_run_id: $ingest_run_id, control_instance_id: $control_instance_id
})
WHERE logical.active_generation = $generation
  AND attempt.generation = $generation
  AND logical.status IN ['queued', 'running', 'stop_requested']
SET logical.status = 'failed',
    logical.finished_at = datetime(),
    logical.updated_at = datetime(),
    attempt.status = 'failed',
    attempt.finished_at = datetime(),
    attempt.failure_category = $failure_category,
    attempt.failure_message = $failure_message
DELETE active_relation
RETURN logical.logical_run_id AS logical_run_id,
       attempt.ingest_run_id AS ingest_run_id
"""
