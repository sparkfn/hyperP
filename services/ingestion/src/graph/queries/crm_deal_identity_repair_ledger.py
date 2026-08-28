"""Parameterized graph-only queries for the disabled CRM repair ledger."""

from __future__ import annotations

CREATE_CRM_DEAL_REPAIR_LEDGER_SCHEMA: tuple[str, ...] = (
    "CREATE CONSTRAINT crm_deal_repair_boundary_manifest_unique IF NOT EXISTS FOR (n:RepairExecutionBoundary) REQUIRE n.manifest_digest IS UNIQUE",
    "CREATE CONSTRAINT crm_deal_repair_boundary_artifact_unique IF NOT EXISTS FOR (n:RepairExecutionBoundary) REQUIRE n.artifact_id IS UNIQUE",
    "CREATE CONSTRAINT crm_deal_repair_run_id_unique IF NOT EXISTS FOR (n:CrmDealRepairRun) REQUIRE n.run_id IS UNIQUE",
    "CREATE CONSTRAINT crm_deal_repair_run_repair_id_unique IF NOT EXISTS FOR (n:CrmDealRepairRun) REQUIRE n.repair_id IS UNIQUE",
    "CREATE CONSTRAINT crm_deal_repair_run_identity_unique IF NOT EXISTS FOR (n:CrmDealRepairRun) REQUIRE n.qualification_identity IS UNIQUE",
    "CREATE INDEX crm_deal_repair_run_status IF NOT EXISTS FOR (n:CrmDealRepairRun) ON (n.status, n.source_instance_id, n.control_instance_id)",
    "CREATE CONSTRAINT crm_deal_repair_quiescence_unique IF NOT EXISTS FOR (n:CrmDealRepairQuiescence) REQUIRE (n.run_id, n.quiescence_id) IS UNIQUE",
    "CREATE CONSTRAINT crm_deal_repair_unit_unique IF NOT EXISTS FOR (n:CrmDealRepairUnit) REQUIRE (n.run_id, n.unit_id) IS UNIQUE",
    "CREATE CONSTRAINT crm_deal_repair_checkpoint_unique IF NOT EXISTS FOR (n:CrmDealRepairCheckpoint) REQUIRE (n.run_id, n.checkpoint_id) IS UNIQUE",
    "CREATE CONSTRAINT crm_deal_repair_fence_unique IF NOT EXISTS FOR (n:CrmDealRepairFence) REQUIRE (n.run_id, n.fence_id) IS UNIQUE",
    "CREATE CONSTRAINT crm_deal_repair_mutation_unique IF NOT EXISTS FOR (n:CrmDealRepairMutationResult) REQUIRE (n.run_id, n.mutation_id) IS UNIQUE",
    "CREATE CONSTRAINT crm_deal_repair_rollback_unique IF NOT EXISTS FOR (n:CrmDealRepairRollbackImage) REQUIRE (n.run_id, n.rollback_image_id) IS UNIQUE",
    "CREATE CONSTRAINT crm_deal_repair_secondary_unique IF NOT EXISTS FOR (n:CrmDealRepairSecondaryDisposition) REQUIRE (n.run_id, n.disposition_id) IS UNIQUE",
    "CREATE CONSTRAINT crm_deal_repair_verification_unique IF NOT EXISTS FOR (n:CrmDealRepairVerification) REQUIRE (n.run_id, n.verification_id) IS UNIQUE",
    "CREATE CONSTRAINT crm_deal_repair_outbox_unique IF NOT EXISTS FOR (n:CrmDealRepairOutbox) REQUIRE (n.run_id, n.event_id) IS UNIQUE",
    "CREATE INDEX crm_deal_repair_unit_state IF NOT EXISTS FOR (n:CrmDealRepairUnit) ON (n.run_id, n.state, n.generation)",
    "CREATE INDEX crm_deal_repair_quiescence_state IF NOT EXISTS FOR (n:CrmDealRepairQuiescence) ON (n.run_id, n.state, n.generation, n.sequence)",
    "CREATE INDEX crm_deal_repair_fence_state IF NOT EXISTS FOR (n:CrmDealRepairFence) ON (n.run_id, n.state, n.generation)",
    "CREATE INDEX crm_deal_repair_checkpoint_sequence IF NOT EXISTS FOR (n:CrmDealRepairCheckpoint) ON (n.run_id, n.unit_id, n.generation, n.sequence, n.attempt)",
    "CREATE INDEX crm_deal_repair_mutation_sequence IF NOT EXISTS FOR (n:CrmDealRepairMutationResult) ON (n.run_id, n.unit_id, n.generation, n.sequence, n.attempt)",
    "CREATE INDEX crm_deal_repair_rollback_state IF NOT EXISTS FOR (n:CrmDealRepairRollbackImage) ON (n.run_id, n.unit_id, n.generation, n.state)",
    "CREATE INDEX crm_deal_repair_secondary_outcome IF NOT EXISTS FOR (n:CrmDealRepairSecondaryDisposition) ON (n.run_id, n.unit_id, n.generation, n.outcome)",
    "CREATE INDEX crm_deal_repair_verification_outcome IF NOT EXISTS FOR (n:CrmDealRepairVerification) ON (n.run_id, n.unit_id, n.generation, n.outcome)",
    "CREATE INDEX crm_deal_repair_outbox_state IF NOT EXISTS FOR (n:CrmDealRepairOutbox) ON (n.run_id, n.state, n.sequence)",
)

