"""Cypher for graph-authoritative bounded ingestion control."""

from __future__ import annotations

ENSURE_BOUNDED_LOGICAL_RUN = """
MATCH (source:SourceSystem {source_key: $source_key, is_active: true})
MATCH (reset:IngestionResetGeneration {
  environment: $environment,
  generation: $reset_generation,
  status: 'active'
})
OPTIONAL MATCH (dispatch:BitrixDispatchControl {
  source_key: $source_key,
  control_instance_id: $control_instance_id
})
WITH source, reset, dispatch
WHERE dispatch IS NULL OR coalesce(dispatch.blocked, false) = false
MERGE (scope:BoundedIngestionScope {scope_key: $scope_key})
ON CREATE SET scope.created_at = datetime(),
  scope.environment = $environment,
  scope.reset_generation = $reset_generation,
  scope.source_key = $source_key,
  scope.control_instance_id = $control_instance_id,
  scope.entity_key = $entity_key,
  scope.stream_key = $stream_key,
  scope.mode = $mode,
  scope.configuration_fingerprint = $configuration_fingerprint,
  scope.connector_version = $connector_version,
  scope.checkpoint_schema_version = $checkpoint_schema_version
WITH source, scope
WHERE scope.environment = $environment
  AND scope.reset_generation = $reset_generation
  AND scope.source_key = $source_key
  AND scope.control_instance_id = $control_instance_id
  AND coalesce(scope.entity_key, '') = coalesce($entity_key, '')
  AND coalesce(scope.stream_key, '') = coalesce($stream_key, '')
  AND scope.mode = $mode
  AND scope.configuration_fingerprint = $configuration_fingerprint
  AND scope.connector_version = $connector_version
  AND scope.checkpoint_schema_version = $checkpoint_schema_version
SET scope.run_lock_version = coalesce(scope.run_lock_version, 0) + 1
WITH source, scope
OPTIONAL MATCH (scope)-[:OWNS]->(unfinished:IngestionLogicalRun)
WHERE unfinished.bounded_status <> 'completed'
WITH source, scope, collect(unfinished) AS unfinished_runs
WHERE size(unfinished_runs) = 0
  OR all(run IN unfinished_runs WHERE run.bounded_logical_key = $logical_key)
MERGE (logical:IngestionLogicalRun {bounded_logical_key: $logical_key})
ON CREATE SET logical.logical_run_id = randomUUID(),
  logical.bounded_scope_key = $scope_key,
  logical.environment = $environment,
  logical.source_key = $source_key,
  logical.control_instance_id = $control_instance_id,
  logical.entity_key = $entity_key,
  logical.execution_stream = $stream_key,
  logical.mode = $mode,
  logical.status = 'queued',
  logical.bounded_status = 'queued',
  logical.active_generation = 0,
  logical.bounded_fencing_token = 0,
  logical.reset_generation = $reset_generation,
  logical.configuration_fingerprint = $configuration_fingerprint,
  logical.connector_version = $connector_version,
  logical.checkpoint_schema_version = $checkpoint_schema_version,
  logical.source_window_fingerprint = $source_window_fingerprint,
  logical.current_phase = $phase,
  logical.occurrence_id = $occurrence_id,
  logical.occurrence_timezone = $timezone,
  logical.occurrence_scheduled = $scheduled,
  logical.occurrence_starts_at = datetime($starts_at),
  logical.drain_starts_at = datetime($drain_starts_at),
  logical.cutoff_at = datetime($cutoff_at),
  logical.next_occurrence_at = datetime($next_eligible_at),
  logical.next_eligible_at = datetime($starts_at),
  logical.manual_pause = false,
  logical.usage_records = 0,
  logical.usage_source_requests = 0,
  logical.usage_pages = 0,
  logical.usage_bytes = 0,
  logical.usage_extraction_calls = 0,
  logical.reserved_records = 0,
  logical.reserved_source_requests = 0,
  logical.reserved_pages = 0,
  logical.reserved_bytes = 0,
  logical.reserved_extraction_calls = 0,
  logical.retry_backlog = 0,
  logical.created_at = datetime(),
  logical.updated_at = datetime()
WITH source, scope, logical
WHERE logical.bounded_scope_key = $scope_key
  AND logical.reset_generation = $reset_generation
  AND logical.configuration_fingerprint = $configuration_fingerprint
  AND logical.connector_version = $connector_version
  AND logical.checkpoint_schema_version = $checkpoint_schema_version
MERGE (scope)-[:OWNS]->(logical)
MERGE (logical)-[:FOR_SOURCE]->(source)
MERGE (checkpoint:IngestionCheckpoint {
  control_instance_id: $control_instance_id,
  logical_run_id: logical.logical_run_id,
  phase: $phase
})
ON CREATE SET checkpoint.generation = 0,
  checkpoint.status = 'paused',
  checkpoint.cursor_json = $cursor_json,
  checkpoint.source_window_json = $source_window_json,
  checkpoint.connector_version = $connector_version,
  checkpoint.schema_version = $checkpoint_schema_version,
  checkpoint.replay_boundary = $replay_boundary,
  checkpoint.committed_count = 0,
  checkpoint.duplicate_count = 0,
  checkpoint.excluded_count = 0,
  checkpoint.retry_count = 0,
  checkpoint.created_at = datetime(),
  checkpoint.updated_at = datetime()
MERGE (checkpoint)-[:CHECKPOINT_FOR]->(logical)
RETURN logical.logical_run_id AS logical_run_id,
  logical.bounded_status AS status,
  logical.manual_pause AS manual_pause,
  toString(logical.next_eligible_at) AS next_eligible_at,
  logical.occurrence_id AS occurrence_id
"""

