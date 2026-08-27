"""Split Cypher primitives for standalone CRM census windows, fences, and checkpoints."""

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

FREEZE_SOURCE_WINDOW = (
    _FRESHNESS
    + """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint,
  authority_digest: $authority_digest, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, current_generation: $generation,
  fence_token: $parent_fence_token})
MATCH (attempt:StandaloneCrmCensusAttempt {census_id: $census_id, generation: $generation,
  fence_token: $parent_fence_token, state: 'running'})
WHERE census.census_kind = 'source_sync' AND census.source_window_json IS NULL
  AND census.cancel_requested_at IS NULL AND attempt.lease_until >= datetime()
  AND attempt.deadline_at > datetime() AND attempt.occurrence_deadline_at > datetime()
  AND $selection_size = size($bounds)
  AND size([kind IN $selected_kinds WHERE size([bound IN $bounds WHERE bound.unit_kind = kind]) = 1]) = size($selected_kinds)
  AND all(bound IN $bounds WHERE bound.upper_id >= 0)
SET census.source_window_json = $window_json, census.expected_unit_count = $selection_size,
    census.state = 'frozen', census.updated_at = datetime()
WITH census
UNWIND $bounds AS bound
MERGE (unit:StandaloneCrmCensusUnit {census_id: census.census_id, unit_kind: bound.unit_kind})
ON CREATE SET unit.upper_id = bound.upper_id, unit.state = CASE WHEN bound.upper_id = 0 THEN 'completed' ELSE 'pending_publication' END,
  unit.processed_count = 0, unit.skipped_count = 0, unit.failed_count = 0,
  unit.no_work_count = CASE WHEN bound.upper_id = 0 THEN 1 ELSE 0 END, unit.generation = $generation,
  unit.parent_fence_token = $parent_fence_token, unit.created_at = datetime()
MERGE (checkpoint:StandaloneCrmCensusCheckpoint {census_id: census.census_id, unit_kind: bound.unit_kind})
ON CREATE SET checkpoint.upper_id = bound.upper_id, checkpoint.last_committed_id = NULL,
  checkpoint.processed_count = 0, checkpoint.skipped_count = 0, checkpoint.failed_count = 0,
  checkpoint.no_work_count = CASE WHEN bound.upper_id = 0 THEN 1 ELSE 0 END, checkpoint.version = 1,
  checkpoint.generation = $generation, checkpoint.parent_fence_token = $parent_fence_token,
  checkpoint.child_fence_token = 0, checkpoint.created_at = datetime()
SET unit.updated_at = datetime(), checkpoint.updated_at = datetime()
RETURN census.census_id AS census_id, count(unit) AS allocated_units
"""
)

FREEZE_NO_SOURCE_WINDOW = (
    _FRESHNESS
    + """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint,
  authority_digest: $authority_digest, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, current_generation: $generation,
  fence_token: $parent_fence_token})
MATCH (attempt:StandaloneCrmCensusAttempt {census_id: $census_id, generation: $generation,
  fence_token: $parent_fence_token, state: 'running'})
WHERE census.census_kind IN ['mapping_prepare','mapping_rollback'] AND census.state IN ['allocated','running']
  AND coalesce(census.no_source_window, false) = false AND census.cancel_requested_at IS NULL
  AND attempt.lease_until >= datetime() AND attempt.deadline_at > datetime()
  AND attempt.occurrence_deadline_at > datetime()
SET census.no_source_window = true, census.expected_unit_count = 1,
    census.state = 'frozen', census.updated_at = datetime()
WITH census
MERGE (unit:StandaloneCrmCensusUnit {census_id: census.census_id, unit_kind: $unit_kind})
ON CREATE SET unit.revision_id = $revision_id, unit.state = 'pending_publication', unit.processed_count = 0,
  unit.skipped_count = 0, unit.failed_count = 0, unit.no_work_count = 0, unit.generation = $generation,
  unit.parent_fence_token = $parent_fence_token, unit.created_at = datetime()
MERGE (checkpoint:StandaloneCrmCensusCheckpoint {census_id: census.census_id, unit_kind: $unit_kind})
ON CREATE SET checkpoint.revision_id = $revision_id, checkpoint.processed_count = 0, checkpoint.skipped_count = 0,
  checkpoint.failed_count = 0, checkpoint.no_work_count = 0, checkpoint.version = 1,
  checkpoint.generation = $generation, checkpoint.parent_fence_token = $parent_fence_token,
  checkpoint.child_fence_token = 0, checkpoint.created_at = datetime()
RETURN census.census_id AS census_id
"""
)

