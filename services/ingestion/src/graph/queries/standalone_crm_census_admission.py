"""Split Cypher primitives for standalone CRM census control."""

from __future__ import annotations

_FRESHNESS = """
MATCH (ready273:DataMigration {migration_key: 'standalone_crm_census_control_v1'})
WHERE ready273.completed_at IS NOT NULL
MATCH (ready272:DataMigration {migration_key: 'bitrix_control_instance_v1'})
WHERE ready272.completed_at IS NOT NULL
MATCH (source:BitrixSourceInstance {source_key: 'bitrix_chat', source_instance_id: $source_instance_id, status: 'active'})
MATCH (control:BitrixSourceInstance {source_key: 'bitrix_chat', source_instance_id: $control_instance_id, status: 'active'})
MATCH (source)-[:OWNS_BITRIX_CONTROL]->(:BitrixExecutionSourceBinding {
  source_key: 'bitrix_chat', source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id
})
"""

ADMIT_CENSUS = (
    _FRESHNESS
    + """
MERGE (scope:StandaloneCrmCensusScopeLock {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, census_kind: $census_kind
})
ON CREATE SET scope.created_at = datetime(), scope.updated_at = datetime()
WITH scope
OPTIONAL MATCH (existing:StandaloneCrmCensus {source_key: $source_key,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  census_kind: $census_kind, occurrence_key: $occurrence_key})
WITH scope, collect(existing) AS rows
WHERE size(rows) <= 1
CALL {
  WITH scope, rows
  WITH scope, rows WHERE size(rows) = 1 AND rows[0].fingerprint = $fingerprint
  RETURN rows[0] AS census, false AS created
  UNION
  WITH scope, rows
  WITH scope WHERE size(rows) = 0 AND scope.active_census_id IS NULL
  CREATE (census:StandaloneCrmCensus {
    census_id: $census_id, source_key: $source_key, source_instance_id: $source_instance_id,
    control_instance_id: $control_instance_id, census_kind: $census_kind,
    occurrence_key: $occurrence_key, fingerprint: $fingerprint, request_json: $request_json,
    budget_json: $budget_json, authority_json: $authority_json, authority_digest: $authority_digest,
    state: 'allocated', no_source_window: false, expected_unit_count: 0, call_count: 0, row_count: 0, attempt_count: 0,
    created_at: datetime(), updated_at: datetime()
  })
  SET scope.active_census_id = census.census_id, scope.updated_at = datetime()
  RETURN census, true AS created
}
RETURN census.census_id AS census_id, census.state AS state, census.fingerprint AS fingerprint,
       census.authority_digest AS authority_digest, created AS created
"""
)

CLAIM_ATTEMPT = (
    _FRESHNESS
    + """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint,
  authority_digest: $authority_digest, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id})
WHERE census.state IN ['allocated','freezing','frozen','publishing','running','recovering']
  AND census.cancel_requested_at IS NULL
  AND (census.occurrence_deadline_at IS NULL OR census.occurrence_deadline_at > datetime())
SET census.updated_at = census.updated_at
WITH census
OPTIONAL MATCH (same:StandaloneCrmCensusAttempt {census_id: census.census_id, task_id: $task_id})
WITH census, collect(same) AS redelivery
CALL {
  WITH census, redelivery
  UNWIND CASE WHEN size(redelivery) = 1 AND redelivery[0].state = 'running'
    AND redelivery[0].generation = census.current_generation
    AND redelivery[0].fence_token = census.fence_token
    THEN [redelivery[0]] ELSE [] END AS attempt
  RETURN attempt
  UNION
  WITH census, redelivery
  UNWIND CASE WHEN size(redelivery) = 0 THEN [1] ELSE [] END AS create_attempt
  OPTIONAL MATCH (current:StandaloneCrmCensusAttempt {census_id: census.census_id,
    generation: census.current_generation})
  WITH census, current
  WHERE census.attempt_count < $max_attempts
    AND (current IS NULL OR current.state IN ['paused_with_checkpoint','superseded','failed','completed'])
  CREATE (attempt:StandaloneCrmCensusAttempt {
    census_id: census.census_id, generation: coalesce(census.current_generation, 0) + 1,
    task_id: $task_id, state: 'running', fence_token: coalesce(census.fence_token, 0) + 1,
    lease_until: datetime() + duration({seconds: $lease_seconds}),
    deadline_at: datetime() + duration({seconds: $attempt_runtime_seconds}),
    occurrence_deadline_at: coalesce(census.occurrence_deadline_at,
      datetime() + duration({seconds: $occurrence_runtime_seconds})),
    call_count: 0, row_count: 0, created_at: datetime(), updated_at: datetime()
  })
  SET census.current_generation = attempt.generation, census.fence_token = attempt.fence_token,
      census.attempt_count = census.attempt_count + 1, census.state = 'running',
      census.occurrence_deadline_at = attempt.occurrence_deadline_at, census.updated_at = datetime()
  RETURN attempt
}
RETURN attempt.census_id AS census_id, attempt.generation AS generation, attempt.task_id AS task_id,
       attempt.state AS state, attempt.fence_token AS fence_token, toString(attempt.deadline_at) AS deadline_at,
       toString(attempt.occurrence_deadline_at) AS occurrence_deadline_at
"""
)

