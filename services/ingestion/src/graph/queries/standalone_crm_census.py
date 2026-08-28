"""Parameterized Cypher for the standalone Bitrix CRM census control plane."""

CREATE_STANDALONE_CRM_CENSUS_SCHEMA: tuple[str, ...] = (
    """CREATE CONSTRAINT standalone_crm_census_id_unique IF NOT EXISTS
FOR (census:StandaloneCrmCensus)
REQUIRE census.census_id IS UNIQUE""",
    """CREATE CONSTRAINT standalone_crm_census_occurrence_unique IF NOT EXISTS
FOR (census:StandaloneCrmCensus)
REQUIRE (census.source_key, census.source_instance_id, census.control_instance_id,
         census.census_kind, census.occurrence_key) IS UNIQUE""",
    """CREATE CONSTRAINT standalone_crm_census_attempt_unique IF NOT EXISTS
FOR (attempt:StandaloneCrmCensusAttempt)
REQUIRE (attempt.census_id, attempt.generation) IS UNIQUE""",
    """CREATE CONSTRAINT standalone_crm_census_unit_unique IF NOT EXISTS
FOR (unit:StandaloneCrmCensusStream)
REQUIRE (unit.census_id, unit.unit_kind) IS UNIQUE""",
    """CREATE CONSTRAINT standalone_crm_census_checkpoint_unique IF NOT EXISTS
FOR (checkpoint:StandaloneCrmCensusCheckpoint)
REQUIRE (checkpoint.census_id, checkpoint.unit_kind) IS UNIQUE""",
    """CREATE CONSTRAINT standalone_crm_census_publication_unique IF NOT EXISTS
FOR (publication:StandaloneCrmChildPublication)
REQUIRE (publication.census_id, publication.generation, publication.unit_kind,
         publication.publication_sequence) IS UNIQUE""",
    """CREATE CONSTRAINT standalone_crm_census_call_intent_unique IF NOT EXISTS
FOR (reservation:StandaloneCrmHttpCallReservation)
REQUIRE (reservation.census_id, reservation.intent_id) IS UNIQUE""",
    """CREATE INDEX standalone_crm_census_active_lookup IF NOT EXISTS
FOR (census:StandaloneCrmCensus)
ON (census.source_key, census.source_instance_id, census.control_instance_id,
    census.census_kind, census.state)""",
    """CREATE INDEX standalone_crm_census_paused_lookup IF NOT EXISTS
FOR (census:StandaloneCrmCensus)
ON (census.state, census.next_action_after)""",
    """CREATE INDEX standalone_crm_attempt_lease_lookup IF NOT EXISTS
FOR (attempt:StandaloneCrmCensusAttempt)
ON (attempt.state, attempt.lease_until)""",
    """CREATE INDEX standalone_crm_publication_recovery_lookup IF NOT EXISTS
FOR (publication:StandaloneCrmChildPublication)
ON (publication.state, publication.census_id)""",
    """CREATE INDEX standalone_crm_reservation_census_lookup IF NOT EXISTS
FOR (reservation:StandaloneCrmHttpCallReservation)
ON (reservation.census_id, reservation.state)""",
)
ADMIT_STANDALONE_CRM_CENSUS = """
MATCH (instance:BitrixSourceInstance {
    source_key: $source_key,
    source_instance_id: $source_instance_id,
    status: 'active'
})
WITH instance, count(instance) AS instance_count
WHERE instance_count = 1
OPTIONAL MATCH (existing:StandaloneCrmCensus {
    source_key: $source_key,
    source_instance_id: $source_instance_id,
    control_instance_id: $control_instance_id,
    census_kind: $census_kind,
    occurrence_key: $occurrence_key
})
WITH instance, existing,
     existing IS NOT NULL AND existing.fingerprint <> $fingerprint AS fingerprint_conflict
OPTIONAL MATCH (active:StandaloneCrmCensus {
    source_key: $source_key,
    source_instance_id: $source_instance_id,
    control_instance_id: $control_instance_id,
    census_kind: $census_kind
})
WHERE active.state IN ['allocated', 'freezing', 'frozen', 'publishing', 'running',
                       'pause_requested', 'paused_with_checkpoint', 'continuing',
                       'cancel_requested', 'recovering']
  AND existing IS NULL
WITH instance, existing, fingerprint_conflict, count(active) AS active_count
WITH *
WHERE fingerprint_conflict = false AND active_count = 0
MERGE (census:StandaloneCrmCensus {
    source_key: $source_key,
    source_instance_id: $source_instance_id,
    control_instance_id: $control_instance_id,
    census_kind: $census_kind,
    occurrence_key: $occurrence_key
})
ON CREATE SET
    census.census_id = $census_id,
    census.occurrence_deadline = $occurrence_deadline,
    census.occurrence_calls = $occurrence_calls,
    census.occurrence_rows = $occurrence_rows,
    census.attempt_calls = $attempt_calls,
    census.attempt_rows = $attempt_rows,
    census.max_attempts = $max_attempts,
    census.attempt_runtime_seconds = $attempt_runtime_seconds,
    census.fingerprint = $fingerprint,
    census.request_json = $request_json,
    census.budget_json = $budget_json,
    census.heads_json = $heads_json,
    census.state = 'allocated',
    census.current_generation = 0,
    census.attempts_used = 0,
    census.calls_used = 0,
    census.rows_processed = 0,
    census.created_at = datetime(),
    census.updated_at = datetime()
SET census.updated_at = datetime()
RETURN census.census_id AS census_id,
       census.fingerprint AS fingerprint,
       census.state AS state,
       census.current_generation AS generation,
       existing IS NULL AS created,
       fingerprint_conflict,
       active_count > 0 AS active_conflict
"""