REBIND_BOUNDED_OCCURRENCE = """
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id,
  reset_generation: $reset_generation
})
SET logical.rebind_lock_version = coalesce(logical.rebind_lock_version, 0) + 1
WITH logical
MATCH (source:SourceSystem {source_key: logical.source_key, is_active: true})
MATCH (:IngestionResetGeneration {
  environment: logical.environment,
  generation: $reset_generation,
  status: 'active'
})
OPTIONAL MATCH (dispatch:BitrixDispatchControl {
  source_key: logical.source_key,
  control_instance_id: logical.control_instance_id
})
WITH logical, source, dispatch
WHERE (dispatch IS NULL OR coalesce(dispatch.blocked, false) = false)
  AND coalesce(logical.manual_pause, false) = false
  AND $occurrence_id <> logical.occurrence_id
  AND datetime($starts_at) > logical.occurrence_starts_at
  AND datetime($starts_at) >= logical.next_occurrence_at
  AND datetime($starts_at) >= logical.next_eligible_at
  AND datetime($now) >= datetime($starts_at)
  AND datetime($now) < datetime($drain_starts_at)
  AND (
    logical.bounded_status IN ['queued', 'paused_with_checkpoint', 'failed']
    OR (
      logical.bounded_status = 'running'
      AND datetime($now) >= logical.lease_expires_at
    )
  )
OPTIONAL MATCH (logical)-[active:ACTIVE_ATTEMPT]->(attempt:IngestRun)
OPTIONAL MATCH (slot:BoundedIngestionGlobalSlot {
  environment: logical.environment,
  reset_generation: $reset_generation,
  slot_index: logical.global_slot_index,
  owner_logical_run_id: logical.logical_run_id
})
SET logical.occurrence_id = $occurrence_id,
  logical.occurrence_timezone = $timezone,
  logical.occurrence_scheduled = $scheduled,
  logical.occurrence_starts_at = datetime($starts_at),
  logical.drain_starts_at = datetime($drain_starts_at),
  logical.cutoff_at = datetime($cutoff_at),
  logical.next_occurrence_at = datetime($next_eligible_at),
  logical.next_eligible_at = datetime($starts_at),
  logical.bounded_status = 'paused_with_checkpoint',
  logical.status = 'paused_with_checkpoint',
  logical.pause_reason = 'schedule_window_closed',
  logical.worker_task_id = NULL,
  logical.lease_token = NULL,
  logical.reserved_records = 0,
  logical.reserved_source_requests = 0,
  logical.reserved_pages = 0,
  logical.reserved_bytes = 0,
  logical.reserved_extraction_calls = 0,
  logical.updated_at = datetime(),
  attempt.status = CASE
    WHEN attempt.status IN ['queued', 'started'] THEN 'superseded'
    ELSE attempt.status END,
  attempt.finished_at = CASE
    WHEN attempt.status IN ['queued', 'started'] THEN datetime()
    ELSE attempt.finished_at END,
  slot.owner_logical_run_id = NULL,
  slot.owner_attempt_generation = NULL,
  slot.owner_lease_token = NULL,
  slot.lease_expires_at = NULL,
  slot.updated_at = datetime()
FOREACH (_ IN CASE WHEN active IS NULL THEN [] ELSE [1] END | DELETE active)
RETURN logical.logical_run_id AS logical_run_id
"""