CLAIM_UNIT_FENCE = (
    _FRESHNESS
    + """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, authority_digest: $authority_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  current_generation: $generation, fence_token: $parent_fence_token})
MATCH (attempt:StandaloneCrmCensusAttempt {census_id: $census_id, generation: $generation,
  fence_token: $parent_fence_token, state: 'running'})
MATCH (unit:StandaloneCrmCensusUnit {census_id: $census_id, unit_kind: $unit_kind})
MATCH (publication:StandaloneCrmChildPublication {publication_id: $publication_id, census_id: $census_id,
  generation: $generation, unit_kind: $unit_kind})
WHERE census.cancel_requested_at IS NULL AND census.fatal_reason IS NULL
  AND census.state IN ['frozen','publishing','running']
  AND (publication.status = 'published'
       OR (publication.status IN ['publishing','ambiguous']
           AND publication.pre_broker_authorized_at IS NOT NULL))
  AND attempt.lease_until >= datetime() AND attempt.deadline_at > datetime()
  AND attempt.occurrence_deadline_at > datetime()
  AND unit.state IN ['pending_publication','publishing','queued','paused','running']
MERGE (fence:StandaloneCrmUnitFence {census_id: $census_id, generation: $generation,
  unit_kind: $unit_kind})
ON CREATE SET fence.parent_fence_token = $parent_fence_token, fence.child_fence_token = 0,
  fence.state = 'released', fence.created_at = datetime(), fence.updated_at = datetime()
WITH census, unit, fence
CALL {
  WITH census, unit, fence
  UNWIND CASE WHEN fence.state = 'active' AND fence.parent_fence_token = $parent_fence_token
    AND fence.owner_task_id = $task_id AND fence.lease_until >= datetime()
    AND (fence.cancel_requested_at IS NULL OR census.cancel_requested_at IS NOT NULL)
    THEN [fence] ELSE [] END AS claimed
  RETURN claimed
  UNION
  WITH census, unit, fence
  UNWIND CASE WHEN fence.state = 'released' AND fence.child_fence_token = 0
    THEN [fence] ELSE [] END AS claimable
  SET claimable.parent_fence_token = $parent_fence_token, claimable.child_fence_token = 1,
    claimable.state = 'active', claimable.owner_task_id = $task_id, claimable.claimed_at = datetime(),
    claimable.lease_until = datetime() + duration({seconds: $lease_seconds}),
    claimable.cancel_requested_at = CASE WHEN census.cancel_requested_at IS NULL THEN NULL
      ELSE datetime() END, claimable.updated_at = datetime(), unit.state = 'running',
    unit.generation = $generation, unit.parent_fence_token = $parent_fence_token,
    unit.child_fence_token = claimable.child_fence_token, unit.updated_at = datetime()
  RETURN claimable AS claimed
  UNION
  WITH census, unit, fence
  UNWIND CASE WHEN fence.state = 'active' AND fence.parent_fence_token = $parent_fence_token
    AND fence.lease_until < datetime() AND $recovery = true
    THEN [fence] ELSE [] END AS recoverable
  SET recoverable.child_fence_token = recoverable.child_fence_token + 1, recoverable.owner_task_id = $task_id,
    recoverable.claimed_at = datetime(), recoverable.lease_until = datetime() + duration({seconds: $lease_seconds}),
    recoverable.cancel_requested_at = CASE WHEN census.cancel_requested_at IS NULL THEN NULL
      ELSE datetime() END, recoverable.updated_at = datetime(), unit.state = 'running',
    unit.generation = $generation, unit.parent_fence_token = $parent_fence_token,
    unit.child_fence_token = recoverable.child_fence_token, unit.updated_at = datetime()
  RETURN recoverable AS claimed
}
RETURN claimed.child_fence_token AS child_fence_token
"""
)


_CHECKPOINT_MATCH = """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, authority_digest: $authority_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  current_generation: $generation, fence_token: $parent_fence_token})
MATCH (attempt:StandaloneCrmCensusAttempt {census_id: $census_id, generation: $generation,
  fence_token: $parent_fence_token, state: 'running'})
MATCH (unit:StandaloneCrmCensusUnit {census_id: $census_id, unit_kind: $unit_kind,
  generation: $generation, parent_fence_token: $parent_fence_token,
  child_fence_token: $child_fence_token})
MATCH (fence:StandaloneCrmUnitFence {census_id: $census_id, unit_kind: $unit_kind,
  generation: $generation, parent_fence_token: $parent_fence_token,
  child_fence_token: $child_fence_token, owner_task_id: $child_task_id, state: 'active'})
MATCH (checkpoint:StandaloneCrmCensusCheckpoint {census_id: $census_id, unit_kind: $unit_kind,
  version: $expected_version})
"""

