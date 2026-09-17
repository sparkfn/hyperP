"""Cypher for redacted bounded logical-run status and operator controls."""

from __future__ import annotations

_STATUS_PROJECTION = """
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
  coalesce(checkpoint.phase, logical.current_phase) AS phase,
  toString(checkpoint.updated_at) AS checkpointed_at,
  coalesce(logical.retry_backlog, 0) AS retry_backlog,
  logical.failure_category AS failure_category
"""

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
"""
    + _STATUS_PROJECTION
)

PAUSE_BOUNDED_LOGICAL_RUN = """
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
    logical.updated_at = datetime()
)
WITH logical,
  CASE
    WHEN NOT identity_matches THEN 'not_found'
    WHEN status_before IN ['queued', 'running', 'paused_with_checkpoint', 'stop_requested'] THEN 'updated'
    ELSE 'conflict'
  END AS outcome
OPTIONAL MATCH (checkpoint:IngestionCheckpoint {
  control_instance_id: logical.control_instance_id,
  logical_run_id: logical.logical_run_id,
  generation: logical.active_generation,
  phase: logical.current_phase
})
""" + _STATUS_PROJECTION.replace("RETURN", "RETURN outcome AS outcome,")

RESUME_BOUNDED_LOGICAL_RUN = """
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
""" + _STATUS_PROJECTION.replace("RETURN", "RETURN outcome AS outcome,")