START_CENSUS_FREEZING = """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint})
WHERE census.state = 'allocated'
SET census.state = 'freezing', census.updated_at = datetime()
RETURN census.census_id AS census_id
"""

GET_STANDALONE_CRM_CENSUS_BY_OCCURRENCE = """
MATCH (census:StandaloneCrmCensus {
    source_key: $source_key,
    source_instance_id: $source_instance_id,
    control_instance_id: $control_instance_id,
    census_kind: $census_kind,
    occurrence_key: $occurrence_key
})
RETURN census.census_id AS census_id,
       census.fingerprint AS fingerprint,
       census.state AS state
"""

GET_CENSUS_STATUS = """
MATCH (census:StandaloneCrmCensus {census_id: $census_id})
OPTIONAL MATCH (census)-[:HAS_ATTEMPT]->(attempt:StandaloneCrmCensusAttempt)
RETURN census.census_id AS census_id,
       census.fingerprint AS fingerprint,
       census.state AS state,
       census.census_kind AS census_kind,
       census.current_generation AS generation,
       census.calls_used AS calls_used,
       census.rows_processed AS rows_processed,
       attempt.generation AS attempt_generation,
       attempt.state AS attempt_state
"""

CLAIM_CENSUS_ATTEMPT = """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint})
WHERE census.state IN ['allocated', 'freezing', 'frozen', 'publishing', 'running',
                       'paused_with_checkpoint', 'continuing', 'recovering']
  AND datetime() < census.occurrence_deadline
  AND census.calls_used < census.occurrence_calls
  AND census.attempts_used < census.max_attempts
  AND census.cancellation_requested_at IS NULL
  AND NOT EXISTS {
      MATCH (census)-[:HAS_ATTEMPT]->(active:StandaloneCrmCensusAttempt {state: 'running'})
  }
CREATE (attempt:StandaloneCrmCensusAttempt {
    census_id: census.census_id,
    generation: census.current_generation + 1,
    state: 'running',
    fence_token: $fence_token,
    lease_until: $lease_until,
    attempt_deadline: $attempt_deadline,
    attempt_calls: census.attempt_calls,
    attempt_rows: census.attempt_rows,
    started_at: datetime(),
    calls_used: 0,
    rows_processed: 0
})
MERGE (census)-[:HAS_ATTEMPT]->(attempt)
WITH census, attempt
OPTIONAL MATCH (census)-[:HAS_ATTEMPT]->(prior:StandaloneCrmCensusAttempt)
WHERE prior.generation < census.current_generation
  AND prior.state IN ['running', 'paused_with_checkpoint']
SET prior.state = 'superseded',
    prior.ended_at = datetime()
SET census.state = CASE WHEN census.window_kind IS NULL THEN 'freezing' ELSE 'running' END,
    census.current_generation = attempt.generation,
    census.attempts_used = census.attempts_used + 1,
    census.updated_at = datetime()
RETURN attempt.generation AS generation, attempt.fence_token AS fence_token
"""