_MONOTONIC_ACCOUNTING = """
  AND ($last_committed_id IS NULL OR checkpoint.last_committed_id IS NULL OR $last_committed_id >= checkpoint.last_committed_id)
  AND ($company_binding_after_contact_id IS NULL OR checkpoint.company_binding_after_contact_id IS NULL
       OR $company_binding_after_contact_id >= checkpoint.company_binding_after_contact_id)
  AND $processed_count >= checkpoint.processed_count AND $skipped_count >= checkpoint.skipped_count
  AND $failed_count >= checkpoint.failed_count AND $no_work_count >= checkpoint.no_work_count
  AND attempt.row_count + (
    ($processed_count + $skipped_count + $failed_count)
    - (coalesce(checkpoint.processed_count, 0) + coalesce(checkpoint.skipped_count, 0)
       + coalesce(checkpoint.failed_count, 0))
  ) <= $max_rows_per_attempt
  AND census.row_count + (
    ($processed_count + $skipped_count + $failed_count)
    - (coalesce(checkpoint.processed_count, 0) + coalesce(checkpoint.skipped_count, 0)
       + coalesce(checkpoint.failed_count, 0))
  ) <= $max_rows_per_occurrence
"""

_CHECKPOINT_GUARD = (
    _CHECKPOINT_MATCH
    + """
WHERE census.cancel_requested_at IS NULL AND census.fatal_reason IS NULL
  AND fence.cancel_requested_at IS NULL
  AND fence.lease_until >= datetime() AND attempt.lease_until >= datetime()
  AND attempt.deadline_at > datetime() AND attempt.occurrence_deadline_at > datetime()
"""
    + _MONOTONIC_ACCOUNTING
)

_SETTLE_GUARD = (
    _CHECKPOINT_MATCH
    + """
WHERE unit.state = 'running'
  AND (
    (
      census.fatal_reason IS NULL AND census.cancel_requested_at IS NULL
      AND fence.cancel_requested_at IS NULL
      AND fence.lease_until >= datetime() AND attempt.lease_until >= datetime()
      AND attempt.deadline_at > datetime() AND attempt.occurrence_deadline_at > datetime()
      AND $terminal_state IN ['completed','failed']
    )
    OR (
      census.fatal_reason = 'authority_stale' AND $terminal_state = 'failed'
      AND $processed_count = checkpoint.processed_count
      AND $skipped_count = checkpoint.skipped_count
      AND $failed_count = checkpoint.failed_count
      AND $no_work_count = checkpoint.no_work_count
      AND coalesce($last_committed_id, -1) = coalesce(checkpoint.last_committed_id, -1)
      AND coalesce($company_binding_after_contact_id, -1)
          = coalesce(checkpoint.company_binding_after_contact_id, -1)
    )
    OR (
      census.fatal_reason IS NULL AND census.cancel_requested_at IS NOT NULL
      AND fence.cancel_requested_at IS NOT NULL AND $terminal_state = 'cancelled'
      AND $processed_count = checkpoint.processed_count
      AND $skipped_count = checkpoint.skipped_count
      AND $failed_count = checkpoint.failed_count
      AND $no_work_count = checkpoint.no_work_count
      AND coalesce($last_committed_id, -1) = coalesce(checkpoint.last_committed_id, -1)
      AND coalesce($company_binding_after_contact_id, -1)
          = coalesce(checkpoint.company_binding_after_contact_id, -1)
    )
  )
"""
    + _MONOTONIC_ACCOUNTING
)


_CHECKPOINT_SET = """
SET census.row_count = census.row_count + (
    ($processed_count + $skipped_count + $failed_count)
    - (coalesce(checkpoint.processed_count, 0) + coalesce(checkpoint.skipped_count, 0)
       + coalesce(checkpoint.failed_count, 0))
  ),
  attempt.row_count = attempt.row_count + (
    ($processed_count + $skipped_count + $failed_count)
    - (coalesce(checkpoint.processed_count, 0) + coalesce(checkpoint.skipped_count, 0)
       + coalesce(checkpoint.failed_count, 0))
  ),
  checkpoint.last_committed_id = $last_committed_id,
  checkpoint.company_binding_after_contact_id = $company_binding_after_contact_id,
  checkpoint.processed_count = $processed_count, checkpoint.skipped_count = $skipped_count,
  checkpoint.failed_count = $failed_count, checkpoint.no_work_count = $no_work_count,
  checkpoint.generation = $generation, checkpoint.parent_fence_token = $parent_fence_token,
  checkpoint.child_fence_token = $child_fence_token, checkpoint.claimed_by_task_id = $child_task_id,
  checkpoint.version = checkpoint.version + 1, checkpoint.updated_at = datetime(),
  unit.processed_count = $processed_count, unit.skipped_count = $skipped_count,
  unit.failed_count = $failed_count, unit.no_work_count = $no_work_count, unit.updated_at = datetime(),
  census.updated_at = datetime()
"""

