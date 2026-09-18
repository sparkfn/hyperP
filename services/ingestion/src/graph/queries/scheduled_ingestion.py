"""Cypher transitions for durable scheduled-ingestion coordination."""

from __future__ import annotations

GET_ACTIVE_SCHEDULE_RESET = """
MATCH (reset:IngestionResetGeneration {environment: $environment, status: 'active'})
RETURN reset.generation AS generation
ORDER BY reset.generation DESC
LIMIT 1
"""

ENSURE_SCHEDULED_GROUP_WORKFLOW = """
MATCH (:IngestionResetGeneration {
  environment: $environment,
  generation: $reset_generation,
  status: 'active'
})
MERGE (workflow:BoundedIngestionScope:ScheduledIngestionWorkflow {scope_key: $scope_key})
ON CREATE SET workflow.workflow_id = randomUUID(),
  workflow.environment = $environment,
  workflow.reset_generation = $reset_generation,
  workflow.group_key = $group_key,
  workflow.control_instance_id = $control_instance_id,
  workflow.children_json = $children_json,
  workflow.child_count = $child_count,
  workflow.current_child_index = 0,
  workflow.completed_prefix = 0,
  workflow.workflow_status = 'queued',
  workflow.manual_pause = false,
  workflow.occurrence_id = $occurrence_id,
  workflow.occurrence_timezone = $timezone,
  workflow.occurrence_starts_at = datetime($starts_at),
  workflow.drain_starts_at = datetime($drain_starts_at),
  workflow.cutoff_at = datetime($cutoff_at),
  workflow.next_eligible_at = datetime($starts_at),
  workflow.created_at = datetime(),
  workflow.updated_at = datetime()
WITH workflow
WHERE workflow.environment = $environment
  AND workflow.reset_generation = $reset_generation
  AND workflow.group_key = $group_key
  AND workflow.control_instance_id = $control_instance_id
  AND workflow.children_json = $children_json
SET workflow.scheduler_lock_version = coalesce(workflow.scheduler_lock_version, 0) + 1,
  workflow.updated_at = datetime()
RETURN workflow.workflow_id AS workflow_id,
  workflow.workflow_status AS workflow_status,
  coalesce(workflow.manual_pause, false) AS manual_pause,
  workflow.current_child_index AS current_child_index,
  workflow.completed_prefix AS completed_prefix,
  workflow.pending_publication_id AS pending_publication_id,
  workflow.occurrence_id AS occurrence_id,
  toString(workflow.next_eligible_at) AS next_eligible_at
"""

REBIND_SCHEDULED_GROUP_OCCURRENCE = """
MATCH (workflow:ScheduledIngestionWorkflow {
  scope_key: $scope_key,
  environment: $environment,
  reset_generation: $reset_generation
})
SET workflow.scheduler_lock_version = coalesce(workflow.scheduler_lock_version, 0) + 1
WITH workflow, coalesce(workflow.workflow_status, 'queued') AS prior_status
WHERE coalesce(workflow.manual_pause, false) = false
  AND workflow.occurrence_id <> $occurrence_id
  AND datetime($starts_at) >= coalesce(workflow.next_eligible_at, datetime($starts_at))
  AND datetime($now) >= datetime($starts_at)
  AND datetime($now) < datetime($drain_starts_at)
SET workflow.occurrence_id = $occurrence_id,
  workflow.occurrence_timezone = $timezone,
  workflow.occurrence_starts_at = datetime($starts_at),
  workflow.drain_starts_at = datetime($drain_starts_at),
  workflow.cutoff_at = datetime($cutoff_at),
  workflow.next_eligible_at = datetime($next_eligible_at),
  workflow.pending_publication_id = NULL,
  workflow.pending_publication_state = NULL,
  workflow.current_source_key = NULL,
  workflow.current_entity_key = NULL,
  workflow.current_mode = NULL,
  workflow.current_child_index = CASE
    WHEN prior_status = 'completed' THEN 0 ELSE workflow.current_child_index END,
  workflow.completed_prefix = CASE
    WHEN prior_status = 'completed' THEN 0 ELSE workflow.completed_prefix END,
  workflow.workflow_status = 'paused_with_checkpoint',
  workflow.pause_reason = 'schedule_window_closed',
  workflow.shared_reserved_records = 0,
  workflow.shared_reserved_source_requests = 0,
  workflow.shared_reserved_pages = 0,
  workflow.shared_reserved_bytes = 0,
  workflow.shared_reserved_extraction_calls = 0,
  workflow.updated_at = datetime()
RETURN workflow.workflow_id AS workflow_id
"""

