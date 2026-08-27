"""Publication outbox and terminal derivation Cypher for standalone CRM census."""

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

_SETTLEMENT = """
MATCH (ready273:DataMigration {migration_key: 'standalone_crm_census_control_v1'})
WHERE ready273.completed_at IS NOT NULL
MATCH (ready272:DataMigration {migration_key: 'bitrix_control_instance_v1'})
WHERE ready272.completed_at IS NOT NULL
"""

_PARENT = """current_generation: $generation, fence_token: $parent_fence_token"""

RESERVE_PUBLICATION = (
    _FRESHNESS
    + """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, authority_digest: $authority_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  current_generation: $generation, fence_token: $parent_fence_token})
MATCH (attempt:StandaloneCrmCensusAttempt {census_id: $census_id, generation: $generation,
  fence_token: $parent_fence_token, state: 'running'})
MATCH (unit:StandaloneCrmCensusUnit {census_id: $census_id, unit_kind: $unit_kind})
WHERE census.state IN ['frozen','publishing','running'] AND census.cancel_requested_at IS NULL
  AND census.fatal_reason IS NULL AND attempt.lease_until >= datetime() AND attempt.deadline_at > datetime()
  AND attempt.occurrence_deadline_at > datetime()
SET census.updated_at = census.updated_at
WITH census, attempt, unit
OPTIONAL MATCH (existing:StandaloneCrmChildPublication {census_id: $census_id, generation: $generation,
  unit_kind: $unit_kind, sequence: $sequence})
WITH census, unit, attempt, collect(existing) AS existing_rows
OPTIONAL MATCH (by_id:StandaloneCrmChildPublication {publication_id: $publication_id})
WITH census, unit, attempt, existing_rows, collect(by_id) AS id_rows
WHERE size(existing_rows) <= 1 AND size(id_rows) <= 1
WITH census, unit, attempt, existing_rows, id_rows,
  size(id_rows) = 1 AND (size(existing_rows) = 0 OR elementId(id_rows[0]) <> elementId(existing_rows[0])) AS has_identity_conflict
CALL {
  WITH census, unit, existing_rows, has_identity_conflict
  UNWIND CASE WHEN has_identity_conflict = false
    AND (size(existing_rows) = 1 OR unit.state IN ['pending_publication','paused'])
    THEN [1] ELSE [] END AS reserve
  MERGE (publication:StandaloneCrmChildPublication {census_id: census.census_id, generation: $generation,
    unit_kind: $unit_kind, sequence: $sequence})
  ON CREATE SET publication.publication_id = $publication_id, publication.task_id = $task_id,
    publication.task_name = $task_name, publication.queue = $queue, publication.payload_json = $payload_json,
    publication.payload_digest = $payload_digest, publication.status = 'reserved',
    publication.created_at = datetime(), publication.updated_at = datetime()
  RETURN publication, false AS identity_conflict
  UNION
  WITH has_identity_conflict
  UNWIND CASE WHEN has_identity_conflict THEN [1] ELSE [] END AS conflict
  RETURN NULL AS publication, true AS identity_conflict
}
WITH census, unit, publication, identity_conflict,
  CASE WHEN publication IS NULL THEN false ELSE
    publication.publication_id = $publication_id AND publication.payload_digest = $payload_digest
    AND publication.payload_json = $payload_json AND publication.task_id = $task_id
    AND publication.task_name = $task_name AND publication.queue = $queue
    AND publication.generation = $generation AND publication.unit_kind = $unit_kind
    AND publication.sequence = $sequence END AS immutable_match
FOREACH (_ IN CASE WHEN publication IS NOT NULL AND publication.status = 'reserved' THEN [1] ELSE [] END |
  SET unit.state = 'publishing', unit.generation = $generation,
    unit.parent_fence_token = $parent_fence_token, census.state = 'publishing',
    unit.updated_at = datetime(), census.updated_at = datetime())
RETURN publication.publication_id AS publication_id, publication.task_id AS task_id,
  publication.payload_json AS payload_json, publication.payload_digest AS payload_digest,
  publication.task_name AS task_name, publication.queue AS queue, publication.status AS status,
  identity_conflict OR NOT immutable_match AS payload_conflict
"""
)