READ_SOURCE_RECORD_BOUNDARY = """
UNWIND $source_record_pks AS source_record_pk
OPTIONAL MATCH (record:SourceRecord {source_record_pk: source_record_pk, record_type: 'crm_deal'})
RETURN source_record_pk, record.source_record_id AS source_record_id,
       record.source_record_version AS source_record_version, record.source_version_key AS source_version_key,
       record.record_hash AS record_hash, record.lifecycle_status AS lifecycle_status,
       record.is_latest AS is_latest, record.source_instance_id AS source_instance_id
ORDER BY source_record_pk
"""

READ_INSTANCE_CONTROL_BOUNDARY = """
CALL {
  OPTIONAL MATCH (source_instance:BitrixSourceInstance {
    source_key: 'bitrix_chat', source_instance_id: $source_instance_id
  })
  OPTIONAL MATCH (source_instance)-[source_instance_of:INSTANCE_OF]->(source_system:SourceSystem)
  RETURN count(DISTINCT source_instance) AS source_registration_count,
    count(DISTINCT source_instance_of) AS source_instance_of_count,
    count(DISTINCT CASE WHEN source_system.source_key = 'bitrix_chat'
      AND source_system.is_active = true THEN source_instance_of END) AS source_active_instance_of_count,
    collect(DISTINCT source_instance.status) AS source_statuses
}
CALL {
  OPTIONAL MATCH (control_instance:BitrixSourceInstance {
    source_key: 'bitrix_chat', source_instance_id: $control_instance_id
  })
  OPTIONAL MATCH (control_instance)-[control_instance_of:INSTANCE_OF]->(control_system:SourceSystem)
  RETURN count(DISTINCT control_instance) AS control_registration_count,
    count(DISTINCT control_instance_of) AS control_instance_of_count,
    count(DISTINCT CASE WHEN control_system.source_key = 'bitrix_chat'
      AND control_system.is_active = true THEN control_instance_of END) AS control_active_instance_of_count,
    collect(DISTINCT control_instance.status) AS control_statuses
}
CALL {
  OPTIONAL MATCH (binding:BitrixExecutionSourceBinding {
    source_key: 'bitrix_chat', control_instance_id: $control_instance_id
  })
  RETURN count(DISTINCT binding) AS binding_count,
    collect(DISTINCT binding.source_instance_id) AS binding_source_instance_ids
}
CALL {
  OPTIONAL MATCH (binding:BitrixExecutionSourceBinding {
    source_key: 'bitrix_chat', control_instance_id: $control_instance_id
  })
  OPTIONAL MATCH (owner:BitrixSourceInstance)-[ownership:OWNS_BITRIX_CONTROL]->(binding)
  RETURN count(ownership) AS binding_ownership_count,
    collect(DISTINCT owner.source_instance_id) AS binding_owner_instance_ids
}
CALL {
  OPTIONAL MATCH (requested_owner:BitrixSourceInstance {
    source_key: 'bitrix_chat', source_instance_id: $source_instance_id
  })-[requested_ownership:OWNS_BITRIX_CONTROL]->(requested_binding:BitrixExecutionSourceBinding {
    source_key: 'bitrix_chat', control_instance_id: $control_instance_id
  })
  RETURN count(DISTINCT requested_binding) AS requested_binding_count,
    count(requested_ownership) AS requested_ownership_count,
    collect(DISTINCT requested_binding.source_instance_id) AS owned_binding_source_instance_ids
}
RETURN source_registration_count, source_instance_of_count, source_active_instance_of_count,
  source_statuses, control_registration_count, control_instance_of_count,
  control_active_instance_of_count, control_statuses, binding_count, binding_ownership_count,
  requested_binding_count, requested_ownership_count, binding_source_instance_ids,
  binding_owner_instance_ids, owned_binding_source_instance_ids
"""