RESERVE_CENSUS_HTTP_CALL = """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint})
WHERE census.census_kind = 'source_sync'
MATCH (attempt:StandaloneCrmCensusAttempt {
    census_id: census.census_id,
    generation: census.current_generation,
    state: 'running'
})
WHERE attempt.fence_token = $fence_token
  AND datetime() < attempt.lease_until
  AND datetime() < census.occurrence_deadline
  AND datetime() < attempt.attempt_deadline
  AND census.cancellation_requested_at IS NULL
  AND census.calls_used < census.occurrence_calls
  AND attempt.calls_used < census.attempt_calls
OPTIONAL MATCH (existing:StandaloneCrmHttpCallReservation {
    census_id: census.census_id,
    intent_id: $intent_id
})
FOREACH (_ IN CASE WHEN existing IS NULL THEN [1] ELSE [] END |
    SET census.calls_used = census.calls_used + 1,
        attempt.calls_used = attempt.calls_used + 1
    CREATE (reservation:StandaloneCrmHttpCallReservation {
        census_id: census.census_id,
        generation: attempt.generation,
        intent_id: $intent_id,
        sequence: census.calls_used,
        call_kind: $call_kind,
        unit_kind: $unit_kind,
        frozen_upper_id: $frozen_upper_id,
        cursor: $cursor,
        retry_ordinal: $retry_ordinal,
        state: 'reserved',
        reserved_at: datetime(),
        deadline: $deadline
    })
)
RETURN existing IS NULL AS reserved,
       coalesce(existing.intent_id, $intent_id) AS intent_id
"""

RECORD_CENSUS_HTTP_OUTCOME = """
MATCH (reservation:StandaloneCrmHttpCallReservation {
    census_id: $census_id,
    intent_id: $intent_id
})
WHERE reservation.state = 'reserved'
SET reservation.state = $outcome,
    reservation.outcome_recorded_at = datetime(),
    reservation.outcome_detail = $outcome_detail
RETURN reservation.intent_id AS intent_id, reservation.state AS state
"""

COMMIT_SOURCE_WINDOW = """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint})
WHERE census.census_kind = 'source_sync'
  AND census.state = 'freezing'
  AND census.window_kind IS NULL
SET census.window_kind = 'source_bounds',
    census.bounds_json = $bounds_json,
    census.selected_kinds = $selected_kinds,
    census.state = 'frozen',
    census.frozen_at = datetime(),
    census.updated_at = datetime()
RETURN census.census_id AS census_id, census.state AS state
"""

COMMIT_NO_SOURCE_WINDOW = """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint})
WHERE census.census_kind IN ['mapping_prepare', 'mapping_rollback']
  AND census.state = 'freezing'
  AND census.window_kind IS NULL
SET census.window_kind = 'no_source_window',
    census.state = 'frozen',
    census.frozen_at = datetime(),
    census.updated_at = datetime()
RETURN census.census_id AS census_id, census.state AS state
"""

