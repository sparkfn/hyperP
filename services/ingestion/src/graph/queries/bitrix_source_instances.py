"""Cypher for immutable Bitrix source-instance control registrations."""

from __future__ import annotations

CREATE_BITRIX_SOURCE_INSTANCE_CONSTRAINTS: tuple[str, ...] = (
    """CREATE CONSTRAINT bitrix_source_instance_identity_unique IF NOT EXISTS
FOR (instance:BitrixSourceInstance)
REQUIRE (instance.source_key, instance.source_instance_id) IS UNIQUE""",
)

REGISTER_BITRIX_SOURCE_INSTANCE = """
MATCH (source:SourceSystem {source_key: $source_key, is_active: true})
MERGE (instance:BitrixSourceInstance {
  source_key: $source_key, source_instance_id: $source_instance_id
})
ON CREATE SET instance.status = 'active', instance.created_at = datetime(),
              instance.updated_at = datetime(), instance.creation_token = $creation_token
WITH source, instance, instance.creation_token = $creation_token AS created
REMOVE instance.creation_token
MERGE (instance)-[:INSTANCE_OF]->(source)
WITH instance, created
OPTIONAL MATCH (instance)-[relationship:INSTANCE_OF]->(linked:SourceSystem)
WITH instance, created, count(relationship) AS relationship_count,
     collect(DISTINCT linked) AS linked_sources
WHERE (created OR instance.status = 'active')
  AND relationship_count = 1
  AND size(linked_sources) = 1
  AND linked_sources[0].source_key = $source_key
  AND linked_sources[0].is_active = true
RETURN instance.source_key AS source_key, instance.source_instance_id AS source_instance_id,
       instance.status AS status, created AS created
"""

REQUIRE_ACTIVE_BITRIX_SOURCE_INSTANCE = """
OPTIONAL MATCH (instance:BitrixSourceInstance {
  source_key: $source_key, source_instance_id: $source_instance_id
})
OPTIONAL MATCH (instance)-[relationship:INSTANCE_OF]->(source:SourceSystem)
WITH collect(DISTINCT instance) AS instances,
     collect(DISTINCT source) AS sources,
     count(relationship) AS relationship_count
RETURN size(instances) AS matches,
       [instance IN instances | instance.status] AS statuses,
       size(sources) AS source_matches,
       relationship_count AS relationship_count,
       [source IN sources | source.source_key] AS source_keys,
       [source IN sources | source.is_active] AS source_active
"""

DISABLE_BITRIX_SOURCE_INSTANCE = """
MATCH (instance:BitrixSourceInstance {
  source_key: $source_key, source_instance_id: $source_instance_id, status: 'active'
})
WHERE $source_instance_id <> 'legacy-default'
  AND size([(instance)-[:INSTANCE_OF]->(:SourceSystem) | 1]) = 1
  AND size([(instance)-[:INSTANCE_OF]->(:SourceSystem {
    source_key: $source_key, is_active: true
  }) | 1]) = 1
OPTIONAL MATCH (logical:IngestionLogicalRun {
  source_key: $source_key, control_instance_id: $source_instance_id
})
WHERE logical.status IN ['queued', 'running', 'stop_requested', 'paused_with_checkpoint']
WITH instance, count(logical) AS active_logical
OPTIONAL MATCH (run:IngestRun {control_instance_id: $source_instance_id})
WHERE run.status IN ['queued', 'started', 'running', 'stop_requested', 'paused_with_checkpoint']
  AND (
    run.source_key = $source_key
    OR size([(run)-[:FROM_SOURCE]->(:SourceSystem {source_key: $source_key}) | 1]) = 1
  )
WITH instance, active_logical, count(DISTINCT run) AS active_runs
OPTIONAL MATCH (stream:BitrixIngestionStream {
  source_key: $source_key, control_instance_id: $source_instance_id, status: 'active'
})
WITH instance, active_logical, active_runs, count(stream) AS active_streams
OPTIONAL MATCH (outbox:BitrixBackfillDispatchOutbox {control_instance_id: $source_instance_id})
WHERE outbox.status IN ['pending', 'publishing']
WITH instance, active_logical, active_runs, active_streams, count(outbox) AS pending_outboxes
OPTIONAL MATCH (dispatch:BitrixDispatchControl {
  source_key: $source_key, control_instance_id: $source_instance_id
})
WITH instance, active_logical, active_runs, active_streams, pending_outboxes,
     count(CASE WHEN dispatch.active_generation_id IS NOT NULL
                 OR dispatch.active_owner IS NOT NULL
                THEN dispatch END) AS active_dispatches
OPTIONAL MATCH (generation:BitrixBackfillGeneration {
  control_instance_id: $source_instance_id
})
WHERE generation.status IN ['allocated', 'backfilling', 'activating', 'active']
WITH instance, active_logical, active_runs, active_streams, pending_outboxes, active_dispatches,
     count(generation) AS pending_generations
WHERE active_logical = 0 AND active_runs = 0 AND active_streams = 0 AND pending_outboxes = 0
  AND active_dispatches = 0 AND pending_generations = 0
SET instance.status = 'disabled', instance.disabled_at = datetime(),
    instance.disabled_by = $actor, instance.disable_reason = $reason, instance.updated_at = datetime()
RETURN instance.source_instance_id AS source_instance_id
"""

ADMIT_BITRIX_CONTROL_INSTANCE = """
MATCH (migration:DataMigration {migration_key: 'bitrix_control_instance_v1'})
WHERE migration.completed_at IS NOT NULL
OPTIONAL MATCH (control:BitrixSourceInstance {
  source_key: 'bitrix_chat', source_instance_id: $control_instance_id, status: 'active'
})-[:INSTANCE_OF]->(:SourceSystem {source_key: 'bitrix_chat', is_active: true})
OPTIONAL MATCH (source:BitrixSourceInstance {
  source_key: 'bitrix_chat', source_instance_id: $source_instance_id, status: 'active'
})-[:INSTANCE_OF]->(:SourceSystem {source_key: 'bitrix_chat', is_active: true})
OPTIONAL MATCH (dispatch:BitrixDispatchControl {
  source_key: 'bitrix_chat', control_instance_id: $control_instance_id
})
WITH collect(DISTINCT control) AS controls,
     collect(DISTINCT source) AS sources,
     collect(DISTINCT dispatch) AS dispatches
WHERE size(controls) = 1
  AND size(sources) = 1
  AND size([(controls[0])-[:INSTANCE_OF]->(:SourceSystem) | 1]) = 1
  AND size([(sources[0])-[:INSTANCE_OF]->(:SourceSystem) | 1]) = 1
  AND size(dispatches) <= 1
  AND coalesce(dispatches[0].blocked, false) = false
RETURN controls[0].source_instance_id AS control_instance_id,
       sources[0].source_instance_id AS source_instance_id
"""