READ_CONTROL_DISPATCH_EVIDENCE = """
MATCH (dispatch:BitrixDispatchControl {
  source_key: 'bitrix_chat', control_instance_id: $control_instance_id
})
RETURN labels(dispatch) AS labels, properties(dispatch) AS properties
"""

READ_CONTROL_NODES = """
MATCH (node)
WHERE node.control_instance_id = $control_instance_id AND (
  node:IngestRun OR node:IngestionLogicalRun OR node:IngestionCheckpoint
  OR node:BitrixIngestionStream OR node:BitrixBackfillGeneration
  OR node:BitrixKnownOwnerRefreshSet OR node:BitrixKnownOwnerRefreshMember
  OR node:BitrixBackfillCoverage OR node:BitrixActivityOwnerRetry
  OR node:BitrixBackfillDispatchOutbox OR node:StageHistoryUnit
  OR node:StageHistoryOccurrence OR node:StageHistoryRetry
  OR node:StageHistoryReviewCommand OR node:StageHistoryUnitAccounting
)
RETURN labels(node) AS labels, properties(node) AS properties
"""

READ_CONTROL_RELATIONSHIPS = """
MATCH (left {control_instance_id: $control_instance_id})-[relationship]->(
  right {control_instance_id: $control_instance_id}
)
WHERE (left:IngestionLogicalRun AND relationship:HAS_ATTEMPT AND right:IngestRun)
  OR (left:IngestionLogicalRun AND relationship:ACTIVE_ATTEMPT AND right:IngestRun)
  OR (left:IngestionCheckpoint AND relationship:CHECKPOINT_FOR AND right:IngestionLogicalRun)
  OR (left:IngestionCheckpoint AND relationship:PRODUCED_BY AND right:IngestRun)
  OR (left:BitrixBackfillGeneration AND relationship:HAS_LOGICAL_RUN
      AND right:IngestionLogicalRun)
  OR (left:BitrixBackfillGeneration AND relationship:HAS_STREAM
      AND right:BitrixIngestionStream)
  OR (left:BitrixBackfillGeneration AND relationship:HAS_KNOWN_OWNER_SET
      AND right:BitrixKnownOwnerRefreshSet)
  OR (left:BitrixBackfillGeneration AND relationship:HAS_COVERAGE
      AND right:BitrixBackfillCoverage)
  OR (left:BitrixBackfillGeneration AND relationship:HAS_OWNER_RETRY
      AND right:BitrixActivityOwnerRetry)
  OR (left:BitrixKnownOwnerRefreshSet AND relationship:HAS_MEMBER
      AND right:BitrixKnownOwnerRefreshMember)
  OR (left:BitrixBackfillGeneration AND relationship:HAS_SUCCESSOR
      AND right:BitrixBackfillGeneration)
  OR (left:IngestionLogicalRun AND relationship:HAS_STAGE_HISTORY_UNIT
      AND right:StageHistoryUnit)
  OR (left:IngestionLogicalRun AND relationship:HAS_STAGE_HISTORY_REVIEW_COMMAND
      AND right:StageHistoryReviewCommand)
  OR (left:StageHistoryUnit AND relationship:CONTAINS_STAGE_HISTORY_OCCURRENCE
      AND right:StageHistoryOccurrence)
  OR (left:StageHistoryUnit AND relationship:HAS_STAGE_HISTORY_ACCOUNTING
      AND right:StageHistoryUnitAccounting)
  OR (left:StageHistoryOccurrence AND relationship:HAS_STAGE_HISTORY_RETRY
      AND right:StageHistoryRetry)
RETURN labels(left) AS left_labels, properties(left) AS left_properties,
  type(relationship) AS relationship_type, properties(relationship) AS relationship_properties,
  labels(right) AS right_labels, properties(right) AS right_properties
"""