CLAIM_BOUNDED_ATTEMPT = """
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id,
  reset_generation: $reset_generation
})
SET logical.claim_lock_version = coalesce(logical.claim_lock_version, 0) + 1
WITH logical
MATCH (source:SourceSystem {source_key: $source_key, is_active: true})
MATCH (reset:IngestionResetGeneration {
  environment: $environment,
  generation: $reset_generation,
  status: 'active'
})
OPTIONAL MATCH (dispatch:BitrixDispatchControl {
  source_key: $source_key,
  control_instance_id: $control_instance_id
})
WITH logical, source, reset, dispatch
WHERE dispatch IS NULL OR coalesce(dispatch.blocked, false) = false
MATCH (checkpoint:IngestionCheckpoint {
  control_instance_id: $control_instance_id,
  logical_run_id: $logical_run_id,
  phase: logical.current_phase
})
WITH logical, checkpoint,
  logical.bounded_status = 'running'
    AND logical.worker_task_id = $worker_task_id
    AND logical.lease_token = $lease_token
    AND datetime($now) < logical.lease_expires_at AS same_claim
WHERE same_claim OR (
  coalesce(logical.manual_pause, false) = false
  AND datetime($now) >= logical.next_eligible_at
  AND datetime($now) >= logical.occurrence_starts_at
  AND datetime($now) < logical.drain_starts_at
  AND (
    logical.bounded_status IN ['queued', 'paused_with_checkpoint', 'failed']
    OR (
      logical.bounded_status = 'running'
      AND datetime($now) >= logical.lease_expires_at
    )
  )
)
WITH logical, checkpoint, same_claim,
  CASE WHEN same_claim THEN logical.active_generation ELSE logical.active_generation + 1 END
    AS generation,
  CASE WHEN same_claim THEN logical.bounded_fencing_token
       ELSE logical.bounded_fencing_token + 1 END AS fencing_token
UNWIND range(0, $max_graph_writers - 1) AS slot_index
MERGE (slot:BoundedIngestionGlobalSlot {
  environment: $environment,
  reset_generation: $reset_generation,
  slot_index: slot_index
})
ON CREATE SET slot.fencing_token = 0,
  slot.created_at = datetime()
SET slot.lock_version = coalesce(slot.lock_version, 0) + 1
WITH logical, checkpoint, same_claim, generation, fencing_token, slot
WHERE (
  same_claim
  AND slot.slot_index = logical.global_slot_index
  AND slot.owner_logical_run_id = logical.logical_run_id
  AND slot.owner_attempt_generation = logical.active_generation
  AND slot.owner_lease_token = logical.lease_token
  AND datetime($now) < slot.lease_expires_at
) OR (
  NOT same_claim
  AND (
    slot.owner_logical_run_id IS NULL
    OR datetime($now) >= slot.lease_expires_at
  )
)
ORDER BY slot.slot_index
WITH logical, checkpoint, same_claim, generation, fencing_token,
  head(collect(slot)) AS slot
WHERE slot IS NOT NULL
OPTIONAL MATCH (logical)-[old_active:ACTIVE_ATTEMPT]->(old_attempt:IngestRun)
FOREACH (_ IN CASE WHEN same_claim OR old_active IS NULL THEN [] ELSE [1] END |
  SET old_attempt.status = CASE
    WHEN old_attempt.status IN ['queued', 'started'] THEN 'superseded'
    ELSE old_attempt.status END,
    old_attempt.finished_at = CASE
      WHEN old_attempt.status IN ['queued', 'started'] THEN datetime()
      ELSE old_attempt.finished_at END
  DELETE old_active
)
FOREACH (_ IN CASE WHEN same_claim THEN [] ELSE [1] END |
  CREATE (attempt:IngestRun {
    ingest_run_id: randomUUID(),
    control_instance_id: $control_instance_id,
    logical_run_id: logical.logical_run_id,
    generation: generation,
    worker_task_id: $worker_task_id,
    lease_token: $lease_token,
    run_type: 'bounded',
    mode: logical.mode,
    entity_key: logical.entity_key,
    status: 'started',
    queued_at: datetime(),
    started_at: datetime(),
    record_count: 0,
    rejected_count: 0,
    metadata: '{}'
  })
  CREATE (logical)-[:HAS_ATTEMPT]->(attempt)
  CREATE (logical)-[:ACTIVE_ATTEMPT]->(attempt)
)
WITH logical, checkpoint, same_claim, generation, fencing_token, slot
OPTIONAL MATCH (logical)-[:ACTIVE_ATTEMPT]->(active_attempt:IngestRun)
WITH logical, checkpoint, same_claim, generation, fencing_token, slot, active_attempt,
  CASE WHEN same_claim THEN slot.fencing_token ELSE slot.fencing_token + 1 END
    AS global_slot_fencing_token
SET slot.owner_logical_run_id = logical.logical_run_id,
  slot.owner_attempt_generation = generation,
  slot.owner_lease_token = $lease_token,
  slot.fencing_token = global_slot_fencing_token,
  slot.lease_expires_at = datetime() + duration({seconds: $lease_seconds}),
  slot.updated_at = datetime(),
  logical.active_generation = generation,
  logical.bounded_fencing_token = fencing_token,
  logical.global_slot_index = slot.slot_index,
  logical.global_slot_fencing_token = global_slot_fencing_token,
  logical.bounded_status = 'running',
  logical.status = 'running',
  logical.worker_task_id = $worker_task_id,
  logical.lease_token = $lease_token,
  logical.lease_expires_at = datetime() + duration({seconds: $lease_seconds}),
  logical.failure_category = NULL,
  logical.publication_intent = false,
  logical.updated_at = datetime(),
  checkpoint.generation = generation,
  checkpoint.status = 'active',
  checkpoint.updated_at = datetime()
RETURN logical.logical_run_id AS logical_run_id,
  active_attempt.ingest_run_id AS ingest_run_id,
  generation AS attempt_generation,
  fencing_token AS fencing_token,
  logical.lease_token AS lease_token,
  toString(logical.lease_expires_at) AS lease_expires_at,
  slot.slot_index AS global_slot_index,
  global_slot_fencing_token AS global_slot_fencing_token,
  checkpoint.phase AS phase,
  checkpoint.cursor_json AS cursor_json,
  checkpoint.source_window_json AS source_window_json,
  checkpoint.last_committed_record_id AS last_committed_record_id,
  checkpoint.connector_version AS connector_version,
  checkpoint.schema_version AS checkpoint_schema_version,
  checkpoint.replay_boundary AS replay_boundary,
  coalesce(logical.usage_records, 0) AS usage_records,
  coalesce(logical.usage_source_requests, 0) AS usage_source_requests,
  coalesce(logical.usage_pages, 0) AS usage_pages,
  coalesce(logical.usage_bytes, 0) AS usage_bytes_read,
  coalesce(logical.usage_extraction_calls, 0) AS usage_extraction_calls,
  coalesce(logical.reserved_records, 0) AS reserved_records,
  coalesce(logical.reserved_source_requests, 0) AS reserved_source_requests,
  coalesce(logical.reserved_pages, 0) AS reserved_pages,
  coalesce(logical.reserved_bytes, 0) AS reserved_bytes_read,
  coalesce(logical.reserved_extraction_calls, 0) AS reserved_extraction_calls,
  coalesce(logical.retry_backlog, 0) AS retry_backlog,
  coalesce(logical.terminal_observed, false) AS terminal_observed,
  coalesce(checkpoint.terminal_committed, false) AS terminal_checkpoint_committed
"""