ENSURE_SCHEDULED_OCCURRENCE_AUTHORITY = """
MATCH (:IngestionResetGeneration {
  environment: $environment,
  generation: $reset_generation,
  status: 'active'
})
MERGE (authority:BoundedIngestionScope:ScheduledOccurrenceAuthority {
  scope_key: $occurrence_authority_key
})
ON CREATE SET authority.environment = $environment,
  authority.reset_generation = $reset_generation,
  authority.occurrence_id = $occurrence_id,
  authority.max_records = $max_records,
  authority.max_source_requests = $max_source_requests,
  authority.max_pages = $max_pages,
  authority.max_bytes = $max_bytes,
  authority.max_extraction_calls = $max_extraction_calls,
  authority.reserved_records = 0,
  authority.reserved_source_requests = 0,
  authority.reserved_pages = 0,
  authority.reserved_bytes = 0,
  authority.reserved_extraction_calls = 0,
  authority.scheduler_turn = 0,
  authority.created_at = datetime()
WITH authority
WHERE authority.environment = $environment
  AND authority.reset_generation = $reset_generation
  AND authority.occurrence_id = $occurrence_id
  AND authority.max_records = $max_records
  AND authority.max_source_requests = $max_source_requests
  AND authority.max_pages = $max_pages
  AND authority.max_bytes = $max_bytes
  AND authority.max_extraction_calls = $max_extraction_calls
SET authority.lock_version = coalesce(authority.lock_version, 0) + 1,
  authority.updated_at = datetime()
RETURN authority.scope_key AS occurrence_authority_key
"""

# A group's current child is deliberately not arbitrated against other
# participants: its weekly slot is unique, so a lost turn would cost a whole
# occurrence. Maintenance claims keep the persisted round-robin ordering because
# they retry in the next hour bucket. Reservations still come from one shared
# occurrence authority, so no participant class can exceed the occurrence ceiling.
CLAIM_SCHEDULED_CHILD_PUBLICATION = """
MATCH (workflow:ScheduledIngestionWorkflow {
  scope_key: $scope_key,
  environment: $environment,
  reset_generation: $reset_generation,
  occurrence_id: $occurrence_id
})
MATCH (authority:ScheduledOccurrenceAuthority {scope_key: $occurrence_authority_key})
MERGE (participant:BoundedIngestionScope:ScheduledOccurrenceParticipant {
  scope_key: $participant_key
})
ON CREATE SET participant.authority_key = $occurrence_authority_key,
  participant.participant_key = $participant_key,
  participant.created_at = datetime()
SET workflow.scheduler_lock_version = coalesce(workflow.scheduler_lock_version, 0) + 1
WITH workflow, authority, participant,
  workflow.pending_publication_id = $publication_id AS retrying_intent,
  coalesce(authority.scheduler_turn, 0) + 1 AS next_turn
WHERE coalesce(workflow.manual_pause, false) = false
  AND workflow.current_child_index = $child_index
  AND workflow.current_child_index < workflow.child_count
  AND datetime($now) >= workflow.occurrence_starts_at
  AND datetime($now) < workflow.drain_starts_at
  AND (workflow.pending_publication_id IS NULL
    OR workflow.pending_publication_id = $publication_id)
  AND coalesce(workflow.pending_publication_state, '-') <> 'accepted'
  AND (retrying_intent OR (
    coalesce(authority.reserved_records, 0) + $reserved_records <= authority.max_records
    AND coalesce(authority.reserved_source_requests, 0) + $reserved_source_requests
      <= authority.max_source_requests
    AND coalesce(authority.reserved_pages, 0) + $reserved_pages <= authority.max_pages
    AND coalesce(authority.reserved_bytes, 0) + $reserved_bytes <= authority.max_bytes
    AND coalesce(authority.reserved_extraction_calls, 0) + $reserved_extraction_calls
      <= authority.max_extraction_calls
  ))
SET authority.scheduler_turn = CASE
    WHEN retrying_intent THEN coalesce(authority.scheduler_turn, 0)
    ELSE next_turn END,
  authority.last_served_participant_key = participant.participant_key,
  authority.reserved_records = coalesce(authority.reserved_records, 0)
    + CASE WHEN retrying_intent THEN 0 ELSE $reserved_records END,
  authority.reserved_source_requests = coalesce(authority.reserved_source_requests, 0)
    + CASE WHEN retrying_intent THEN 0 ELSE $reserved_source_requests END,
  authority.reserved_pages = coalesce(authority.reserved_pages, 0)
    + CASE WHEN retrying_intent THEN 0 ELSE $reserved_pages END,
  authority.reserved_bytes = coalesce(authority.reserved_bytes, 0)
    + CASE WHEN retrying_intent THEN 0 ELSE $reserved_bytes END,
  authority.reserved_extraction_calls = coalesce(authority.reserved_extraction_calls, 0)
    + CASE WHEN retrying_intent THEN 0 ELSE $reserved_extraction_calls END,
  authority.updated_at = datetime(),
  participant.ready = false,
  participant.last_served_turn = CASE
    WHEN retrying_intent THEN coalesce(authority.scheduler_turn, 0)
    ELSE next_turn END,
  workflow.pending_publication_id = $publication_id,
  workflow.pending_publication_state = 'publishing',
  workflow.current_source_key = $source_key,
  workflow.current_entity_key = $entity_key,
  workflow.current_mode = $mode,
  workflow.current_stream_key = $stream_key,
  workflow.current_configuration_fingerprint = $configuration_fingerprint,
  workflow.current_source_window_fingerprint = $source_window_fingerprint,
  workflow.current_child_key = $child_key,
  workflow.shared_reserved_records = $reserved_records,
  workflow.shared_reserved_source_requests = $reserved_source_requests,
  workflow.shared_reserved_pages = $reserved_pages,
  workflow.shared_reserved_bytes = $reserved_bytes,
  workflow.shared_reserved_extraction_calls = $reserved_extraction_calls,
  workflow.workflow_status = 'publishing',
  workflow.pause_reason = NULL,
  workflow.updated_at = datetime()
RETURN workflow.workflow_id AS workflow_id,
  workflow.pending_publication_id AS publication_id,
  workflow.pending_publication_state AS publication_state
"""