ALLOCATE_SOURCE_UNITS = """
UNWIND $units AS unit
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint})
WHERE census.state = 'frozen' AND census.window_kind = 'source_bounds'
  AND census.cancellation_requested_at IS NULL
WITH census, collect(unit.unit_kind) AS unit_kinds, collect(unit) AS units
WHERE unit_kinds = census.selected_kinds
UNWIND units AS unit
MERGE (child:StandaloneCrmCensusStream {
    census_id: census.census_id,
    unit_kind: unit.unit_kind
})
ON CREATE SET
    child.frozen_upper_id = unit.frozen_upper_id,
    child.revision_id = unit.revision_id,
    child.state = unit.state,
    child.fence_token = unit.fence_token,
    child.fence_generation = census.current_generation,
    child.fence_active = false,
    child.expected_rows = unit.expected_rows,
    child.processed_rows = 0,
    child.skipped_rows = 0,
    child.failed_rows = 0,
    child.checkpointed = false,
    child.created_at = datetime()
MERGE (checkpoint:StandaloneCrmCensusCheckpoint {
    census_id: child.census_id,
    unit_kind: child.unit_kind
})
ON CREATE SET
    checkpoint.last_id = 0,
    checkpoint.rows_processed = 0,
    checkpoint.binding_position = 0,
    checkpoint.checkpoint_version = 1
RETURN collect(child.unit_kind) AS unit_kinds
"""

RESERVE_CHILD_PUBLICATION = """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint})
WHERE census.state IN ['frozen', 'publishing', 'running'] AND datetime() < census.occurrence_deadline
  AND census.cancellation_requested_at IS NULL
MATCH (attempt:StandaloneCrmCensusAttempt {
    census_id: census.census_id,
    generation: census.current_generation,
    state: 'running'
})
WHERE attempt.fence_token = $fence_token
MATCH (child:StandaloneCrmCensusStream {
    census_id: census.census_id,
    unit_kind: $unit_kind
})
WHERE child.frozen_upper_id > 0 AND child.state IN ['pending_publication', 'publishing']
MERGE (publication:StandaloneCrmChildPublication {
    census_id: census.census_id,
    generation: attempt.generation,
    unit_kind: child.unit_kind,
    publication_sequence: $publication_sequence
})
ON CREATE SET
    publication.task_name = $task_name,
    publication.task_id = $task_id,
    publication.queue = $queue,
    publication.payload_version = $payload_version,
    publication.payload_digest = $payload_digest,
    publication.payload_json = $payload_json,
    publication.state = 'publishing',
    publication.created_at = datetime()
SET census.state = 'publishing', census.updated_at = datetime()
WITH census, attempt, child, publication
RETURN publication.task_id AS task_id,
       publication.payload_json AS payload_json,
       publication.payload_digest = $payload_digest AS payload_matches,
       publication.state AS state
"""

CONFIRM_CHILD_PUBLICATION = """
MATCH (publication:StandaloneCrmChildPublication {
    census_id: $census_id,
    task_id: $task_id,
    state: 'publishing'
})
MATCH (child:StandaloneCrmCensusStream {
    census_id: $census_id,
    unit_kind: publication.unit_kind
})
SET publication.state = 'confirmed',
    publication.published_at = datetime(),
    child.state = 'queued',
    child.fence_active = true,
    child.fence_generation = publication.generation
RETURN publication.task_id AS task_id
"""

CLAIM_CENSUS_CHILD = """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint})
WHERE census.state IN ['running', 'publishing'] AND datetime() < census.occurrence_deadline
  AND census.cancellation_requested_at IS NULL
MATCH (child:StandaloneCrmCensusStream {census_id: census.census_id, unit_kind: $unit_kind})
WHERE child.fence_active = true
  AND child.fence_generation = census.current_generation
  AND child.fence_token = $fence_token
  AND child.state IN ['queued', 'running', 'paused']
SET child.state = 'running', child.fence_active = true
SET census.state = 'running', census.updated_at = datetime()
RETURN child.frozen_upper_id AS frozen_upper_id, child.revision_id AS revision_id
"""