_BOUNDED_WRITE_AUTHORITY = """
MATCH (source:SourceSystem {source_key: logical.source_key})
MATCH (reset:IngestionResetGeneration {
  environment: logical.environment,
  generation: $reset_generation
})
MATCH (slot:BoundedIngestionGlobalSlot {
  environment: logical.environment,
  reset_generation: $reset_generation,
  slot_index: $global_slot_index
})
OPTIONAL MATCH (dispatch:BitrixDispatchControl {
  source_key: logical.source_key,
  control_instance_id: logical.control_instance_id
})
SET source.bounded_write_lock_version =
    coalesce(source.bounded_write_lock_version, 0) + 1,
  reset.bounded_write_lock_version =
    coalesce(reset.bounded_write_lock_version, 0) + 1,
  slot.bounded_write_lock_version =
    coalesce(slot.bounded_write_lock_version, 0) + 1
FOREACH (_ IN CASE WHEN dispatch IS NULL THEN [] ELSE [1] END |
  SET dispatch.bounded_write_lock_version =
    coalesce(dispatch.bounded_write_lock_version, 0) + 1
)
WITH logical, source, reset, slot, dispatch
WHERE source.is_active = true
  AND reset.status = 'active'
  AND slot.owner_logical_run_id = logical.logical_run_id
  AND slot.owner_attempt_generation = $attempt_generation
  AND slot.owner_lease_token = $lease_token
  AND slot.fencing_token = $global_slot_fencing_token
  AND datetime() < logical.lease_expires_at
  AND datetime() < logical.cutoff_at
  AND datetime() < slot.lease_expires_at
  AND (dispatch IS NULL OR coalesce(dispatch.blocked, false) = false)
"""

RESERVE_BOUNDED_USAGE = (
    """
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id,
  reset_generation: $reset_generation,
  active_generation: $attempt_generation,
  bounded_fencing_token: $fencing_token,
  bounded_status: 'running',
  worker_task_id: $worker_task_id,
  lease_token: $lease_token
})
"""
    + _BOUNDED_WRITE_AUTHORITY
    + """
OPTIONAL MATCH (existing:BoundedUsageReservation {
  logical_run_id: $logical_run_id,
  attempt_generation: $attempt_generation,
  reservation_key: $reservation_key
})
WITH logical, slot, existing
WHERE existing IS NOT NULL OR (
  coalesce(logical.reserved_records, 0) + $records <= $max_records
  AND coalesce(logical.reserved_source_requests, 0) + $source_requests
    <= $max_source_requests
  AND coalesce(logical.reserved_pages, 0) + $pages <= $max_pages
  AND coalesce(logical.reserved_bytes, 0) + $bytes_read <= $max_bytes
  AND coalesce(logical.reserved_extraction_calls, 0) + $extraction_calls
    <= $max_extraction_calls
)
MERGE (reservation:BoundedUsageReservation {
  logical_run_id: $logical_run_id,
  attempt_generation: $attempt_generation,
  reservation_key: $reservation_key
})
ON CREATE SET reservation.creation_token = $creation_token,
  reservation.created_at = datetime(),
  reservation.records = $records,
  reservation.source_requests = $source_requests,
  reservation.pages = $pages,
  reservation.bytes_read = $bytes_read,
  reservation.extraction_calls = $extraction_calls
WITH logical, reservation,
  reservation.creation_token = $creation_token AS created
REMOVE reservation.creation_token
FOREACH (_ IN CASE WHEN created THEN [1] ELSE [] END |
  SET logical.reserved_records = coalesce(logical.reserved_records, 0) + $records,
    logical.reserved_source_requests =
      coalesce(logical.reserved_source_requests, 0) + $source_requests,
    logical.reserved_pages = coalesce(logical.reserved_pages, 0) + $pages,
    logical.reserved_bytes = coalesce(logical.reserved_bytes, 0) + $bytes_read,
    logical.reserved_extraction_calls =
      coalesce(logical.reserved_extraction_calls, 0) + $extraction_calls,
    logical.updated_at = datetime()
)
RETURN logical.logical_run_id AS logical_run_id,
  created AS created
"""
)

CLAIM_BOUNDED_RECEIPT = (
    """
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id,
  reset_generation: $reset_generation,
  active_generation: $attempt_generation,
  bounded_fencing_token: $fencing_token,
  bounded_status: 'running',
  worker_task_id: $worker_task_id,
  lease_token: $lease_token
})
"""
    + _BOUNDED_WRITE_AUTHORITY
    + """
MATCH (checkpoint:IngestionCheckpoint {
  control_instance_id: logical.control_instance_id,
  logical_run_id: $logical_run_id,
  phase: $phase,
  generation: $attempt_generation,
  status: 'active'
})
WHERE checkpoint.cursor_json = $cursor_before_json
MERGE (receipt:BoundedIngestionReceipt {
  logical_run_id: $logical_run_id,
  attempt_generation: $attempt_generation,
  replay_id: $replay_id
})
ON CREATE SET receipt.creation_token = $creation_token,
  receipt.status = 'pending',
  receipt.created_at = datetime()
WITH receipt, receipt.creation_token = $creation_token AS created
REMOVE receipt.creation_token
RETURN created AS created,
  receipt.status AS status,
  receipt.dispositions_json AS dispositions_json
"""
)