RECOVER_ATTEMPT = (
    _FRESHNESS
    + """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint,
  authority_digest: $authority_digest, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id})
MATCH (old:StandaloneCrmCensusAttempt {census_id: $census_id, generation: $generation,
  fence_token: $parent_fence_token, state: 'running'})
WHERE old.lease_until < datetime() AND census.current_generation = $generation
  AND census.fence_token = $parent_fence_token AND census.cancel_requested_at IS NULL
  AND NOT EXISTS {
    MATCH (:StandaloneCrmHttpCallReservation {census_id: $census_id,
      generation: $generation, outcome: 'reserved'})
  }
SET old.state = 'superseded', old.superseded_at = datetime(), old.updated_at = datetime(),
    census.state = 'recovering', census.updated_at = datetime()
WITH census
OPTIONAL MATCH (fence:StandaloneCrmUnitFence {census_id: census.census_id, generation: $generation,
  state: 'active'})
SET fence.state = 'superseded', fence.superseded_at = datetime(), fence.updated_at = datetime()
WITH census
OPTIONAL MATCH (unit:StandaloneCrmCensusUnit {census_id: census.census_id, generation: $generation,
  state: 'running'})
SET unit.state = 'paused', unit.updated_at = datetime()
RETURN census.census_id AS census_id
"""
)

RESERVE_HTTP_CALL = (
    _FRESHNESS
    + """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, authority_digest: $authority_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id})
MATCH (attempt:StandaloneCrmCensusAttempt {census_id: $census_id, generation: $generation,
  fence_token: $parent_fence_token, state: 'running'})
WHERE census.current_generation = $generation AND census.fence_token = $parent_fence_token
  AND census.census_kind = 'source_sync' AND census.state IN ['freezing','frozen','publishing','running']
  AND census.cancel_requested_at IS NULL AND attempt.lease_until >= datetime()
  AND attempt.deadline_at > datetime() AND attempt.occurrence_deadline_at > datetime()
SET census.updated_at = census.updated_at
WITH census, attempt
WHERE attempt.call_count < $max_calls_per_attempt AND census.call_count < $max_calls_per_occurrence
OPTIONAL MATCH (existing:StandaloneCrmHttpCallReservation {intent_id: $intent_id})
WITH census, attempt, collect(existing) AS existing_rows
WHERE size(existing_rows) = 0
CREATE (reservation:StandaloneCrmHttpCallReservation {
  intent_id: $intent_id, census_id: $census_id, generation: $generation, fence_token: $parent_fence_token,
  sequence: $sequence, call_kind: $call_kind, unit_kind: $unit_kind, retry_ordinal: $retry_ordinal,
  metadata_digest: $metadata_digest, cursor_id: $cursor_id, subject_id: $subject_id, upper_id: $upper_id,
  outcome: 'reserved', reserved_at: datetime(), updated_at: datetime()
})
SET census.call_count = census.call_count + 1, attempt.call_count = attempt.call_count + 1,
    census.updated_at = datetime(), attempt.updated_at = datetime()
RETURN reservation.intent_id AS intent_id
"""
)

RECORD_HTTP_OUTCOME = (
    _FRESHNESS
    + """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, authority_digest: $authority_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  current_generation: $generation, fence_token: $parent_fence_token})
MATCH (reservation:StandaloneCrmHttpCallReservation {intent_id: $intent_id, census_id: $census_id,
  generation: $generation, fence_token: $parent_fence_token, outcome: 'reserved'})
WHERE reservation.call_kind <> 'probe' OR $outcome <> 'succeeded' OR $numeric_result IS NOT NULL
SET reservation.outcome = $outcome, reservation.numeric_result = $numeric_result,
    reservation.result_digest = $result_digest, reservation.updated_at = datetime(), census.updated_at = datetime()
RETURN reservation.intent_id AS intent_id
"""
)


CLASSIFY_RESERVED_HTTP_CALL_UNKNOWN = (
    _FRESHNESS
    + """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint,
  authority_digest: $authority_digest, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, current_generation: $generation,
  fence_token: $parent_fence_token})
MATCH (reservation:StandaloneCrmHttpCallReservation {intent_id: $intent_id, census_id: $census_id,
  generation: $generation, fence_token: $parent_fence_token, outcome: 'reserved'})
SET reservation.outcome = 'unknown', reservation.unknown_at = datetime(),
  reservation.updated_at = datetime(), census.updated_at = datetime()
RETURN reservation.intent_id AS intent_id
"""
)


CLASSIFY_CURRENT_RESERVED_HTTP_CALL_UNKNOWN = """
MATCH (ready273:DataMigration {migration_key: 'standalone_crm_census_control_v1'})
WHERE ready273.completed_at IS NOT NULL
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint,
  authority_digest: $authority_digest, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id})
MATCH (reservation:StandaloneCrmHttpCallReservation {intent_id: $intent_id,
  census_id: $census_id, outcome: 'reserved'})
SET reservation.outcome = 'unknown', reservation.unknown_at = datetime(),
  reservation.updated_at = datetime(), census.updated_at = datetime()
RETURN reservation.intent_id AS intent_id
"""
