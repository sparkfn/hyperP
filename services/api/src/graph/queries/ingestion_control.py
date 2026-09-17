"""Cypher for redacted bounded logical-run status and operator controls."""

from __future__ import annotations

_STATUS_PROJECTION = """
WITH logical, checkpoint, min(retry.created_at) AS retry_oldest_at
RETURN logical.logical_run_id AS logical_run_id,
  logical.source_key AS source_key,
  logical.control_instance_id AS control_instance_id,
  logical.entity_key AS entity_key,
  coalesce(logical.bounded_status, logical.status) AS status,
  logical.pause_reason AS pause_reason,
  logical.occurrence_id AS occurrence_id,
  coalesce(logical.occurrence_timezone, logical.timezone) AS timezone,
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
  checkpoint.cursor_json IS NOT NULL AS checkpoint_cursor_present,
  coalesce(checkpoint.phase, logical.current_phase) AS phase,
  toString(checkpoint.updated_at) AS checkpointed_at,
  coalesce(logical.retry_backlog, 0) AS retry_backlog,
  toString(retry_oldest_at) AS retry_oldest_at,
  logical.failure_category AS failure_category
"""


_CONTROL_STATUS_PROJECTION = _STATUS_PROJECTION.replace(
    "WITH logical, checkpoint,",
    "WITH logical, checkpoint, outcome,",
    1,
).replace("RETURN", "RETURN outcome AS outcome,", 1)

GET_BOUNDED_LOGICAL_RUN = (
    """
MATCH (logical:IngestionLogicalRun {logical_run_id: $logical_run_id})
WHERE logical.bounded_scope_key IS NOT NULL
OPTIONAL MATCH (checkpoint:IngestionCheckpoint {
  control_instance_id: logical.control_instance_id,
  logical_run_id: logical.logical_run_id,
  generation: logical.active_generation,
  phase: logical.current_phase
})
OPTIONAL MATCH (retry:BoundedIngestionRetry {
  logical_run_id: logical.logical_run_id,
  status: 'pending'
})
"""
    + _STATUS_PROJECTION
)

PAUSE_BOUNDED_LOGICAL_RUN = (
    """
MATCH (logical:IngestionLogicalRun {logical_run_id: $logical_run_id})
WHERE logical.bounded_scope_key IS NOT NULL
WITH logical,
  logical.source_key = $source_key
    AND logical.control_instance_id = $control_instance_id
    AND logical.reset_generation = $reset_generation AS identity_matches,
  coalesce(logical.bounded_status, logical.status) AS status_before
FOREACH (_ IN CASE
  WHEN identity_matches
    AND status_before IN ['queued', 'running', 'paused_with_checkpoint', 'stop_requested']
  THEN [1]
  ELSE []
END |
  SET logical.status = 'paused_with_checkpoint',
    logical.bounded_status = 'paused_with_checkpoint',
    logical.pause_reason = 'manual',
    logical.manual_pause = true,
    logical.manual_pause_reason = $reason,
    logical.manual_paused_at = datetime(),
    logical.publication_intent = false,
    logical.recovery_authorized = false,
    logical.updated_at = datetime()
)
WITH logical,
  CASE
    WHEN NOT identity_matches THEN 'not_found'
    WHEN status_before IN ['queued', 'running', 'paused_with_checkpoint', 'stop_requested'] THEN 'updated'
    ELSE 'conflict'
  END AS outcome
OPTIONAL MATCH (slot:BoundedIngestionGlobalSlot {
  environment: logical.environment,
  reset_generation: logical.reset_generation,
  slot_index: logical.global_slot_index,
  owner_logical_run_id: logical.logical_run_id
})
OPTIONAL MATCH (logical)-[active:ACTIVE_ATTEMPT]->(attempt:IngestRun)
FOREACH (_ IN CASE WHEN outcome = 'updated' AND slot IS NOT NULL THEN [1] ELSE [] END |
  SET slot.owner_logical_run_id = NULL,
    slot.owner_attempt_generation = NULL,
    slot.owner_lease_token = NULL,
    slot.lease_expires_at = NULL,
    slot.updated_at = datetime()
)
FOREACH (_ IN CASE WHEN outcome = 'updated' AND active IS NOT NULL THEN [1] ELSE [] END |
  SET attempt.status = 'paused_with_checkpoint',
    attempt.finished_at = datetime()
  DELETE active
)
WITH logical, outcome
OPTIONAL MATCH (checkpoint:IngestionCheckpoint {
  control_instance_id: logical.control_instance_id,
  logical_run_id: logical.logical_run_id,
  generation: logical.active_generation,
  phase: logical.current_phase
})
OPTIONAL MATCH (retry:BoundedIngestionRetry {
  logical_run_id: logical.logical_run_id,
  status: 'pending'
})
"""
    + _CONTROL_STATUS_PROJECTION
)

RESUME_BOUNDED_LOGICAL_RUN = (
    """
MATCH (logical:IngestionLogicalRun {logical_run_id: $logical_run_id})
WHERE logical.bounded_scope_key IS NOT NULL
WITH logical,
  logical.source_key = $source_key
    AND logical.control_instance_id = $control_instance_id
    AND logical.reset_generation = $reset_generation AS identity_matches,
  coalesce(logical.bounded_status, logical.status) AS status_before,
  coalesce(logical.manual_pause, false) AS manual_pause_before,
  coalesce(logical.publication_intent, false) AS publication_intent_before
FOREACH (_ IN CASE
  WHEN identity_matches
    AND status_before = 'paused_with_checkpoint'
    AND (manual_pause_before OR publication_intent_before)
  THEN [1]
  ELSE []
END |
  SET logical.manual_pause = false,
    logical.pause_reason = NULL,
    logical.manual_release_requested_at = coalesce(
      logical.manual_release_requested_at,
      datetime()
    ),
    logical.publication_intent = true,
    logical.recovery_authorized = true,
    logical.publication_requested_at = coalesce(logical.publication_requested_at, datetime()),
    logical.updated_at = datetime()
)
WITH logical,
  CASE
    WHEN NOT identity_matches THEN 'not_found'
    WHEN status_before = 'paused_with_checkpoint'
      AND (manual_pause_before OR publication_intent_before) THEN 'updated'
    ELSE 'conflict'
  END AS outcome
OPTIONAL MATCH (checkpoint:IngestionCheckpoint {
  control_instance_id: logical.control_instance_id,
  logical_run_id: logical.logical_run_id,
  generation: logical.active_generation,
  phase: logical.current_phase
})
OPTIONAL MATCH (retry:BoundedIngestionRetry {
  logical_run_id: logical.logical_run_id,
  status: 'pending'
})
"""
    + _CONTROL_STATUS_PROJECTION
)