CONFIRM_SCHEDULED_CHILD_PUBLICATION = """
MATCH (workflow:ScheduledIngestionWorkflow {
  scope_key: $scope_key,
  pending_publication_id: $publication_id
})
SET workflow.pending_publication_state = 'accepted',
  workflow.pending_publication_accepted_at = datetime(),
  workflow.updated_at = datetime()
RETURN workflow.workflow_id AS workflow_id
"""

# Completion authority is #430's bounded run, matched by the full scope it was
# admitted for (source, entity, control instance, configuration fingerprint and
# source-window fingerprint). The scheduler's occurrence is deliberately not part
# of the match: #430 never rewrites the occurrence of an already-completed run, so
# an occurrence-keyed match would make a lost completion callback invisible
# forever and stall the group. A different source window is a different child.
RECONCILE_SCHEDULED_CHILD_COMPLETION = """
MATCH (workflow:ScheduledIngestionWorkflow {
  scope_key: $scope_key,
  environment: $environment,
  reset_generation: $reset_generation
})
OPTIONAL MATCH (run:IngestionLogicalRun {
  environment: $environment,
  reset_generation: $reset_generation,
  source_key: workflow.current_source_key,
  control_instance_id: workflow.control_instance_id,
  configuration_fingerprint: workflow.current_configuration_fingerprint,
  source_window_fingerprint: workflow.current_source_window_fingerprint,
  bounded_status: 'completed'
})
WHERE coalesce(run.entity_key, '') = coalesce(workflow.current_entity_key, '')
WITH workflow, count(run) > 0 AS completed,
  workflow.current_child_index + 1 AS next_child_index
FOREACH (_ IN CASE WHEN completed THEN [1] ELSE [] END |
  SET workflow.current_child_index = next_child_index,
    workflow.completed_prefix = workflow.completed_prefix + 1,
    workflow.pending_publication_id = NULL,
    workflow.pending_publication_state = NULL,
    workflow.current_source_key = NULL,
    workflow.current_entity_key = NULL,
    workflow.current_mode = NULL,
    workflow.current_configuration_fingerprint = NULL,
    workflow.current_source_window_fingerprint = NULL,
    workflow.current_child_key = NULL,
    workflow.workflow_status = CASE
      WHEN next_child_index >= workflow.child_count THEN 'completed'
      ELSE 'queued' END,
    workflow.pause_reason = NULL,
    workflow.updated_at = datetime()
)
RETURN workflow.workflow_id AS workflow_id,
  completed AS completed,
  workflow.current_child_index AS current_child_index,
  workflow.workflow_status AS workflow_status
"""

BLOCK_SCHEDULED_GROUP_WORKFLOW = """
MATCH (workflow:ScheduledIngestionWorkflow {
  scope_key: $scope_key,
  environment: $environment,
  reset_generation: $reset_generation
})
SET workflow.workflow_status = 'blocked',
  workflow.pause_reason = $reason,
  workflow.next_eligible_at = datetime($next_eligible_at),
  workflow.updated_at = datetime()
RETURN workflow.workflow_id AS workflow_id
"""