ADVANCE_CENSUS_CHECKPOINT = """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint})
MATCH (attempt:StandaloneCrmCensusAttempt {
    census_id: census.census_id,
    generation: census.current_generation,
    state: 'running'
})
WHERE attempt.fence_token = $fence_token AND datetime() < attempt.lease_until
MATCH (child:StandaloneCrmCensusStream {
    census_id: census.census_id,
    unit_kind: $unit_kind
})
WHERE child.fence_active = true
  AND child.fence_generation = census.current_generation
  AND child.fence_token = $fence_token
  AND census.cancellation_requested_at IS NULL
  AND $last_id >= 0
  AND $binding_position >= 0
  AND $last_id <= child.frozen_upper_id
  AND $rows_processed <= child.expected_rows
MERGE (checkpoint:StandaloneCrmCensusCheckpoint {
    census_id: census.census_id,
    unit_kind: $unit_kind
})
ON CREATE SET
    checkpoint.last_id = 0,
    checkpoint.rows_processed = 0,
    checkpoint.binding_position = 0,
    checkpoint.checkpoint_version = 1
WITH census, attempt, child, checkpoint,
     checkpoint.last_id AS prior_last,
     checkpoint.rows_processed AS prior_rows,
     checkpoint.binding_position AS prior_binding
WHERE $last_id >= prior_last
  AND ($last_id > prior_last OR $binding_position >= prior_binding)
  AND $rows_processed >= prior_rows
  AND census.rows_processed + ($rows_processed - prior_rows) <= census.occurrence_rows
  AND attempt.rows_processed + ($rows_processed - prior_rows) <= census.attempt_rows
SET checkpoint.last_id = $last_id,
    checkpoint.rows_processed = $rows_processed,
    checkpoint.binding_position = $binding_position,
    checkpoint.updated_at = datetime(),
    census.rows_processed = census.rows_processed + ($rows_processed - prior_rows),
    attempt.rows_processed = attempt.rows_processed + ($rows_processed - prior_rows),
    child.processed_rows = $rows_processed,
    child.checkpointed = true
RETURN checkpoint.last_id AS last_id, checkpoint.rows_processed AS rows_processed
"""

REQUEST_CENSUS_PAUSE = """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint})
WHERE census.state IN ['running', 'publishing', 'frozen', 'freezing']
OPTIONAL MATCH (attempt:StandaloneCrmCensusAttempt {
    census_id: census.census_id,
    generation: census.current_generation,
    state: 'running'
})
SET census.state = 'paused_with_checkpoint',
    census.pause_reason = $reason,
    attempt.state = CASE WHEN attempt IS NULL THEN attempt.state ELSE 'paused_with_checkpoint' END,
    attempt.ended_at = CASE WHEN attempt IS NULL THEN attempt.ended_at ELSE datetime() END,
    census.updated_at = datetime()
RETURN census.state AS state
"""

REQUEST_CENSUS_CANCELLATION = """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint})
WHERE census.state IN ['allocated', 'freezing', 'frozen', 'publishing', 'running',
                       'pause_requested', 'paused_with_checkpoint', 'continuing', 'recovering']
SET census.state = CASE WHEN census.window_kind IS NULL THEN 'freeze_failed' ELSE 'cancel_requested' END,
    census.cancellation_requested_at = datetime(),
    census.cancellation_actor = $actor,
    census.updated_at = datetime()
RETURN census.state AS state
"""

CONTINUE_CENSUS = """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint})
WHERE census.state = 'paused_with_checkpoint'
  AND census.cancellation_requested_at IS NULL
  AND datetime() < census.occurrence_deadline
  AND census.calls_used < census.occurrence_calls
  AND census.rows_processed < census.occurrence_rows
  AND census.attempts_used < census.max_attempts
SET census.state = 'recovering', census.updated_at = datetime()
RETURN census.census_id AS census_id, census.attempt_runtime_seconds AS attempt_runtime_seconds
"""