LOAD_BOUNDED_RETRIES = """
MATCH (retry:BoundedIngestionRetry {
  logical_run_id: $logical_run_id,
  replay_id: $replay_id,
  status: 'pending'
})
RETURN retry.source_record_id AS source_record_id,
  retry.source_version AS source_version,
  retry.category AS category,
  retry.attempt_count AS attempt_count,
  toString(retry.eligible_at) AS eligible_at
ORDER BY retry.source_record_id
"""

PERSIST_BOUNDED_RETRY = """
MATCH (logical:IngestionLogicalRun {logical_run_id: $logical_run_id})
MERGE (retry:BoundedIngestionRetry {
  logical_run_id: $logical_run_id,
  replay_id: $replay_id,
  source_record_id: $source_record_id
})
ON CREATE SET retry.creation_token = $creation_token,
  retry.created_at = datetime()
SET retry.source_version = $source_version,
  retry.category = $category,
  retry.attempt_count = $attempt_count,
  retry.eligible_at = $eligible_at,
  retry.status = 'pending',
  retry.updated_at = datetime()
MERGE (retry)-[:RETRY_FOR]->(logical)
WITH retry, retry.creation_token = $creation_token AS created
REMOVE retry.creation_token
RETURN created AS created
"""

RESOLVE_BOUNDED_RETRY = """
MATCH (logical:IngestionLogicalRun {logical_run_id: $logical_run_id})
MATCH (retry:BoundedIngestionRetry {
  logical_run_id: $logical_run_id,
  replay_id: $replay_id,
  source_record_id: $source_record_id,
  status: 'pending'
})-[:RETRY_FOR]->(logical)
SET retry.status = 'resolved',
  retry.resolved_at = datetime(),
  retry.updated_at = datetime()
RETURN retry.source_record_id AS source_record_id
"""

FINALIZE_BOUNDED_UNIT = (
    """
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id,
  reset_generation: $reset_generation,
  active_generation: $attempt_generation,
  bounded_fencing_token: $fencing_token,
  bounded_status: 'running',
  worker_task_id: $worker_task_id,
  lease_token: $lease_token
})
"""
    + _BOUNDED_WRITE_AUTHORITY
    + """
MATCH (checkpoint:IngestionCheckpoint {
  control_instance_id: logical.control_instance_id,
  logical_run_id: $logical_run_id,
  phase: $phase,
  generation: $attempt_generation,
  status: 'active'
})
MATCH (receipt:BoundedIngestionReceipt {
  logical_run_id: $logical_run_id,
  attempt_generation: $attempt_generation,
  replay_id: $replay_id
})
WHERE receipt.status IN ['pending', 'retry_pending']
SET receipt.status = CASE
    WHEN $checkpoint_can_advance THEN 'committed'
    ELSE 'retry_pending' END,
  receipt.dispositions_json = $dispositions_json,
  receipt.terminal = $terminal,
  receipt.committed_at = datetime(),
  receipt.records = $records,
  receipt.source_requests = $source_requests,
  receipt.pages = $pages,
  receipt.bytes_read = $bytes_read,
  receipt.extraction_calls = $extraction_calls,
  checkpoint.cursor_json = CASE
    WHEN $checkpoint_can_advance THEN $cursor_after_json
    ELSE checkpoint.cursor_json END,
  checkpoint.last_committed_record_id = CASE
    WHEN $checkpoint_can_advance THEN $last_committed_record_id
    ELSE checkpoint.last_committed_record_id END,
  checkpoint.terminal_observed = coalesce(checkpoint.terminal_observed, false) OR $terminal,
  checkpoint.terminal_committed = CASE
    WHEN $terminal AND $checkpoint_can_advance THEN true
    ELSE coalesce(checkpoint.terminal_committed, false) END,
  checkpoint.committed_count = coalesce(checkpoint.committed_count, 0) + $committed_delta,
  checkpoint.duplicate_count = coalesce(checkpoint.duplicate_count, 0) + $duplicate_delta,
  checkpoint.excluded_count = coalesce(checkpoint.excluded_count, 0) + $excluded_delta,
  checkpoint.retry_count = coalesce(checkpoint.retry_count, 0) + $retry_delta,
  checkpoint.updated_at = datetime(),
  logical.usage_records = coalesce(logical.usage_records, 0) + $records,
  logical.usage_source_requests =
    coalesce(logical.usage_source_requests, 0) + $source_requests,
  logical.usage_pages = coalesce(logical.usage_pages, 0) + $pages,
  logical.usage_bytes = coalesce(logical.usage_bytes, 0) + $bytes_read,
  logical.usage_extraction_calls =
    coalesce(logical.usage_extraction_calls, 0) + $extraction_calls,
  logical.retry_backlog = CASE
    WHEN coalesce(logical.retry_backlog, 0) + $retry_backlog_delta < 0 THEN 0
    ELSE coalesce(logical.retry_backlog, 0) + $retry_backlog_delta END,
  logical.terminal_observed = coalesce(logical.terminal_observed, false) OR $terminal,
  logical.current_phase = checkpoint.phase,
  logical.updated_at = datetime()
RETURN logical.logical_run_id AS logical_run_id
"""
)