CLAIM_SCHEDULED_MAINTENANCE = """
MATCH (:IngestionResetGeneration {
  environment: $environment,
  generation: $reset_generation,
  status: 'active'
})
MATCH (authority:ScheduledOccurrenceAuthority {scope_key: $occurrence_authority_key})
MERGE (obligation:BoundedIngestionScope:ScheduledMaintenanceObligation {scope_key: $scope_key})
ON CREATE SET obligation.obligation_id = $obligation_id,
  obligation.environment = $environment,
  obligation.reset_generation = $reset_generation,
  obligation.kind = $kind,
  obligation.phase = $phase,
  obligation.occurrence_id = $occurrence_id,
  obligation.occurrence_json = $occurrence_json,
  obligation.bucket = $bucket,
  obligation.authority_key = $occurrence_authority_key,
  obligation.publication_state = 'publishing',
  obligation.created_at = datetime()
MERGE (participant:BoundedIngestionScope:ScheduledOccurrenceParticipant {
  scope_key: $participant_key
})
ON CREATE SET participant.authority_key = $occurrence_authority_key,
  participant.participant_key = $participant_key,
  participant.created_at = datetime()
WITH obligation, authority, participant,
  coalesce(obligation.reserved_charged, false) AS already_charged
WHERE obligation.environment = $environment
  AND obligation.reset_generation = $reset_generation
  AND obligation.occurrence_id = $occurrence_id
  AND obligation.publication_state <> 'accepted'
  AND (already_charged OR (
    coalesce(authority.reserved_records, 0) + $reserved_records <= authority.max_records
    AND coalesce(authority.reserved_source_requests, 0) + $reserved_source_requests
      <= authority.max_source_requests
    AND coalesce(authority.reserved_pages, 0) + $reserved_pages <= authority.max_pages
    AND coalesce(authority.reserved_bytes, 0) + $reserved_bytes <= authority.max_bytes
    AND coalesce(authority.reserved_extraction_calls, 0) + $reserved_extraction_calls
      <= authority.max_extraction_calls
  ))
SET authority.lock_version = coalesce(authority.lock_version, 0) + 1,
  participant.ready = true,
  participant.updated_at = datetime()
WITH obligation, authority, participant, already_charged
OPTIONAL MATCH (candidate:ScheduledOccurrenceParticipant {authority_key: $occurrence_authority_key})
WHERE candidate.ready = true
WITH obligation, authority, participant, already_charged,
  min(candidate.participant_key) AS first_ready_key,
  min(CASE
    WHEN candidate.participant_key > coalesce(authority.last_served_participant_key, '')
    THEN candidate.participant_key ELSE NULL END) AS next_ready_key
WITH obligation, authority, participant, already_charged,
  CASE WHEN already_charged THEN participant.participant_key
  ELSE coalesce(next_ready_key, first_ready_key) END AS selected_key
WHERE selected_key = participant.participant_key
SET obligation.publication_state = 'publishing',
  obligation.reserved_records = $reserved_records,
  obligation.reserved_source_requests = $reserved_source_requests,
  obligation.reserved_pages = $reserved_pages,
  obligation.reserved_bytes = $reserved_bytes,
  obligation.reserved_extraction_calls = $reserved_extraction_calls,
  obligation.reserved_charged = true,
  obligation.updated_at = datetime(),
  authority.scheduler_turn = CASE
    WHEN already_charged THEN authority.scheduler_turn
    ELSE coalesce(authority.scheduler_turn, 0) + 1 END,
  authority.last_served_participant_key = CASE
    WHEN already_charged THEN authority.last_served_participant_key
    ELSE participant.participant_key END,
  authority.reserved_records = coalesce(authority.reserved_records, 0)
    + CASE WHEN already_charged THEN 0 ELSE $reserved_records END,
  authority.reserved_source_requests = coalesce(authority.reserved_source_requests, 0)
    + CASE WHEN already_charged THEN 0 ELSE $reserved_source_requests END,
  authority.reserved_pages = coalesce(authority.reserved_pages, 0)
    + CASE WHEN already_charged THEN 0 ELSE $reserved_pages END,
  authority.reserved_bytes = coalesce(authority.reserved_bytes, 0)
    + CASE WHEN already_charged THEN 0 ELSE $reserved_bytes END,
  authority.reserved_extraction_calls = coalesce(authority.reserved_extraction_calls, 0)
    + CASE WHEN already_charged THEN 0 ELSE $reserved_extraction_calls END,
  authority.updated_at = datetime(),
  participant.ready = false,
  participant.last_served_turn = authority.scheduler_turn
RETURN obligation.obligation_id AS obligation_id,
  obligation.publication_state AS publication_state
"""

CONFIRM_SCHEDULED_MAINTENANCE = """
MATCH (obligation:ScheduledMaintenanceObligation {scope_key: $scope_key})
SET obligation.publication_state = 'accepted',
  obligation.accepted_at = datetime(),
  obligation.updated_at = datetime()
RETURN obligation.obligation_id AS obligation_id
"""