MARK_PUBLICATION_PUBLISHING = (
    _FRESHNESS
    + """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, authority_digest: $authority_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  current_generation: $generation, fence_token: $parent_fence_token})
MATCH (publication:StandaloneCrmChildPublication {publication_id: $publication_id, census_id: $census_id,
  generation: $generation})
WHERE census.cancel_requested_at IS NULL AND census.fatal_reason IS NULL
  AND census.state IN ['frozen','publishing','running']
  AND publication.status IN ['reserved','ambiguous','published']
SET publication.status = 'publishing', publication.updated_at = datetime()
RETURN publication.publication_id AS publication_id
"""
)

MARK_PUBLICATION_AMBIGUOUS = (
    _SETTLEMENT
    + """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, authority_digest: $authority_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  current_generation: $generation, fence_token: $parent_fence_token})
MATCH (publication:StandaloneCrmChildPublication {publication_id: $publication_id, census_id: $census_id,
  generation: $generation})
WHERE publication.status IN ['reserved','publishing','ambiguous']
SET publication.status = CASE WHEN census.cancel_requested_at IS NULL THEN 'ambiguous' ELSE 'retired' END,
  publication.retired_at = CASE WHEN census.cancel_requested_at IS NULL THEN publication.retired_at
                                ELSE coalesce(publication.retired_at, datetime()) END,
  publication.updated_at = datetime()
RETURN publication.publication_id AS publication_id
"""
)

MARK_PUBLICATION_PUBLISHED = (
    _SETTLEMENT
    + """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, authority_digest: $authority_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  current_generation: $generation, fence_token: $parent_fence_token})
MATCH (publication:StandaloneCrmChildPublication {publication_id: $publication_id, census_id: $census_id,
  generation: $generation})
WHERE census.cancel_requested_at IS NULL AND publication.status IN ['reserved','publishing','ambiguous']
SET publication.status = 'published', publication.published_at = coalesce(publication.published_at, datetime()),
  publication.updated_at = datetime()
RETURN publication.publication_id AS publication_id
"""
)

AUTHORIZE_PUBLICATION_BROKER = (
    _FRESHNESS
    + """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, authority_digest: $authority_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  current_generation: $generation, fence_token: $parent_fence_token})
MATCH (attempt:StandaloneCrmCensusAttempt {census_id: $census_id, generation: $generation,
  fence_token: $parent_fence_token, state: 'running'})
MATCH (publication:StandaloneCrmChildPublication {publication_id: $publication_id, census_id: $census_id,
  generation: $generation, status: 'publishing'})
WHERE census.cancel_requested_at IS NULL AND census.fatal_reason IS NULL
  AND census.state IN ['frozen','publishing','running'] AND attempt.lease_until >= datetime()
  AND attempt.deadline_at > datetime() AND attempt.occurrence_deadline_at > datetime()
  AND NOT EXISTS {
    MATCH (fence:StandaloneCrmUnitFence {census_id: $census_id, unit_kind: publication.unit_kind,
      generation: $generation})
    WHERE fence.state IN ['active','released','superseded'] AND fence.owner_task_id IS NOT NULL
  }
  AND NOT EXISTS {
    MATCH (checkpoint:StandaloneCrmCensusCheckpoint {census_id: $census_id,
      unit_kind: publication.unit_kind, generation: $generation})
    WHERE checkpoint.version > 1 AND checkpoint.child_fence_token > 0
  }
SET publication.pre_broker_authorized_at = datetime(), publication.updated_at = datetime()
RETURN publication.publication_id AS publication_id
"""
)


GET_PUBLICATION_RECOVERY = """
MATCH (publication:StandaloneCrmChildPublication {publication_id: $publication_id})
MATCH (census:StandaloneCrmCensus {census_id: publication.census_id})
OPTIONAL MATCH (fence:StandaloneCrmUnitFence {census_id: census.census_id,
  unit_kind: publication.unit_kind, generation: publication.generation})
WHERE fence.state IN ['active','released','superseded'] AND fence.owner_task_id IS NOT NULL
OPTIONAL MATCH (checkpoint:StandaloneCrmCensusCheckpoint {census_id: census.census_id,
  unit_kind: publication.unit_kind, generation: publication.generation})
WHERE checkpoint.version > 1 AND checkpoint.child_fence_token > 0
RETURN census {.*} AS census, publication {.*} AS publication,
  CASE WHEN count(fence) > 0 THEN 'fence_claim'
       WHEN count(checkpoint) > 0 THEN 'checkpoint_advanced'
       ELSE 'none' END AS observation
"""