PAUSE_BOUNDED_RUN = """
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id,
  reset_generation: $reset_generation,
  active_generation: $attempt_generation,
  bounded_fencing_token: $fencing_token,
  bounded_status: 'running',
  worker_task_id: $worker_task_id,
  lease_token: $lease_token
})

MATCH (source:SourceSystem {source_key: logical.source_key})
MATCH (reset:IngestionResetGeneration {
  environment: logical.environment,
  generation: $reset_generation
})
MATCH (slot:BoundedIngestionGlobalSlot {
  environment: logical.environment,
  reset_generation: $reset_generation,
  slot_index: $global_slot_index
})
OPTIONAL MATCH (dispatch:BitrixDispatchControl {
  source_key: logical.source_key,
  control_instance_id: logical.control_instance_id
})
SET source.bounded_state_lock_version =
    coalesce(source.bounded_state_lock_version, 0) + 1,
  reset.bounded_state_lock_version =
    coalesce(reset.bounded_state_lock_version, 0) + 1,
  slot.bounded_state_lock_version =
    coalesce(slot.bounded_state_lock_version, 0) + 1
FOREACH (_ IN CASE WHEN dispatch IS NULL THEN [] ELSE [1] END |
  SET dispatch.bounded_state_lock_version =
    coalesce(dispatch.bounded_state_lock_version, 0) + 1
)
WITH logical, source, reset, slot, dispatch
WHERE source.is_active = true
  AND reset.status = 'active'
  AND slot.owner_logical_run_id = logical.logical_run_id
  AND slot.owner_attempt_generation = $attempt_generation
  AND slot.owner_lease_token = $lease_token
  AND slot.fencing_token = $global_slot_fencing_token
  AND datetime() < logical.lease_expires_at
  AND datetime() < slot.lease_expires_at
  AND (dispatch IS NULL OR coalesce(dispatch.blocked, false) = false)
OPTIONAL MATCH (logical)-[active:ACTIVE_ATTEMPT]->(attempt:IngestRun)
SET logical.bounded_status = 'paused_with_checkpoint',
  logical.status = 'paused_with_checkpoint',
  logical.pause_reason = $pause_reason,
  logical.next_eligible_at = datetime($next_eligible_at),
  logical.worker_task_id = NULL,
  logical.lease_token = NULL,
  logical.updated_at = datetime(),
  attempt.status = 'paused_with_checkpoint',
  attempt.finished_at = datetime(),
  slot.owner_logical_run_id = NULL,
  slot.owner_attempt_generation = NULL,
  slot.owner_lease_token = NULL,
  slot.lease_expires_at = NULL,
  slot.updated_at = datetime()
FOREACH (_ IN CASE WHEN active IS NULL THEN [] ELSE [1] END | DELETE active)
RETURN logical.logical_run_id AS logical_run_id
"""

FAIL_BOUNDED_RUN = """
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id,
  reset_generation: $reset_generation,
  active_generation: $attempt_generation,
  bounded_fencing_token: $fencing_token,
  bounded_status: 'running',
  worker_task_id: $worker_task_id,
  lease_token: $lease_token
})

MATCH (source:SourceSystem {source_key: logical.source_key})
MATCH (reset:IngestionResetGeneration {
  environment: logical.environment,
  generation: $reset_generation
})
MATCH (slot:BoundedIngestionGlobalSlot {
  environment: logical.environment,
  reset_generation: $reset_generation,
  slot_index: $global_slot_index
})
OPTIONAL MATCH (dispatch:BitrixDispatchControl {
  source_key: logical.source_key,
  control_instance_id: logical.control_instance_id
})
SET source.bounded_state_lock_version =
    coalesce(source.bounded_state_lock_version, 0) + 1,
  reset.bounded_state_lock_version =
    coalesce(reset.bounded_state_lock_version, 0) + 1,
  slot.bounded_state_lock_version =
    coalesce(slot.bounded_state_lock_version, 0) + 1
FOREACH (_ IN CASE WHEN dispatch IS NULL THEN [] ELSE [1] END |
  SET dispatch.bounded_state_lock_version =
    coalesce(dispatch.bounded_state_lock_version, 0) + 1
)
WITH logical, source, reset, slot, dispatch
WHERE source.is_active = true
  AND reset.status = 'active'
  AND slot.owner_logical_run_id = logical.logical_run_id
  AND slot.owner_attempt_generation = $attempt_generation
  AND slot.owner_lease_token = $lease_token
  AND slot.fencing_token = $global_slot_fencing_token
  AND datetime() < logical.lease_expires_at
  AND datetime() < slot.lease_expires_at
  AND (dispatch IS NULL OR coalesce(dispatch.blocked, false) = false)
OPTIONAL MATCH (logical)-[active:ACTIVE_ATTEMPT]->(attempt:IngestRun)
SET logical.bounded_status = 'failed',
  logical.status = 'failed',
  logical.failure_category = $failure_category,
  logical.failure_message = $failure_message,
  logical.next_eligible_at = datetime($next_eligible_at),
  logical.worker_task_id = NULL,
  logical.lease_token = NULL,
  logical.updated_at = datetime(),
  attempt.status = 'failed',
  attempt.failure_category = $failure_category,
  attempt.failure_message = $failure_message,
  attempt.finished_at = datetime(),
  slot.owner_logical_run_id = NULL,
  slot.owner_attempt_generation = NULL,
  slot.owner_lease_token = NULL,
  slot.lease_expires_at = NULL,
  slot.updated_at = datetime()
FOREACH (_ IN CASE WHEN active IS NULL THEN [] ELSE [1] END | DELETE active)
RETURN logical.logical_run_id AS logical_run_id
"""