FINALIZE_CENSUS = """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint})
WHERE census.state IN ['running', 'publishing', 'cancel_requested', 'paused_with_checkpoint']
OPTIONAL MATCH (child:StandaloneCrmCensusStream {census_id: census.census_id})
OPTIONAL MATCH (publication:StandaloneCrmChildPublication {census_id: census.census_id})
OPTIONAL MATCH (attempt:StandaloneCrmCensusAttempt {
    census_id: census.census_id,
    generation: census.current_generation
})
WITH census,
     collect(child) AS child_records,
     collect(publication) AS publication_records,
     collect(DISTINCT attempt) AS attempt_records
WITH census, child_records, publication_records, attempt_records,
     reduce(total = 0, child IN child_records | total + coalesce(child.processed_rows, 0)) AS processed_rows,
     reduce(total = 0, child IN child_records | total + coalesce(child.skipped_rows, 0)) AS skipped_rows,
     reduce(total = 0, child IN child_records | total + coalesce(child.failed_rows, 0)) AS failed_rows,
     size([child IN child_records WHERE child.frozen_upper_id = 0 AND child.state = 'completed']) AS no_work_units,
     all(child IN child_records WHERE
         child.state IN ['completed', 'failed', 'cancelled', 'superseded'] OR
         ($allow_paused AND child.state = 'paused' AND child.checkpointed = true)) AS children_settled,
     all(publication IN publication_records WHERE
         publication.state IN ['confirmed', 'failed']) AS publications_settled,
     none(child IN child_records WHERE child.fence_active = true) AS fences_settled
WHERE children_settled AND publications_settled AND fences_settled
  AND (census.census_kind <> 'source_sync' OR size(child_records) = size(census.selected_kinds))
  AND ($terminal_state <> 'completed' OR all(child IN child_records WHERE child.state = 'completed'))
WITH census, child_records, publication_records, attempt_records,
     processed_rows, skipped_rows, failed_rows, no_work_units
OPTIONAL MATCH (terminal_attempt:StandaloneCrmCensusAttempt {
    census_id: census.census_id,
    generation: census.current_generation
})
WITH census, child_records, publication_records, terminal_attempt,
     processed_rows, skipped_rows, failed_rows, no_work_units
SET census.state = $terminal_state,
    census.terminal_reason = $reason,
    census.terminal_at = datetime(),
    census.terminal_total_calls = census.calls_used,
    census.terminal_rows_processed = processed_rows,
    census.terminal_rows_skipped = skipped_rows,
    census.terminal_rows_failed = failed_rows,
    census.terminal_no_work_units = no_work_units,
    terminal_attempt.state = CASE
        WHEN terminal_attempt IS NULL THEN terminal_attempt.state
        WHEN $terminal_state = 'completed' THEN 'completed'
        ELSE 'failed'
    END,
    terminal_attempt.ended_at = CASE WHEN terminal_attempt IS NULL THEN terminal_attempt.ended_at ELSE datetime() END,
    census.updated_at = datetime()
RETURN census.state AS state
"""

MARK_CHILD_TERMINAL = """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint})
MATCH (child:StandaloneCrmCensusStream {census_id: census.census_id, unit_kind: $unit_kind})
WHERE child.fence_active = true
  AND child.fence_generation = census.current_generation
  AND child.fence_token = $fence_token
SET child.state = $terminal_state,
    child.fence_active = false,
    child.terminal_reason = $reason,
    child.terminal_at = datetime()
RETURN child.unit_kind AS unit_kind, child.state AS state
"""

SUPERSEDE_STALE_ATTEMPT = """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint})
WHERE census.state IN ['running', 'publishing', 'paused_with_checkpoint']
MATCH (attempt:StandaloneCrmCensusAttempt {
    census_id: census.census_id,
    generation: census.current_generation,
    state: 'running'
})
WHERE datetime() >= attempt.lease_until
SET attempt.state = 'superseded',
    attempt.ended_at = datetime()
SET census.state = 'recovering', census.updated_at = datetime()
RETURN attempt.generation AS generation, attempt.fence_token AS fence_token
"""

LIST_UNRESOLVED_PUBLICATIONS = """
MATCH (publication:StandaloneCrmChildPublication {census_id: $census_id})
WHERE publication.state IN ['publishing', 'reserved']
RETURN publication.generation AS generation,
       publication.unit_kind AS unit_kind,
       publication.publication_sequence AS publication_sequence,
       publication.task_name AS task_name,
       publication.task_id AS task_id,
       publication.queue AS queue,
       publication.payload_version AS payload_version,
       publication.payload_digest AS payload_digest,
       publication.payload_json AS payload_json,
       publication.state AS state
ORDER BY publication.publication_sequence
"""
