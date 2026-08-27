"""Cancellation and pending-fatal settlement Cypher for standalone CRM census."""

from __future__ import annotations

_SETTLEMENT = """
MATCH (ready273:DataMigration {migration_key: 'standalone_crm_census_control_v1'})
WHERE ready273.completed_at IS NOT NULL
MATCH (ready272:DataMigration {migration_key: 'bitrix_control_instance_v1'})
WHERE ready272.completed_at IS NOT NULL
"""

REQUEST_CANCELLATION = (
    _SETTLEMENT
    + """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, authority_digest: $authority_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id})
WHERE census.state IN ['allocated','freezing','frozen','publishing','running','pause_requested',
  'paused_with_checkpoint','continuing','cancel_requested','recovering','freeze_failed',
  'authority_stale_pending']
WITH census, census.source_window_json IS NULL AND coalesce(census.no_source_window, false) = false AS pre_window,
  census.cancel_requested_at IS NULL AS first_request, census.fatal_reason = 'authority_stale' AS fatal_stale
SET census.cancel_requested_at = coalesce(census.cancel_requested_at, datetime()),
  census.cancel_requested_by = coalesce(census.cancel_requested_by, $actor),
  census.cancel_reason = coalesce(census.cancel_reason, $reason),
  census.state = CASE WHEN fatal_stale THEN 'authority_stale_pending'
                      WHEN pre_window THEN 'freeze_failed' ELSE 'cancel_requested' END,
  census.terminal_state = CASE WHEN fatal_stale THEN census.terminal_state
                               WHEN pre_window THEN 'freeze_failed' ELSE census.terminal_state END,
  census.terminal_reason = CASE WHEN fatal_stale THEN census.terminal_reason
                                WHEN pre_window THEN coalesce(census.terminal_reason, 'cancelled_before_window')
                                ELSE census.terminal_reason END,
  census.terminal_at = CASE WHEN fatal_stale THEN census.terminal_at
                            WHEN pre_window THEN coalesce(census.terminal_at, datetime())
                            ELSE census.terminal_at END,
  census.updated_at = datetime()
WITH census, pre_window, first_request, fatal_stale
OPTIONAL MATCH (unit:StandaloneCrmCensusUnit {census_id: census.census_id})
WHERE NOT pre_window AND first_request AND unit.state IN ['pending_publication','queued','paused']
SET unit.cancel_requested_at = datetime(), unit.state = 'cancelled', unit.updated_at = datetime()
WITH census, pre_window, first_request, fatal_stale, count(unit) AS directly_cancelled
OPTIONAL MATCH (publication:StandaloneCrmChildPublication {census_id: census.census_id})
WHERE NOT pre_window AND first_request AND publication.status IN ['reserved','publishing','ambiguous','published']
  AND NOT EXISTS {
    MATCH (fence:StandaloneCrmUnitFence {census_id: census.census_id, unit_kind: publication.unit_kind,
      generation: publication.generation})
    WHERE fence.state IN ['active','released','superseded'] AND fence.owner_task_id IS NOT NULL
  }
  AND NOT EXISTS {
    MATCH (checkpoint:StandaloneCrmCensusCheckpoint {census_id: census.census_id,
      unit_kind: publication.unit_kind, generation: publication.generation})
    WHERE checkpoint.version > 1 AND checkpoint.child_fence_token > 0
  }
SET publication.status = 'retired', publication.retired_at = datetime(), publication.updated_at = datetime()
WITH census, pre_window, first_request, fatal_stale, directly_cancelled,
  collect(publication.unit_kind) AS retired_kinds, count(publication) AS retired_publications
OPTIONAL MATCH (retired_unit:StandaloneCrmCensusUnit {census_id: census.census_id})
WHERE NOT pre_window AND first_request AND retired_unit.unit_kind IN retired_kinds
  AND retired_unit.state IN ['publishing','queued','paused']
SET retired_unit.cancel_requested_at = datetime(), retired_unit.state = 'cancelled',
  retired_unit.updated_at = datetime()
WITH census, pre_window, first_request, fatal_stale, directly_cancelled, retired_publications,
  count(retired_unit) AS retired_units
OPTIONAL MATCH (fence:StandaloneCrmUnitFence {census_id: census.census_id, state: 'active'})
FOREACH (_ IN CASE WHEN NOT pre_window AND first_request THEN [1] ELSE [] END |
  SET fence.cancel_requested_at = coalesce(fence.cancel_requested_at, datetime()), fence.updated_at = datetime())
WITH census, pre_window, fatal_stale, directly_cancelled, retired_units, retired_publications, count(fence) AS active_fences
OPTIONAL MATCH (scope:StandaloneCrmCensusScopeLock {active_census_id: census.census_id})
FOREACH (_ IN CASE WHEN pre_window AND NOT fatal_stale THEN [1] ELSE [] END | REMOVE scope.active_census_id)
RETURN directly_cancelled + retired_units AS child_count, retired_publications, active_fences,
  pre_window AND NOT fatal_stale AS freeze_failed
"""
)


MARK_CENSUS_AUTHORITY_STALE = (
    _SETTLEMENT
    + """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, fingerprint: $fingerprint,
  authority_digest: $authority_digest, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id})
WHERE census.terminal_state IS NULL
SET census.state = 'authority_stale_pending', census.fatal_reason = 'authority_stale',
  census.updated_at = datetime()
RETURN census.census_id AS census_id
"""
)