CONFIRM_OBSERVED_PUBLICATION = (
    _SETTLEMENT
    + """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, authority_digest: $authority_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  current_generation: $generation, fence_token: $parent_fence_token})
MATCH (publication:StandaloneCrmChildPublication {publication_id: $publication_id, census_id: $census_id,
  generation: $generation})
WHERE publication.status IN ['reserved','publishing','ambiguous'] AND (
  EXISTS {
    MATCH (fence:StandaloneCrmUnitFence {census_id: $census_id, unit_kind: publication.unit_kind,
      generation: $generation})
    WHERE fence.state IN ['active','released','superseded'] AND fence.owner_task_id IS NOT NULL
  }
  OR EXISTS {
    MATCH (checkpoint:StandaloneCrmCensusCheckpoint {census_id: $census_id, unit_kind: publication.unit_kind,
      generation: $generation})
    WHERE checkpoint.version > 1 AND checkpoint.child_fence_token > 0
  }
)
SET publication.status = 'published', publication.confirmed_by_child_at = datetime(),
  publication.updated_at = datetime()
RETURN publication.publication_id AS publication_id
"""
)

PAUSE_ATTEMPT = (
    _FRESHNESS
    + """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, authority_digest: $authority_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  current_generation: $generation, fence_token: $parent_fence_token})
MATCH (attempt:StandaloneCrmCensusAttempt {census_id: $census_id, generation: $generation,
  fence_token: $parent_fence_token, state: 'running'})
SET attempt.state = 'paused_with_checkpoint', attempt.updated_at = datetime(), census.state = 'paused_with_checkpoint',
  census.pause_reason = $reason, census.updated_at = datetime()
RETURN census.census_id AS census_id
"""
)