VALIDATE_CHECKPOINT_UNIT = _FRESHNESS + _CHECKPOINT_GUARD + "RETURN checkpoint.version AS version"
CHECKPOINT_UNIT = (
    _FRESHNESS + _CHECKPOINT_GUARD + _CHECKPOINT_SET + "RETURN checkpoint.version AS version"
)
VALIDATE_SETTLE_UNIT = _SETTLEMENT + _SETTLE_GUARD + "RETURN checkpoint.version AS version"
SETTLE_UNIT = (
    _SETTLEMENT
    + _SETTLE_GUARD
    + _CHECKPOINT_SET
    + """
SET unit.state = $terminal_state, unit.cancel_requested_at = CASE WHEN $terminal_state = 'cancelled'
      THEN coalesce(unit.cancel_requested_at, datetime()) ELSE unit.cancel_requested_at END,
  fence.state = 'released', fence.released_at = datetime(), fence.updated_at = datetime()
RETURN checkpoint.version AS version
"""
)

RENEW_UNIT_FENCE = (
    _FRESHNESS
    + """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, authority_digest: $authority_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  current_generation: $generation, fence_token: $parent_fence_token})
MATCH (attempt:StandaloneCrmCensusAttempt {census_id: $census_id, generation: $generation,
  fence_token: $parent_fence_token, state: 'running'})
MATCH (fence:StandaloneCrmUnitFence {census_id: $census_id, unit_kind: $unit_kind,
  generation: $generation, parent_fence_token: $parent_fence_token,
  child_fence_token: $child_fence_token, owner_task_id: $child_task_id, state: 'active'})
WHERE census.cancel_requested_at IS NULL AND census.fatal_reason IS NULL
  AND fence.cancel_requested_at IS NULL AND attempt.lease_until >= datetime() AND attempt.deadline_at > datetime()
  AND attempt.occurrence_deadline_at > datetime() AND fence.lease_until >= datetime()
SET fence.lease_until = datetime() + duration({seconds: $lease_seconds}),
  fence.updated_at = datetime(), attempt.lease_until = datetime() + duration({seconds: $lease_seconds}),
  attempt.updated_at = datetime()
RETURN fence.child_fence_token AS child_fence_token
"""
)

RELEASE_UNIT_FENCE = (
    _SETTLEMENT
    + """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, authority_digest: $authority_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  current_generation: $generation, fence_token: $parent_fence_token})
MATCH (attempt:StandaloneCrmCensusAttempt {census_id: $census_id, generation: $generation,
  fence_token: $parent_fence_token, state: 'running'})
MATCH (unit:StandaloneCrmCensusUnit {census_id: $census_id, unit_kind: $unit_kind,
  generation: $generation, parent_fence_token: $parent_fence_token,
  child_fence_token: $child_fence_token})
MATCH (fence:StandaloneCrmUnitFence {census_id: $census_id, unit_kind: $unit_kind,
  generation: $generation, parent_fence_token: $parent_fence_token,
  child_fence_token: $child_fence_token, owner_task_id: $child_task_id, state: 'active'})
WHERE (
    census.fatal_reason IS NULL AND census.cancel_requested_at IS NULL
    AND fence.cancel_requested_at IS NULL AND fence.lease_until >= datetime()
    AND attempt.lease_until >= datetime() AND attempt.deadline_at > datetime()
    AND attempt.occurrence_deadline_at > datetime()
  )
  OR (census.fatal_reason = 'authority_stale')
  OR (census.fatal_reason IS NULL AND census.cancel_requested_at IS NOT NULL
      AND fence.cancel_requested_at IS NOT NULL)
SET fence.state = 'released', fence.released_at = datetime(), fence.updated_at = datetime(),
  unit.state = CASE WHEN census.fatal_reason = 'authority_stale' THEN 'failed'
                    WHEN census.cancel_requested_at IS NOT NULL THEN 'cancelled' ELSE 'paused' END,
  unit.cancel_requested_at = CASE WHEN census.cancel_requested_at IS NOT NULL
      THEN coalesce(unit.cancel_requested_at, datetime()) ELSE unit.cancel_requested_at END,
  unit.updated_at = datetime()
RETURN fence.child_fence_token AS child_fence_token
"""
)
