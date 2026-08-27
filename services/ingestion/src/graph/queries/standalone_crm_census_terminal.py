"""Terminal derivation and durable status Cypher for standalone CRM census."""

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

TERMINALIZE_CENSUS = (
    _SETTLEMENT
    + """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, authority_digest: $authority_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  current_generation: $generation, fence_token: $parent_fence_token})
MATCH (attempt:StandaloneCrmCensusAttempt {census_id: $census_id, generation: $generation,
  fence_token: $parent_fence_token})
WHERE attempt.state IN ['running','paused_with_checkpoint']
OPTIONAL MATCH (unit:StandaloneCrmCensusUnit {census_id: census.census_id})
WITH census, attempt, collect(unit) AS units
WHERE size(units) = census.expected_unit_count
OPTIONAL MATCH (publication:StandaloneCrmChildPublication {census_id: census.census_id})
WITH census, attempt, units, collect(publication) AS publications
OPTIONAL MATCH (fence:StandaloneCrmUnitFence {census_id: census.census_id})
WITH census, attempt, units, publications, collect(fence) AS fences
OPTIONAL MATCH (checkpoint:StandaloneCrmCensusCheckpoint {census_id: census.census_id})
WITH census, attempt, units, publications, fences, collect(checkpoint) AS checkpoints,
  [unit IN units WHERE unit.upper_id IS NULL OR unit.upper_id > 0] AS publication_units
WHERE (census.source_window_json IS NOT NULL OR coalesce(census.no_source_window, false)
       OR (census.fatal_reason = 'authority_stale' AND size(units) = 0))
  AND all(unit IN units WHERE unit.state IN ['completed','failed','cancelled','superseded'])
  AND all(publication IN publications WHERE publication.sequence = 1
    AND publication.unit_kind IN [unit IN publication_units | unit.unit_kind]
    AND publication.status IN ['published','retired'])
  AND all(fence IN fences WHERE fence.unit_kind IN [unit IN publication_units | unit.unit_kind]
    AND fence.state IN ['released','superseded'])
  AND all(publication IN publications WHERE CASE WHEN publication.status = 'published'
      THEN size([fence IN fences WHERE fence.unit_kind = publication.unit_kind
        AND fence.generation = publication.generation AND fence.state IN ['released','superseded']]) = 1
      WHEN publication.status = 'retired'
      THEN size([fence IN fences WHERE fence.unit_kind = publication.unit_kind
        AND fence.generation = publication.generation]) = 0
      ELSE false END)
  AND all(unit IN units WHERE CASE WHEN unit.upper_id = 0
      THEN size([publication IN publications WHERE publication.unit_kind = unit.unit_kind]) = 0
      ELSE true END)
  AND CASE WHEN census.cancel_requested_at IS NULL THEN
    all(unit IN publication_units WHERE size([publication IN publications
      WHERE publication.unit_kind = unit.unit_kind AND publication.status = 'published'
        AND publication.generation = unit.generation]) = 1)
    AND all(unit IN publication_units WHERE size([fence IN fences
      WHERE fence.unit_kind = unit.unit_kind AND fence.generation = unit.generation
        AND fence.state IN ['released','superseded']]) = 1)
    AND all(unit IN publication_units WHERE size([checkpoint IN checkpoints
      WHERE checkpoint.unit_kind = unit.unit_kind AND checkpoint.generation = unit.generation
        AND checkpoint.version > 1 AND checkpoint.child_fence_token > 0]) = 1)
    ELSE
    all(unit IN publication_units WHERE
      CASE WHEN unit.state = 'cancelled'
        AND size([publication IN publications WHERE publication.unit_kind = unit.unit_kind]) = 0
        THEN size([fence IN fences WHERE fence.unit_kind = unit.unit_kind]) = 0
          AND size([checkpoint IN checkpoints WHERE checkpoint.unit_kind = unit.unit_kind
            AND checkpoint.version > 1 AND checkpoint.child_fence_token > 0]) = 0
      WHEN size([publication IN publications WHERE publication.unit_kind = unit.unit_kind
          AND publication.status = 'retired']) > 0
        THEN unit.state = 'cancelled'
          AND all(publication IN publications WHERE publication.unit_kind <> unit.unit_kind
            OR publication.status <> 'retired'
            OR (size([fence IN fences WHERE fence.unit_kind = unit.unit_kind
              AND fence.generation = publication.generation]) = 0
              AND size([checkpoint IN checkpoints WHERE checkpoint.unit_kind = unit.unit_kind
                AND checkpoint.generation = publication.generation AND checkpoint.version > 1
                AND checkpoint.child_fence_token > 0]) = 0))
      WHEN size([publication IN publications WHERE publication.unit_kind = unit.unit_kind
          AND publication.status = 'published' AND publication.generation = unit.generation]) = 1
        THEN size([fence IN fences WHERE fence.unit_kind = unit.unit_kind
            AND fence.generation = unit.generation AND fence.state IN ['released','superseded']]) = 1
          AND size([checkpoint IN checkpoints WHERE checkpoint.unit_kind = unit.unit_kind
            AND checkpoint.generation = unit.generation AND checkpoint.version > 1
            AND checkpoint.child_fence_token > 0]) = 1
      ELSE false END)
  END
WITH census, attempt, units,
  reduce(processed = 0, unit IN units | processed + coalesce(unit.processed_count, 0)) AS processed,
  reduce(skipped = 0, unit IN units | skipped + coalesce(unit.skipped_count, 0)) AS skipped,
  reduce(failed = 0, unit IN units | failed + coalesce(unit.failed_count, 0)) AS failed,
  size([unit IN units WHERE unit.upper_id = 0]) AS no_work
WITH census, attempt, units, processed, skipped, failed, no_work, size(units) AS expected_units,
  CASE WHEN census.fatal_reason = 'authority_stale' THEN 'failed'
       WHEN census.terminal_state IN ['freeze_failed','failed'] THEN census.terminal_state
       WHEN failed > 0 OR any(unit IN units WHERE unit.state = 'failed') THEN 'failed'
       WHEN census.cancel_requested_at IS NOT NULL THEN 'cancelled_with_checkpoint'
       ELSE 'completed' END AS derived_state
SET census.state = derived_state, census.terminal_state = derived_state, census.expected_units = expected_units,
  attempt.state = CASE WHEN derived_state = 'failed' THEN 'failed' ELSE 'completed' END,
  attempt.lease_until = datetime(), attempt.updated_at = datetime(),
  census.processed_units = processed, census.skipped_units = skipped, census.failed_units = failed,
  census.no_work_units = no_work, census.terminal_at = datetime(), census.updated_at = datetime()
WITH census, expected_units, processed, skipped, failed, no_work
MATCH (scope:StandaloneCrmCensusScopeLock {source_key: census.source_key, source_instance_id: census.source_instance_id,
  control_instance_id: census.control_instance_id, census_kind: census.census_kind, active_census_id: census.census_id})
REMOVE scope.active_census_id
RETURN census.terminal_state AS terminal_state, expected_units, processed, skipped, failed, no_work
"""
)

GET_CENSUS_STATUS = """
MATCH (census:StandaloneCrmCensus {census_id: $census_id})
OPTIONAL MATCH (attempt:StandaloneCrmCensusAttempt {census_id: census.census_id})
OPTIONAL MATCH (unit:StandaloneCrmCensusUnit {census_id: census.census_id})
OPTIONAL MATCH (publication:StandaloneCrmChildPublication {census_id: census.census_id})
OPTIONAL MATCH (fence:StandaloneCrmUnitFence {census_id: census.census_id})
RETURN census {.*, terminal_state: census.terminal_state} AS census,
  collect(DISTINCT attempt {.*, deadline_at: toString(attempt.deadline_at),
    occurrence_deadline_at: toString(attempt.occurrence_deadline_at)}) AS attempts,
  collect(DISTINCT unit {.*}) AS units,
  collect(DISTINCT publication {.*}) AS publications, collect(DISTINCT fence {.*}) AS fences
"""