READ_STALE_RUN_CONTROL_EVIDENCE = """
OPTIONAL MATCH (run:IngestRun {ingest_run_id: $stale_run_id})
OPTIONAL MATCH (run)-[relationship:FROM_SOURCE]->(source:SourceSystem)
RETURN CASE WHEN run IS NULL THEN 'absent' ELSE 'present' END AS stale_run_state,
  labels(run) AS left_labels, properties(run) AS left_properties,
  type(relationship) AS relationship_type, properties(relationship) AS relationship_properties,
  labels(source) AS right_labels, properties(source) AS right_properties
"""

READ_STALE_RUN_ASSOCIATIONS = """
MATCH (run:IngestRun {ingest_run_id: $stale_run_id})
MATCH (logical:IngestionLogicalRun)-[relationship:HAS_ATTEMPT|ACTIVE_ATTEMPT]->(run)
RETURN 'logical_attempt' AS association_kind, labels(logical) AS left_labels,
  properties(logical) AS left_properties, type(relationship) AS relationship_type,
  properties(relationship) AS relationship_properties, labels(run) AS right_labels,
  properties(run) AS right_properties
UNION ALL
MATCH (run:IngestRun {ingest_run_id: $stale_run_id})
MATCH (logical:IngestionLogicalRun)-[:HAS_ATTEMPT|ACTIVE_ATTEMPT]->(run)
MATCH (checkpoint:IngestionCheckpoint)-[relationship:CHECKPOINT_FOR]->(logical)
RETURN 'checkpoint_for' AS association_kind, labels(checkpoint) AS left_labels,
  properties(checkpoint) AS left_properties, type(relationship) AS relationship_type,
  properties(relationship) AS relationship_properties, labels(logical) AS right_labels,
  properties(logical) AS right_properties
UNION ALL
MATCH (run:IngestRun {ingest_run_id: $stale_run_id})
MATCH (checkpoint:IngestionCheckpoint)-[relationship:PRODUCED_BY]->(run)
RETURN 'checkpoint_produced_by' AS association_kind, labels(checkpoint) AS left_labels,
  properties(checkpoint) AS left_properties, type(relationship) AS relationship_type,
  properties(relationship) AS relationship_properties, labels(run) AS right_labels,
  properties(run) AS right_properties
UNION ALL
MATCH (run:IngestRun {ingest_run_id: $stale_run_id})
MATCH (logical:IngestionLogicalRun)-[:HAS_ATTEMPT|ACTIVE_ATTEMPT]->(run)
MATCH (stream:BitrixIngestionStream {
  logical_run_id: logical.logical_run_id, control_instance_id: logical.control_instance_id
})
RETURN 'logical_stream' AS association_kind, labels(logical) AS left_labels,
  properties(logical) AS left_properties, NULL AS relationship_type,
  NULL AS relationship_properties, labels(stream) AS right_labels,
  properties(stream) AS right_properties
"""

