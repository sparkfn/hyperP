"""Cypher for immutable Bitrix source-instance control registrations."""

from __future__ import annotations

CREATE_BITRIX_SOURCE_INSTANCE_CONSTRAINTS: tuple[str, ...] = (
    """CREATE CONSTRAINT bitrix_source_instance_identity_unique IF NOT EXISTS
FOR (instance:BitrixSourceInstance)
REQUIRE (instance.source_key, instance.source_instance_id) IS UNIQUE""",
    """CREATE CONSTRAINT bitrix_execution_source_binding_control_unique IF NOT EXISTS
FOR (binding:BitrixExecutionSourceBinding)
REQUIRE (binding.source_key, binding.control_instance_id) IS UNIQUE""",
)

REGISTER_BITRIX_SOURCE_INSTANCE = """
MATCH (source:SourceSystem {source_key: $source_key, is_active: true})
OPTIONAL MATCH (existing:BitrixSourceInstance {
  source_key: $source_key, source_instance_id: $source_instance_id
})
WITH source, collect(DISTINCT existing) AS existing_instances
WHERE size(existing_instances) <= 1
CALL {
  WITH source, existing_instances
  WITH source WHERE size(existing_instances) = 0
  CREATE (instance:BitrixSourceInstance {
    source_key: $source_key,
    source_instance_id: $source_instance_id,
    status: 'active',
    created_at: datetime(),
    updated_at: datetime()
  })
  CREATE (instance)-[:INSTANCE_OF]->(source)
  RETURN instance, true AS created
  UNION
  WITH source, existing_instances
  UNWIND existing_instances AS instance
  OPTIONAL MATCH (instance)-[relationship:INSTANCE_OF]->(linked:SourceSystem)
  WITH instance, count(relationship) AS relationship_count,
       collect(DISTINCT linked) AS linked_sources
  WHERE instance.status = 'active'
    AND relationship_count = 1
    AND size(linked_sources) = 1
    AND linked_sources[0].source_key = $source_key
    AND linked_sources[0].is_active = true
  RETURN instance, false AS created
}
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
OPTIONAL MATCH (instance)-[:OWNS_BITRIX_CONTROL]->(
  binding:BitrixExecutionSourceBinding {source_key: $source_key}
)
WITH instance,
     [control IN collect(DISTINCT binding.control_instance_id) WHERE control IS NOT NULL]
       AS owned_controls
OPTIONAL MATCH (logical:IngestionLogicalRun {source_key: $source_key})
WHERE logical.status IN ['queued', 'running', 'stop_requested', 'paused_with_checkpoint']
  AND (
    logical.control_instance_id IN owned_controls
    OR logical.control_instance_id IS NULL
    OR NOT EXISTS {
      MATCH (:BitrixExecutionSourceBinding {
        source_key: $source_key, control_instance_id: logical.control_instance_id
      })
    }
  )
WITH instance, owned_controls, count(logical) AS active_logical
OPTIONAL MATCH (run:IngestRun)
WHERE run.status IN ['queued', 'started', 'running', 'stop_requested', 'paused_with_checkpoint']
  AND (run.source_key = $source_key
       OR size([(run)-[:FROM_SOURCE]->(:SourceSystem {source_key: $source_key}) | 1]) = 1)
  AND (
    run.control_instance_id IN owned_controls
    OR run.control_instance_id IS NULL
    OR NOT EXISTS {
      MATCH (:BitrixExecutionSourceBinding {
        source_key: $source_key, control_instance_id: run.control_instance_id
      })
    }
  )
WITH instance, owned_controls, active_logical, count(DISTINCT run) AS active_runs
OPTIONAL MATCH (stream:BitrixIngestionStream {source_key: $source_key, status: 'active'})
WHERE stream.control_instance_id IN owned_controls
   OR stream.control_instance_id IS NULL
   OR NOT EXISTS {
     MATCH (:BitrixExecutionSourceBinding {
       source_key: $source_key, control_instance_id: stream.control_instance_id
     })
   }
WITH instance, owned_controls, active_logical, active_runs, count(stream) AS active_streams
OPTIONAL MATCH (outbox:BitrixBackfillDispatchOutbox)
WHERE outbox.status IN ['pending', 'publishing']
  AND (
    outbox.control_instance_id IN owned_controls
    OR outbox.control_instance_id IS NULL
    OR NOT EXISTS {
      MATCH (:BitrixExecutionSourceBinding {
        source_key: $source_key, control_instance_id: outbox.control_instance_id
      })
    }
  )
WITH instance, owned_controls, active_logical, active_runs, active_streams,
     count(outbox) AS pending_outboxes
OPTIONAL MATCH (dispatch:BitrixDispatchControl {source_key: $source_key})
WHERE dispatch.control_instance_id IN owned_controls
   OR dispatch.control_instance_id IS NULL
   OR NOT EXISTS {
     MATCH (:BitrixExecutionSourceBinding {
       source_key: $source_key, control_instance_id: dispatch.control_instance_id
     })
   }
WITH instance, owned_controls, active_logical, active_runs, active_streams, pending_outboxes,
     count(CASE WHEN dispatch.active_generation_id IS NOT NULL
                 OR dispatch.active_owner IS NOT NULL
                THEN dispatch END) AS active_dispatches
OPTIONAL MATCH (generation:BitrixBackfillGeneration)
WHERE generation.status IN ['allocated', 'backfilling', 'activating', 'active']
  AND (
    generation.control_instance_id IN owned_controls
    OR generation.control_instance_id IS NULL
    OR NOT EXISTS {
      MATCH (:BitrixExecutionSourceBinding {
        source_key: $source_key, control_instance_id: generation.control_instance_id
      })
    }
  )
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
WITH controls[0] AS control, sources[0] AS source
OPTIONAL MATCH (existing:BitrixExecutionSourceBinding {
  source_key: 'bitrix_chat', control_instance_id: control.source_instance_id
})
OPTIONAL MATCH (owner:BitrixSourceInstance)-[:OWNS_BITRIX_CONTROL]->(existing)
WITH control, source, collect(DISTINCT existing) AS existing_bindings,
     collect(DISTINCT owner) AS owners
WHERE size(existing_bindings) = 0
   OR (
     size(existing_bindings) = 1
     AND existing_bindings[0].source_instance_id = source.source_instance_id
     AND size(owners) = 1
     AND owners[0] = source
   )
MERGE (binding:BitrixExecutionSourceBinding {
  source_key: 'bitrix_chat', control_instance_id: control.source_instance_id
})
ON CREATE SET binding.source_instance_id = source.source_instance_id,
              binding.created_at = datetime()
WITH control, source, binding
WHERE binding.source_instance_id = source.source_instance_id
SET binding.updated_at = datetime()
MERGE (source)-[:OWNS_BITRIX_CONTROL]->(binding)
RETURN control.source_instance_id AS control_instance_id,
       source.source_instance_id AS source_instance_id
"""