FINALIZE_BOUNDED_RUN = """
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id,
  reset_generation: $reset_generation,
  active_generation: $attempt_generation,
  bounded_fencing_token: $fencing_token,
  bounded_status: 'running',
  worker_task_id: $worker_task_id,
  lease_token: $lease_token
})

MATCH (source:SourceSystem {source_key: logical.source_key})
MATCH (reset:IngestionResetGeneration {
  environment: logical.environment,
  generation: $reset_generation
})
MATCH (slot:BoundedIngestionGlobalSlot {
  environment: logical.environment,
  reset_generation: $reset_generation,
  slot_index: $global_slot_index
})
OPTIONAL MATCH (dispatch:BitrixDispatchControl {
  source_key: logical.source_key,
  control_instance_id: logical.control_instance_id
})
SET source.bounded_state_lock_version =
    coalesce(source.bounded_state_lock_version, 0) + 1,
  reset.bounded_state_lock_version =
    coalesce(reset.bounded_state_lock_version, 0) + 1,
  slot.bounded_state_lock_version =
    coalesce(slot.bounded_state_lock_version, 0) + 1
FOREACH (_ IN CASE WHEN dispatch IS NULL THEN [] ELSE [1] END |
  SET dispatch.bounded_state_lock_version =
    coalesce(dispatch.bounded_state_lock_version, 0) + 1
)
WITH logical, source, reset, slot, dispatch
WHERE source.is_active = true
  AND reset.status = 'active'
  AND slot.owner_logical_run_id = logical.logical_run_id
  AND slot.owner_attempt_generation = $attempt_generation
  AND slot.owner_lease_token = $lease_token
  AND slot.fencing_token = $global_slot_fencing_token
  AND datetime() < logical.lease_expires_at
  AND datetime() < slot.lease_expires_at
  AND (dispatch IS NULL OR coalesce(dispatch.blocked, false) = false)
MATCH (checkpoint:IngestionCheckpoint {
  logical_run_id: $logical_run_id,
  phase: logical.current_phase,
  generation: $attempt_generation,
  status: 'active',
  terminal_committed: true
})
WHERE coalesce(logical.retry_backlog, 0) = 0
OPTIONAL MATCH (logical)-[active:ACTIVE_ATTEMPT]->(attempt:IngestRun)
SET logical.bounded_status = 'completed',
  logical.status = 'completed',
  logical.pause_reason = NULL,
  logical.worker_task_id = NULL,
  logical.lease_token = NULL,
  logical.publication_intent = false,
  logical.finished_at = datetime(),
  logical.updated_at = datetime(),
  checkpoint.status = 'completed',
  checkpoint.updated_at = datetime(),
  attempt.status = 'completed',
  attempt.finished_at = datetime(),
  attempt.record_count = coalesce(logical.usage_records, 0),
  attempt.rejected_count = 0,
  slot.owner_logical_run_id = NULL,
  slot.owner_attempt_generation = NULL,
  slot.owner_lease_token = NULL,
  slot.lease_expires_at = NULL,
  slot.updated_at = datetime()
FOREACH (_ IN CASE WHEN active IS NULL THEN [] ELSE [1] END | DELETE active)
RETURN logical.logical_run_id AS logical_run_id
"""

GET_BOUNDED_STATUS = """
MATCH (logical:IngestionLogicalRun {logical_run_id: $logical_run_id})
OPTIONAL MATCH (checkpoint:IngestionCheckpoint {
  logical_run_id: logical.logical_run_id,
  phase: logical.current_phase,
  generation: logical.active_generation
})
OPTIONAL MATCH (retry:BoundedIngestionRetry {
  logical_run_id: logical.logical_run_id,
  status: 'pending'
})
WITH logical, checkpoint, min(retry.created_at) AS retry_oldest_at
RETURN logical.logical_run_id AS logical_run_id,
  logical.source_key AS source_key,
  logical.control_instance_id AS control_instance_id,
  logical.entity_key AS entity_key,
  logical.bounded_status AS status,
  logical.pause_reason AS pause_reason,
  logical.occurrence_id AS occurrence_id,
  logical.occurrence_timezone AS timezone,
  toString(logical.occurrence_starts_at) AS starts_at,
  toString(logical.drain_starts_at) AS drain_starts_at,
  toString(logical.cutoff_at) AS cutoff_at,
  toString(logical.next_eligible_at) AS next_eligible_at,
  coalesce(logical.usage_records, 0) AS records,
  coalesce(logical.usage_source_requests, 0) AS source_requests,
  coalesce(logical.usage_pages, 0) AS pages,
  coalesce(logical.usage_bytes, 0) AS bytes_read,
  coalesce(logical.usage_extraction_calls, 0) AS extraction_calls,
  coalesce(logical.reserved_records, 0) AS reserved_records,
  coalesce(logical.reserved_source_requests, 0) AS reserved_source_requests,
  coalesce(logical.reserved_pages, 0) AS reserved_pages,
  coalesce(logical.reserved_bytes, 0) AS reserved_bytes_read,
  coalesce(logical.reserved_extraction_calls, 0) AS reserved_extraction_calls,
  coalesce(logical.active_generation, 0) AS attempt_generation,
  logical.source_window_fingerprint AS source_window_fingerprint,
  checkpoint IS NOT NULL AS checkpoint_cursor_present,
  checkpoint.phase AS phase,
  toString(checkpoint.updated_at) AS checkpointed_at,
  coalesce(logical.retry_backlog, 0) AS retry_backlog,
  toString(retry_oldest_at) AS retry_oldest_at,
  logical.failure_category AS failure_category
LIMIT 1
"""