QUALIFY_REPAIR_RUN = """
MERGE (run:CrmDealRepairRun {repair_id: $repair_id})
ON CREATE SET run.run_id = $run_id, run.qualification_identity = $qualification_identity,
  run.manifest_digest = $manifest_digest, run.artifact_id = $artifact_id,
  run.artifact_manifest_hmac = $artifact_manifest_hmac, run.inventory_digest = $inventory_digest,
  run.boundary_digest = $boundary_digest, run.source_instance_id = $source_instance_id,
  run.control_instance_id = $control_instance_id, run.source_record_pks_json = $source_record_pks_json,
  run.manifest_json = $manifest_json, run.inventory_row_count = $inventory_row_count,
  run.eligible_unit_count = $eligible_unit_count,
  run.negative_control_count = $negative_control_count,
  run.execution_allowed = $execution_allowed,
  run.status = 'qualified', run.created_at = datetime()
WITH run
WHERE run.qualification_identity = $qualification_identity
  AND run.manifest_digest = $manifest_digest AND run.artifact_id = $artifact_id
  AND run.artifact_manifest_hmac = $artifact_manifest_hmac
  AND run.inventory_digest = $inventory_digest AND run.boundary_digest = $boundary_digest
  AND run.source_instance_id = $source_instance_id AND run.control_instance_id = $control_instance_id
  AND run.source_record_pks_json = $source_record_pks_json AND run.manifest_json = $manifest_json
  AND run.inventory_row_count = $inventory_row_count
  AND run.eligible_unit_count = $eligible_unit_count
  AND run.negative_control_count = $negative_control_count AND run.status = 'qualified'
  AND run.execution_allowed = false
MERGE (boundary:RepairExecutionBoundary {manifest_digest: $manifest_digest})
ON CREATE SET boundary.artifact_id = $artifact_id, boundary.artifact_manifest_hmac = $artifact_manifest_hmac,
  boundary.inventory_digest = $inventory_digest, boundary.boundary_digest = $boundary_digest,
  boundary.source_instance_id = $source_instance_id, boundary.control_instance_id = $control_instance_id,
  boundary.source_record_pks_json = $source_record_pks_json, boundary.manifest_json = $manifest_json,
  boundary.inventory_row_count = $inventory_row_count,
  boundary.eligible_unit_count = $eligible_unit_count,
  boundary.negative_control_count = $negative_control_count,
  boundary.execution_allowed = $execution_allowed, boundary.created_at = datetime()
WITH run, boundary
WHERE boundary.artifact_id = $artifact_id AND boundary.artifact_manifest_hmac = $artifact_manifest_hmac
  AND boundary.inventory_digest = $inventory_digest AND boundary.boundary_digest = $boundary_digest
  AND boundary.source_instance_id = $source_instance_id AND boundary.control_instance_id = $control_instance_id
  AND boundary.source_record_pks_json = $source_record_pks_json
  AND boundary.manifest_json = $manifest_json
  AND boundary.inventory_row_count = $inventory_row_count
  AND boundary.eligible_unit_count = $eligible_unit_count
  AND boundary.negative_control_count = $negative_control_count
  AND boundary.execution_allowed = false
MERGE (run)-[:QUALIFIED_WITH]->(boundary)
RETURN run.run_id AS run_id, run.status AS status
"""

GET_REPAIR_RUN = """
MATCH (run:CrmDealRepairRun {repair_id: $repair_id})
OPTIONAL MATCH (run)-[qualification:QUALIFIED_WITH]->(boundary:RepairExecutionBoundary)
WITH run, count(qualification) AS qualification_link_count,
  collect(CASE WHEN boundary IS NULL THEN NULL ELSE {
    manifest_digest: boundary.manifest_digest,
    artifact_id: boundary.artifact_id,
    artifact_manifest_hmac: boundary.artifact_manifest_hmac,
    inventory_digest: boundary.inventory_digest,
    boundary_digest: boundary.boundary_digest,
    source_instance_id: boundary.source_instance_id,
    control_instance_id: boundary.control_instance_id,
    source_record_pks_json: boundary.source_record_pks_json,
    manifest_json: boundary.manifest_json,
    inventory_row_count: boundary.inventory_row_count,
    eligible_unit_count: boundary.eligible_unit_count,
    negative_control_count: boundary.negative_control_count,
    execution_allowed: boundary.execution_allowed
  } END) AS boundaries
RETURN run.run_id AS run_id, run.manifest_digest AS manifest_digest, run.artifact_id AS artifact_id,
  run.qualification_identity AS qualification_identity,
  run.artifact_manifest_hmac AS artifact_manifest_hmac, run.inventory_digest AS inventory_digest,
  run.boundary_digest AS boundary_digest, run.source_instance_id AS source_instance_id,
  run.control_instance_id AS control_instance_id, run.status AS status,
  run.source_record_pks_json AS source_record_pks_json, run.manifest_json AS manifest_json,
  run.inventory_row_count AS inventory_row_count, run.eligible_unit_count AS eligible_unit_count,
  run.negative_control_count AS negative_control_count,
  run.execution_allowed AS execution_allowed,
  qualification_link_count, boundaries
"""