CONTINUE_ATTEMPT = (
    _FRESHNESS
    + """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint,
  authority_digest: $authority_digest, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id})
WHERE census.state IN ['paused_with_checkpoint','recovering','running']
  AND census.cancel_requested_at IS NULL
SET census.updated_at = census.updated_at
WITH census
OPTIONAL MATCH (same:StandaloneCrmCensusAttempt {census_id: census.census_id, task_id: $task_id})
WITH census, collect(same) AS redelivery
CALL {
  WITH census, redelivery
  UNWIND CASE WHEN size(redelivery) = 1 AND census.state = 'running'
    AND redelivery[0].state = 'running' AND redelivery[0].generation = census.current_generation
    AND redelivery[0].fence_token = census.fence_token
    AND redelivery[0].lease_until >= datetime() AND redelivery[0].deadline_at > datetime()
    AND redelivery[0].occurrence_deadline_at > datetime()
    THEN [redelivery[0]] ELSE [] END AS attempt
  RETURN attempt
  UNION
  WITH census, redelivery
  UNWIND CASE WHEN size(redelivery) = 0 AND census.state IN ['paused_with_checkpoint','recovering']
    AND census.attempt_count < $max_attempts AND census.occurrence_deadline_at > datetime()
    AND census.call_count < $max_calls_per_occurrence AND census.row_count < $max_rows_per_occurrence
    THEN [1] ELSE [] END AS continue_attempt
  MATCH (old:StandaloneCrmCensusAttempt {census_id: census.census_id,
    generation: census.current_generation, fence_token: census.fence_token})
  WHERE old.state IN ['paused_with_checkpoint','superseded']
    AND NOT EXISTS {
      MATCH (old_publication:StandaloneCrmChildPublication {census_id: census.census_id,
        generation: old.generation})
      WHERE old_publication.status IN ['reserved','publishing','ambiguous']
        OR (old_publication.status = 'published' AND NOT EXISTS {
          MATCH (old_fence:StandaloneCrmUnitFence {census_id: census.census_id,
            unit_kind: old_publication.unit_kind, generation: old.generation})
          WHERE old_fence.state IN ['active','released','superseded']
            AND old_fence.owner_task_id IS NOT NULL
        } AND NOT EXISTS {
          MATCH (old_checkpoint:StandaloneCrmCensusCheckpoint {census_id: census.census_id,
            unit_kind: old_publication.unit_kind, generation: old.generation})
          WHERE old_checkpoint.version > 1 AND old_checkpoint.child_fence_token > 0
        })
    }
    AND NOT EXISTS {
      MATCH (:StandaloneCrmUnitFence {census_id: census.census_id, generation: old.generation,
        state: 'active'})
    }
    AND NOT EXISTS {
      MATCH (:StandaloneCrmHttpCallReservation {census_id: census.census_id,
        generation: old.generation, outcome: 'reserved'})
    }
  OPTIONAL MATCH (old_fence:StandaloneCrmUnitFence {census_id: census.census_id,
    generation: old.generation, state: 'active'})
  WITH census, old, collect(old_fence) AS old_fences
  SET old.state = 'superseded', old.superseded_at = datetime(), old.updated_at = datetime()
  FOREACH (old_fence IN old_fences |
    SET old_fence.state = 'superseded', old_fence.superseded_at = datetime(),
      old_fence.updated_at = datetime())
  CREATE (attempt:StandaloneCrmCensusAttempt {
    census_id: census.census_id, generation: coalesce(census.current_generation, 0) + 1,
    task_id: $task_id, state: 'running', fence_token: coalesce(census.fence_token, 0) + 1,
    lease_until: datetime() + duration({seconds: $lease_seconds}),
    deadline_at: datetime() + duration({seconds: $attempt_runtime_seconds}),
    occurrence_deadline_at: census.occurrence_deadline_at,
    call_count: 0, row_count: 0, created_at: datetime(), updated_at: datetime()
  })
  SET census.current_generation = attempt.generation, census.fence_token = attempt.fence_token,
    census.attempt_count = census.attempt_count + 1, census.state = 'running',
    census.updated_at = datetime()
  RETURN attempt
}
RETURN attempt.census_id AS census_id, attempt.generation AS generation, attempt.task_id AS task_id,
  attempt.state AS state, attempt.fence_token AS fence_token, toString(attempt.deadline_at) AS deadline_at,
  toString(attempt.occurrence_deadline_at) AS occurrence_deadline_at
"""
)


FAIL_EXHAUSTED_CENSUS = (
    _FRESHNESS
    + """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint,
  authority_digest: $authority_digest, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id})
WHERE census.terminal_state IS NULL AND (
  census.attempt_count >= $max_attempts OR census.occurrence_deadline_at <= datetime()
  OR census.call_count >= $max_calls_per_occurrence OR census.row_count >= $max_rows_per_occurrence
)
SET census.state = 'failed', census.terminal_state = 'failed', census.terminal_reason = $reason,
  census.terminal_at = datetime(), census.updated_at = datetime()
WITH census
OPTIONAL MATCH (scope:StandaloneCrmCensusScopeLock {source_key: census.source_key,
  source_instance_id: census.source_instance_id, control_instance_id: census.control_instance_id,
  census_kind: census.census_kind, active_census_id: census.census_id})
REMOVE scope.active_census_id
RETURN census.census_id AS census_id
"""
)

MARK_CENSUS_FREEZE_FAILED = (
    _FRESHNESS
    + """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, authority_digest: $authority_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  current_generation: $generation, fence_token: $parent_fence_token})
WHERE census.source_window_json IS NULL AND coalesce(census.no_source_window, false) = false
  AND census.state IN ['allocated','freezing','running','cancel_requested','recovering']
SET census.state = 'freeze_failed', census.terminal_state = 'freeze_failed', census.terminal_reason = $reason,
  census.terminal_at = datetime(), census.updated_at = datetime()
WITH census
MATCH (scope:StandaloneCrmCensusScopeLock {source_key: census.source_key, source_instance_id: census.source_instance_id,
  control_instance_id: census.control_instance_id, census_kind: census.census_kind, active_census_id: census.census_id})
REMOVE scope.active_census_id
RETURN census.census_id AS census_id
"""
)