REQUEST_MANUAL_PAUSE = """
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id,
  source_key: $source_key,
  control_instance_id: $control_instance_id,
  reset_generation: $reset_generation
})
WHERE logical.bounded_status IN ['queued', 'running', 'paused_with_checkpoint']
SET logical.manual_pause = true,
  logical.pause_reason = 'manual',
  logical.bounded_status = 'paused_with_checkpoint',
  logical.status = 'paused_with_checkpoint',
  logical.next_eligible_at = datetime('9999-12-31T23:59:59Z'),
  logical.updated_at = datetime()
RETURN logical.logical_run_id AS logical_run_id
"""

RELEASE_MANUAL_PAUSE = """
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id,
  source_key: $source_key,
  control_instance_id: $control_instance_id,
  reset_generation: $reset_generation,
  bounded_status: 'paused_with_checkpoint'
})
MATCH (:IngestionResetGeneration {
  environment: logical.environment,
  generation: $reset_generation,
  status: 'active'
})
WHERE coalesce(logical.manual_pause, false) = true
SET logical.manual_pause = false,
  logical.pause_reason = NULL,
  logical.next_eligible_at = logical.occurrence_starts_at,
  logical.publication_intent = true,
  logical.publication_requested_at = datetime(),
  logical.updated_at = datetime()
RETURN logical.logical_run_id AS logical_run_id
"""
GET_BOUNDED_RECOVERY = """
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id,
  source_key: $source_key,
  control_instance_id: $control_instance_id,
  reset_generation: $reset_generation
})
MATCH (:IngestionResetGeneration {
  environment: logical.environment,
  generation: $reset_generation,
  status: 'active'
})
MATCH (checkpoint:IngestionCheckpoint {
  control_instance_id: logical.control_instance_id,
  logical_run_id: logical.logical_run_id,
  phase: logical.current_phase,
  generation: logical.active_generation
})
WHERE coalesce(logical.manual_pause, false) = false
  AND coalesce(logical.recovery_authorized, false) = true
RETURN logical.bounded_status AS recovery_status,
  toString(logical.lease_expires_at) AS lease_expires_at,
  logical.environment AS environment,
  logical.source_key AS source_key,
  logical.control_instance_id AS control_instance_id,
  logical.entity_key AS entity_key,
  logical.execution_stream AS stream_key,
  logical.mode AS mode,
  logical.configuration_fingerprint AS configuration_fingerprint,
  logical.connector_version AS connector_version,
  logical.checkpoint_schema_version AS checkpoint_schema_version,
  checkpoint.source_window_json AS source_window_json,
  logical.occurrence_id AS occurrence_id,
  logical.occurrence_timezone AS timezone,
  coalesce(logical.occurrence_scheduled, true) AS scheduled,
  toString(logical.occurrence_starts_at) AS starts_at,
  toString(logical.drain_starts_at) AS drain_starts_at,
  toString(logical.cutoff_at) AS cutoff_at,
  toString(logical.next_occurrence_at) AS next_eligible_at
LIMIT 1
"""
GET_ACTIVE_RESET_GENERATION = """
MATCH (reset:IngestionResetGeneration {environment: $environment, status: 'active'})
RETURN reset.generation AS generation
ORDER BY reset.generation DESC
LIMIT 1
"""

COMPARE_AND_ADVANCE_RESET_GENERATION = """
MATCH (current:IngestionResetGeneration {
  environment: $environment,
  generation: $expected_generation
})
SET current.cas_lock_version = coalesce(current.cas_lock_version, 0) + 1
WITH current
WHERE current.status = 'active'
SET current.status = 'retired',
  current.retired_at = datetime(),
  current.retired_by = $actor,
  current.retirement_reference = $reference
CREATE (next:IngestionResetGeneration {
  environment: $environment,
  generation: $expected_generation + 1,
  status: 'active',
  created_at: datetime(),
  created_by: $actor,
  authorization_reference: $reference
})
RETURN next.generation AS generation
"""
PAUSE_BOUNDED_UNCLAIMED = """
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id,
  source_key: $source_key,
  control_instance_id: $control_instance_id,
  reset_generation: $reset_generation
})
WHERE logical.bounded_status IN ['queued', 'paused_with_checkpoint', 'failed']
  AND coalesce(logical.manual_pause, false) = false
SET logical.bounded_status = 'paused_with_checkpoint',
  logical.status = 'paused_with_checkpoint',
  logical.pause_reason = $pause_reason,
  logical.next_eligible_at = CASE
    WHEN $next_eligible_at IS NULL THEN logical.next_eligible_at
    ELSE datetime($next_eligible_at) END,
  logical.updated_at = datetime()
RETURN logical.logical_run_id AS logical_run_id
"""

RETIRE_OWNED_BITRIX_PREDECESSOR = """
MATCH (stream:BitrixIngestionStream {
  source_key: 'bitrix_chat',
  control_instance_id: $control_instance_id,
  stream_key: $stream_key
})
SET stream.bounded_takeover_lock_version =
  coalesce(stream.bounded_takeover_lock_version, 0) + 1
WITH stream
WHERE stream.logical_run_id = $logical_run_id
  AND stream.attempt_generation < $attempt_generation
  AND stream.status = 'active'
SET stream.status = 'superseded',
  stream.finished_at = datetime(),
  stream.updated_at = datetime()
RETURN stream.logical_run_id AS logical_run_id
"""
