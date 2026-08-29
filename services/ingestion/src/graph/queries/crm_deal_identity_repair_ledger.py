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
    "CREATE CONSTRAINT crm_deal_repair_control_run_unique IF NOT EXISTS FOR (n:CrmDealRepairControl) REQUIRE n.run_id IS UNIQUE",
    "CREATE CONSTRAINT crm_deal_repair_allocation_completion_unique IF NOT EXISTS FOR (n:CrmDealRepairAllocationCompletion) REQUIRE n.run_id IS UNIQUE",
    "CREATE CONSTRAINT crm_deal_repair_qualified_row_unique IF NOT EXISTS FOR (n:CrmDealRepairQualifiedInventoryRow) REQUIRE (n.run_id, n.inventory_key) IS UNIQUE",
    "CREATE CONSTRAINT crm_deal_repair_authorization_proof_unique IF NOT EXISTS FOR (n:CrmDealRepairAuthorizationProof) REQUIRE (n.run_id, n.operation, n.revision) IS UNIQUE",
    "CREATE CONSTRAINT crm_deal_repair_publication_reservation_unique IF NOT EXISTS FOR (n:BitrixRepairPublicationReservation) REQUIRE (n.control_instance_id, n.routing_identity_digest, n.occurrence_generation_identity) IS UNIQUE",
    "CREATE CONSTRAINT crm_deal_repair_publication_token_unique IF NOT EXISTS FOR (n:BitrixRepairPublicationReservation) REQUIRE n.reservation_token IS UNIQUE",
    "CREATE INDEX crm_deal_repair_publication_reservation_state IF NOT EXISTS FOR (n:BitrixRepairPublicationReservation) ON (n.control_instance_id, n.status)",
    "CREATE INDEX crm_deal_repair_control_state IF NOT EXISTS FOR (n:CrmDealRepairControl) ON (n.state, n.revision)",
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


PERSIST_QUALIFIED_INVENTORY_ROWS = """
MATCH (run:CrmDealRepairRun {run_id: $run_id, status: 'qualified', execution_allowed: false})
UNWIND $rows AS row
MERGE (qualified:CrmDealRepairQualifiedInventoryRow {run_id: $run_id, inventory_key: row.inventory_key})
ON CREATE SET qualified.source_record_pk = row.source_record_pk,
  qualified.inventory_fingerprint = row.inventory_fingerprint, qualified.execution_allowed = false,
  qualified.created_at = datetime()
WITH qualified, row
WHERE qualified.source_record_pk = row.source_record_pk
  AND qualified.inventory_fingerprint = row.inventory_fingerprint
  AND qualified.execution_allowed = false
RETURN count(qualified) AS row_count
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

RESERVE_REPAIR_PUBLICATION = """
OPTIONAL MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: $control_instance_id})
WITH dispatch
WHERE dispatch IS NULL OR (
  dispatch.blocked = false AND dispatch.repair_control_run_id IS NULL
  AND dispatch.repair_control_owner_id IS NULL AND dispatch.repair_control_token IS NULL
  AND dispatch.repair_control_revision IS NULL AND dispatch.repair_control_state IS NULL
)
MERGE (reservation:BitrixRepairPublicationReservation {
  control_instance_id: $control_instance_id,
  routing_identity_digest: $routing_identity_digest,
  occurrence_generation_identity: $occurrence_generation_identity
})
ON CREATE SET reservation.reservation_token = $reservation_token,
  reservation.stream_scope = $stream_scope, reservation.status = 'pending',
  reservation.execution_allowed = false, reservation.created_at = datetime()
WITH reservation
WHERE reservation.stream_scope = $stream_scope
  AND reservation.execution_allowed = false
  AND (
    reservation.reservation_token = $reservation_token
    OR reservation.status IN ['pending', 'publishing', 'published']
  )
RETURN reservation.reservation_token AS reservation_token, reservation.status AS status,
  reservation.canvas_or_workflow_id AS publication_id,
  reservation.reservation_token <> $reservation_token AS is_exact_replay
"""

BEGIN_REPAIR_PUBLICATION = """
MATCH (reservation:BitrixRepairPublicationReservation {control_instance_id: $control_instance_id,
  reservation_token: $reservation_token, routing_identity_digest: $routing_identity_digest,
  occurrence_generation_identity: $occurrence_generation_identity, stream_scope: $stream_scope,
  status: 'pending', execution_allowed: false})
OPTIONAL MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: $control_instance_id})
WITH reservation, dispatch
WHERE dispatch IS NULL OR (
  dispatch.blocked = false AND dispatch.repair_control_run_id IS NULL
  AND dispatch.repair_control_owner_id IS NULL AND dispatch.repair_control_token IS NULL
  AND dispatch.repair_control_revision IS NULL AND dispatch.repair_control_state IS NULL
)
SET reservation.status = 'publishing', reservation.publishing_at = datetime()
RETURN reservation.reservation_token AS reservation_token
"""

PUBLISH_REPAIR_PUBLICATION = """
MATCH (reservation:BitrixRepairPublicationReservation {control_instance_id: $control_instance_id,
  reservation_token: $reservation_token, status: 'publishing', execution_allowed: false})
SET reservation.status = 'published', reservation.canvas_or_workflow_id = $publication_id,
  reservation.published_at = datetime()
RETURN reservation.reservation_token AS reservation_token
"""

READ_REPAIR_PUBLICATION_RESERVATION = """
MATCH (reservation:BitrixRepairPublicationReservation {control_instance_id: $control_instance_id,
  reservation_token: $reservation_token, execution_allowed: false})
RETURN reservation.status AS status, reservation.canvas_or_workflow_id AS publication_id
"""

READ_REPAIR_PUBLICATION_RESERVATION_BY_IDENTITY = """
MATCH (reservation:BitrixRepairPublicationReservation {control_instance_id: $control_instance_id,
  routing_identity_digest: $routing_identity_digest,
  occurrence_generation_identity: $occurrence_generation_identity, execution_allowed: false})
RETURN reservation.reservation_token AS reservation_token, reservation.stream_scope AS stream_scope,
  reservation.status AS status, reservation.canvas_or_workflow_id AS publication_id
"""

GET_REPAIR_DISPATCH_BLOCK = """
MATCH (dispatch:BitrixDispatchControl {
  source_key: 'bitrix_chat', control_instance_id: $control_instance_id, blocked: true
})
WHERE dispatch.repair_control_state IN ['quiescing', 'quiesced', 'allocated', 'paused']
RETURN dispatch.repair_control_run_id AS run_id
"""

READ_REPAIR_CONTROL = """
MATCH (run:CrmDealRepairRun {run_id: $run_id, status: 'qualified', execution_allowed: false})
OPTIONAL MATCH (control:CrmDealRepairControl {run_id: $run_id})
OPTIONAL MATCH (completion:CrmDealRepairAllocationCompletion {run_id: $run_id})
OPTIONAL MATCH (unit:CrmDealRepairUnit {run_id: $run_id})
RETURN run.boundary_digest AS boundary_digest, control.owner_id AS owner_id,
  control.token AS token, control.revision AS revision, control.state AS state,
  control.prior_state AS prior_state, completion.allocation_digest AS allocation_digest,
  completion.unit_count AS completion_unit_count, count(unit) AS persisted_unit_count
"""

PERSIST_REPAIR_BOUNDARY_COMPONENT_PROOF = """
MATCH (run:CrmDealRepairRun {run_id: $run_id, status: 'qualified', boundary_digest: $boundary_digest,
  execution_allowed: false})
MATCH (control:CrmDealRepairControl {run_id: $run_id, owner_id: $owner_id, token: $token,
  revision: $expected_revision, boundary_digest: $boundary_digest})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: run.control_instance_id, blocked: true, repair_control_run_id: $run_id,
  repair_control_owner_id: $owner_id, repair_control_token: $token,
  repair_control_revision: $expected_revision})
WHERE (control.baseline_inventory_digest IS NULL OR (
  control.baseline_source_instance_id = $baseline_source_instance_id
  AND control.baseline_control_instance_id = $baseline_control_instance_id
  AND control.baseline_inventory_digest = $baseline_inventory_digest
  AND control.baseline_inventory_row_count = $baseline_inventory_row_count
  AND control.baseline_eligible_unit_count = $baseline_eligible_unit_count
  AND control.baseline_negative_control_count = $baseline_negative_control_count
  AND control.baseline_source_records_digest = $baseline_source_records_digest
  AND control.baseline_source_instance_digest = $baseline_source_instance_digest
))
SET control.baseline_source_instance_id = $baseline_source_instance_id,
  control.baseline_control_instance_id = $baseline_control_instance_id,
  control.baseline_inventory_digest = $baseline_inventory_digest,
  control.baseline_inventory_row_count = $baseline_inventory_row_count,
  control.baseline_eligible_unit_count = $baseline_eligible_unit_count,
  control.baseline_negative_control_count = $baseline_negative_control_count,
  control.baseline_source_records_digest = $baseline_source_records_digest,
  control.baseline_source_instance_digest = $baseline_source_instance_digest,
  control.baseline_control_digest = coalesce(control.baseline_control_digest, $baseline_control_digest),
  control.baseline_stale_run_evidence_digest = coalesce(
    control.baseline_stale_run_evidence_digest, $baseline_stale_run_evidence_digest
  ),
  control.authorized_control_digest = $authorized_control_digest,
  control.authorized_stale_run_evidence_digest = $authorized_stale_run_evidence_digest,
  control.boundary_proof_updated_at = datetime()
RETURN control.run_id AS run_id
"""

PERSIST_REPAIR_TRANSACTION_AUTHORIZATION = """
MATCH (control:CrmDealRepairControl {run_id: $run_id, owner_id: $owner_id, token: $token,
  revision: $revision, state: $state, boundary_digest: $boundary_digest})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: $control_instance_id, blocked: true, repair_control_run_id: $run_id,
  repair_control_owner_id: $owner_id, repair_control_token: $token,
  repair_control_revision: $revision, repair_control_state: $state,
  block_reason: 'crm_deal_repair_quiescence'})
MERGE (proof:CrmDealRepairAuthorizationProof {
  run_id: $run_id, operation: $operation, revision: $revision
})
ON CREATE SET proof.authorization_digest = $authorization_digest,
  proof.pre_control_digest = $pre_control_digest,
  proof.post_control_digest = $post_control_digest,
  proof.pre_stale_run_evidence_digest = $pre_stale_run_evidence_digest,
  proof.post_stale_run_evidence_digest = $post_stale_run_evidence_digest,
  proof.operation_capture_digest = $operation_capture_digest,
  proof.execution_allowed = false, proof.created_at = datetime()
WITH control, proof
WHERE proof.authorization_digest = $authorization_digest
  AND proof.pre_control_digest = $pre_control_digest
  AND proof.post_control_digest = $post_control_digest
  AND proof.pre_stale_run_evidence_digest = $pre_stale_run_evidence_digest
  AND proof.post_stale_run_evidence_digest = $post_stale_run_evidence_digest
  AND proof.operation_capture_digest = $operation_capture_digest
  AND proof.execution_allowed = false
SET control.last_authorization_operation = $operation,
  control.last_authorization_digest = $authorization_digest,
  control.last_authorization_pre_control_digest = $pre_control_digest,
  control.last_authorization_post_control_digest = $post_control_digest,
  control.last_authorization_pre_stale_digest = $pre_stale_run_evidence_digest,
  control.last_authorization_post_stale_digest = $post_stale_run_evidence_digest
RETURN proof.run_id AS run_id
"""

READ_REPAIR_BOUNDARY_COMPONENT_PROOF = """
MATCH (control:CrmDealRepairControl {run_id: $run_id})
WHERE control.baseline_inventory_digest IS NOT NULL
RETURN control.baseline_source_instance_id AS baseline_source_instance_id,
  control.baseline_control_instance_id AS baseline_control_instance_id,
  control.baseline_inventory_digest AS baseline_inventory_digest,
  control.baseline_inventory_row_count AS baseline_inventory_row_count,
  control.baseline_eligible_unit_count AS baseline_eligible_unit_count,
  control.baseline_negative_control_count AS baseline_negative_control_count,
  control.baseline_source_records_digest AS baseline_source_records_digest,
  control.baseline_source_instance_digest AS baseline_source_instance_digest,
  control.baseline_control_digest AS baseline_control_digest,
  control.baseline_stale_run_evidence_digest AS baseline_stale_run_evidence_digest,
  control.authorized_control_digest AS authorized_control_digest,
  control.authorized_stale_run_evidence_digest AS authorized_stale_run_evidence_digest
"""

RECORD_REPAIR_TASK_PROOF = """
MATCH (run:CrmDealRepairRun {run_id: $run_id, status: 'qualified', boundary_digest: $boundary_digest,
  execution_allowed: false})
MATCH (control:CrmDealRepairControl {run_id: $run_id, owner_id: $owner_id, token: $token,
  revision: $expected_revision, boundary_digest: $boundary_digest})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: run.control_instance_id, blocked: true, repair_control_run_id: $run_id,
  repair_control_owner_id: $owner_id, repair_control_token: $token,
  repair_control_revision: $expected_revision})
SET control.task_proof_state = $proof_state, control.stop_reason = $stop_reason,
  control.task_proof_updated_at = datetime()
RETURN control.run_id AS run_id
"""

READ_REPAIR_CONTROL_STATUS = """
MATCH (run:CrmDealRepairRun {run_id: $run_id, status: 'qualified', execution_allowed: false})
OPTIONAL MATCH (control:CrmDealRepairControl {run_id: $run_id})
OPTIONAL MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: run.control_instance_id})
OPTIONAL MATCH (completion:CrmDealRepairAllocationCompletion {run_id: $run_id})
CALL {
  WITH run
  OPTIONAL MATCH (unit:CrmDealRepairUnit {run_id: run.run_id})
  RETURN count(unit) AS allocation_unit_count
}
CALL {
  WITH run
  OPTIONAL MATCH (node {control_instance_id: run.control_instance_id})
  WHERE (node:IngestionLogicalRun OR node:IngestRun OR node:IngestionCheckpoint
    OR node:BitrixIngestionStream OR node:BitrixBackfillGeneration
    OR node:BitrixBackfillDispatchOutbox)
    AND (node.stream_key IN ['crm_deals', 'crm_activities', 'openlines_conversations']
      OR node.bitrix_stream_key IN ['crm_deals', 'crm_activities', 'openlines_conversations']
      OR node.entity_key IN ['crm_deals', 'crm_activities', 'openlines_conversations'])
  RETURN count(CASE WHEN node.status IN ['active', 'running', 'started', 'queued', 'pending',
      'backfilling'] THEN node END) AS topology_active_count,
    count(CASE WHEN node.status IN ['superseded', 'failed'] THEN node END) AS topology_superseded_count,
    head(collect(CASE WHEN node.stop_reason IS NOT NULL THEN node.stop_reason END)) AS stop_reason
}
CALL {
  WITH run
  OPTIONAL MATCH (stale:IngestRun {control_instance_id: run.control_instance_id,
    failure_category: 'crm_deal_repair_stale_run'})
  RETURN count(stale) AS stale_run_proof_count
}
RETURN run.run_id AS run_id, run.boundary_digest AS boundary_digest, control.owner_id AS owner_id,
  control.revision AS revision, control.state AS state, control.prior_state AS prior_state,
  completion.allocation_digest AS allocation_digest, completion.unit_count AS completion_unit_count,
  allocation_unit_count, dispatch.blocked AS dispatch_blocked,
  dispatch.repair_control_owner_id AS dispatch_owner_id, topology_active_count,
  topology_superseded_count, stale_run_proof_count, control.task_proof_state AS task_proof_state,
  coalesce(control.stop_reason, stop_reason) AS stop_reason
"""

INVENTORY_STALE_REPAIR_RUN_PROOF = """
MATCH (run:CrmDealRepairRun {run_id: $run_id, status: 'qualified', execution_allowed: false})
MATCH (control:CrmDealRepairControl {run_id: $run_id, owner_id: $owner_id, token: $token,
  revision: $expected_revision, boundary_digest: $boundary_digest})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: run.control_instance_id, blocked: true, repair_control_run_id: $run_id,
  repair_control_owner_id: $owner_id, repair_control_token: $token,
  repair_control_revision: $expected_revision})
MATCH (stale:IngestRun {ingest_run_id: $stale_run_id, control_instance_id: run.control_instance_id,
  source_key: 'bitrix_chat'})-[:FROM_SOURCE]->(:SourceSystem {source_key: 'bitrix_chat'})
CALL {
  WITH stale
  OPTIONAL MATCH (logical:IngestionLogicalRun)-[:HAS_ATTEMPT|ACTIVE_ATTEMPT]->(stale)
  RETURN [item IN collect(DISTINCT logical.logical_run_id) WHERE item IS NOT NULL | item]
    AS logical_run_ids
}
CALL {
  WITH stale
  OPTIONAL MATCH (checkpoint:IngestionCheckpoint)-[:PRODUCED_BY]->(stale)
  RETURN [item IN collect(DISTINCT checkpoint.logical_run_id + '|' + checkpoint.phase + '|'
    + toString(checkpoint.generation)) WHERE item IS NOT NULL | item] AS produced_checkpoint_ids
}
CALL {
  WITH stale
  OPTIONAL MATCH (logical:IngestionLogicalRun)-[:HAS_ATTEMPT|ACTIVE_ATTEMPT]->(stale)
  OPTIONAL MATCH (checkpoint:IngestionCheckpoint)-[:CHECKPOINT_FOR]->(logical)
  RETURN [item IN collect(DISTINCT checkpoint.logical_run_id + '|' + checkpoint.phase + '|'
    + toString(checkpoint.generation)) WHERE item IS NOT NULL | item] AS logical_checkpoint_ids
}
CALL {
  WITH stale
  OPTIONAL MATCH (logical:IngestionLogicalRun)-[:HAS_ATTEMPT|ACTIVE_ATTEMPT]->(stale)
  OPTIONAL MATCH (stream:BitrixIngestionStream {control_instance_id: logical.control_instance_id,
    logical_run_id: logical.logical_run_id, ingest_run_id: stale.ingest_run_id})
  RETURN [item IN collect(DISTINCT stream.stream_key) WHERE item IS NOT NULL | item] AS stream_keys
}
WITH stale, logical_run_ids, produced_checkpoint_ids, logical_checkpoint_ids, stream_keys
RETURN stale.ingest_run_id AS ingest_run_id, stale.control_instance_id AS control_instance_id,
  stale.source_key AS source_key, stale.status AS status, logical_run_ids,
  CASE WHEN size(produced_checkpoint_ids) = 0 THEN logical_checkpoint_ids
    WHEN size(logical_checkpoint_ids) = 0 THEN produced_checkpoint_ids ELSE [] END AS checkpoint_ids,
  stream_keys, size(produced_checkpoint_ids) AS produced_checkpoint_count,
  size(logical_checkpoint_ids) AS logical_checkpoint_count
"""

INVENTORY_REPAIR_TOPOLOGY = """
MATCH (run:CrmDealRepairRun {run_id: $run_id, status: 'qualified', execution_allowed: false})
MATCH (control:CrmDealRepairControl {
  run_id: $run_id, owner_id: $owner_id, token: $token, revision: $expected_revision,
  state: 'quiescing', boundary_digest: $boundary_digest
})
MATCH (dispatch:BitrixDispatchControl {
  source_key: 'bitrix_chat', control_instance_id: run.control_instance_id,
  repair_control_run_id: $run_id, repair_control_owner_id: $owner_id,
  repair_control_token: $token, repair_control_revision: $expected_revision,
  repair_control_state: 'quiescing', blocked: true
})
CALL {
  WITH run
  OPTIONAL MATCH (reservation:BitrixRepairPublicationReservation {
    control_instance_id: run.control_instance_id
  })
  WHERE reservation.status IN ['pending', 'publishing', 'published']
  RETURN collect(CASE WHEN reservation IS NULL THEN NULL ELSE {
    reservation_token: reservation.reservation_token, status: reservation.status,
    routing_identity_digest: reservation.routing_identity_digest,
    occurrence_generation_identity: reservation.occurrence_generation_identity
  } END) AS publication_reservations,
    count(CASE WHEN reservation.status IN ['pending', 'publishing'] THEN reservation END)
      AS uncertain_reservation_count
}
WITH run, control, dispatch, publication_reservations, uncertain_reservation_count
WHERE uncertain_reservation_count = 0
CALL {
  WITH run
  OPTIONAL MATCH (logical:IngestionLogicalRun {control_instance_id: run.control_instance_id})
  WHERE logical.source_key = 'bitrix_chat'
    AND coalesce(logical.bitrix_stream_key, logical.entity_key) IN
      ['crm_deals', 'crm_activities', 'openlines_conversations']
  RETURN collect(DISTINCT CASE WHEN logical IS NULL THEN NULL ELSE {
    logical_run_id: logical.logical_run_id, status: logical.status
  } END) AS logical_run_ids
}
CALL {
  WITH run
  OPTIONAL MATCH (logical:IngestionLogicalRun {control_instance_id: run.control_instance_id})
    -[:HAS_ATTEMPT|ACTIVE_ATTEMPT]->
    (attempt:IngestRun {control_instance_id: run.control_instance_id})
  WHERE logical.source_key = 'bitrix_chat'
    AND coalesce(logical.bitrix_stream_key, logical.entity_key) IN
      ['crm_deals', 'crm_activities', 'openlines_conversations']
  RETURN collect(DISTINCT CASE WHEN attempt IS NULL THEN NULL ELSE {
    ingest_run_id: attempt.ingest_run_id, status: attempt.status, generation: attempt.generation
  } END) AS ingest_run_ids
}
CALL {
  WITH run
  OPTIONAL MATCH (checkpoint:IngestionCheckpoint {control_instance_id: run.control_instance_id})
  WHERE EXISTS {
    MATCH (logical:IngestionLogicalRun {
      control_instance_id: run.control_instance_id, logical_run_id: checkpoint.logical_run_id
    })
    WHERE logical.source_key = 'bitrix_chat'
      AND coalesce(logical.bitrix_stream_key, logical.entity_key) IN
        ['crm_deals', 'crm_activities', 'openlines_conversations']
  }
  RETURN collect(DISTINCT CASE WHEN checkpoint IS NULL THEN NULL ELSE {
    logical_run_id: checkpoint.logical_run_id, phase: checkpoint.phase,
    generation: checkpoint.generation, status: checkpoint.status
  } END) AS checkpoint_ids
}
CALL {
  WITH run
  OPTIONAL MATCH (stream:BitrixIngestionStream {
    source_key: 'bitrix_chat', control_instance_id: run.control_instance_id
  })
  WHERE stream.stream_key IN ['crm_deals', 'crm_activities', 'openlines_conversations']
  RETURN collect(DISTINCT CASE WHEN stream IS NULL THEN NULL ELSE {
    stream_key: stream.stream_key, logical_run_id: stream.logical_run_id,
    ingest_run_id: stream.ingest_run_id, attempt_generation: stream.attempt_generation,
    stream_generation: stream.stream_generation, fencing_token: stream.fencing_token,
    status: stream.status
  } END) AS stream_ids
}
CALL {
  WITH run
  OPTIONAL MATCH (generation:BitrixBackfillGeneration {control_instance_id: run.control_instance_id})
  WHERE EXISTS {
    MATCH (generation)-[:HAS_LOGICAL_RUN]->(logical:IngestionLogicalRun {
      control_instance_id: run.control_instance_id
    })
    WHERE logical.source_key = 'bitrix_chat'
      AND coalesce(logical.bitrix_stream_key, logical.entity_key) IN
        ['crm_deals', 'crm_activities', 'openlines_conversations']
  }
  OR EXISTS {
    MATCH (generation)-[:HAS_STREAM]->(stream:BitrixIngestionStream {
      source_key: 'bitrix_chat', control_instance_id: run.control_instance_id
    })
    WHERE stream.stream_key IN ['crm_deals', 'crm_activities', 'openlines_conversations']
  }
  RETURN collect(DISTINCT CASE WHEN generation IS NULL THEN NULL ELSE {
    generation_id: generation.generation_id, status: generation.status
  } END) AS generation_ids
}
CALL {
  WITH run
  OPTIONAL MATCH (publication:BitrixBackfillDispatchOutbox {
    control_instance_id: run.control_instance_id
  })
  WHERE EXISTS {
    MATCH (generation:BitrixBackfillGeneration {
      control_instance_id: run.control_instance_id,
      generation_id: publication.successor_generation_id
    })-[:HAS_STREAM]->(stream:BitrixIngestionStream {
      source_key: 'bitrix_chat', control_instance_id: run.control_instance_id
    })
    WHERE stream.stream_key IN ['crm_deals', 'crm_activities', 'openlines_conversations']
  }
  RETURN collect(DISTINCT CASE WHEN publication IS NULL THEN NULL ELSE {
    successor_generation_id: publication.successor_generation_id,
    evidence_digest: publication.evidence_digest, occurrence: publication.occurrence,
    status: publication.status
  } END) AS publication_ids
}
RETURN logical_run_ids, ingest_run_ids, checkpoint_ids, stream_ids, generation_ids, publication_ids,
  publication_reservations
"""

SUPERSEDE_CAPTURED_REPAIR_TOPOLOGY = """
MATCH (run:CrmDealRepairRun {run_id: $run_id, status: 'qualified', boundary_digest: $boundary_digest,
  execution_allowed: false})
MATCH (control:CrmDealRepairControl {run_id: $run_id, owner_id: $owner_id, token: $token,
  revision: $expected_revision, state: 'quiescing', boundary_digest: $boundary_digest})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat', control_instance_id: run.control_instance_id,
  repair_control_run_id: $run_id, repair_control_owner_id: $owner_id,
  repair_control_token: $token, repair_control_revision: $expected_revision,
  repair_control_state: 'quiescing', blocked: true})
CALL {
  WITH run
  OPTIONAL MATCH (reservation:BitrixRepairPublicationReservation {
    control_instance_id: run.control_instance_id
  })
  WHERE reservation.status IN ['pending', 'publishing']
  RETURN count(reservation) AS uncertain_reservation_count
}
WITH run, control, dispatch, uncertain_reservation_count
WHERE uncertain_reservation_count = 0
CALL {
  WITH run
  UNWIND CASE WHEN size($logical_run_ids) = 0 THEN [NULL] ELSE $logical_run_ids END AS captured
  OPTIONAL MATCH (logical:IngestionLogicalRun {
    control_instance_id: run.control_instance_id, logical_run_id: captured.logical_run_id
  })
  WHERE captured IS NOT NULL
    AND logical.source_key = 'bitrix_chat'
    AND coalesce(logical.bitrix_stream_key, logical.entity_key) IN
      ['crm_deals', 'crm_activities', 'openlines_conversations']
    AND logical.status = captured.status
    AND logical.status IN ['active', 'running', 'started', 'queued', 'pending', 'backfilling']
  RETURN count(DISTINCT logical) AS logical_count
}
CALL {
  WITH run
  UNWIND CASE WHEN size($ingest_run_ids) = 0 THEN [NULL] ELSE $ingest_run_ids END AS captured
  OPTIONAL MATCH (attempt:IngestRun {
    control_instance_id: run.control_instance_id, ingest_run_id: captured.ingest_run_id
  })
  WHERE captured IS NOT NULL
    AND attempt.status = captured.status
    AND attempt.generation = captured.generation
    AND attempt.status IN ['active', 'running', 'started', 'queued', 'pending', 'paused']
    AND EXISTS {
      MATCH (logical:IngestionLogicalRun {control_instance_id: run.control_instance_id})
        -[:HAS_ATTEMPT|ACTIVE_ATTEMPT]->(attempt)
      WHERE logical.source_key = 'bitrix_chat'
        AND coalesce(logical.bitrix_stream_key, logical.entity_key) IN
          ['crm_deals', 'crm_activities', 'openlines_conversations']
    }
  RETURN count(DISTINCT attempt) AS ingest_count
}
CALL {
  WITH run
  UNWIND CASE WHEN size($checkpoint_ids) = 0 THEN [NULL] ELSE $checkpoint_ids END AS captured
  OPTIONAL MATCH (checkpoint:IngestionCheckpoint {
    control_instance_id: run.control_instance_id, logical_run_id: captured.logical_run_id,
    phase: captured.phase, generation: captured.generation
  })
  WHERE captured IS NOT NULL
    AND checkpoint.status = captured.status
    AND checkpoint.status IN ['active', 'running', 'started', 'queued', 'pending']
    AND EXISTS {
      MATCH (logical:IngestionLogicalRun {
        control_instance_id: run.control_instance_id, logical_run_id: checkpoint.logical_run_id
      })
      WHERE logical.source_key = 'bitrix_chat'
        AND coalesce(logical.bitrix_stream_key, logical.entity_key) IN
          ['crm_deals', 'crm_activities', 'openlines_conversations']
    }
  RETURN count(DISTINCT checkpoint) AS checkpoint_count
}
CALL {
  WITH run
  UNWIND CASE WHEN size($stream_ids) = 0 THEN [NULL] ELSE $stream_ids END AS captured
  OPTIONAL MATCH (stream:BitrixIngestionStream {
    source_key: 'bitrix_chat', control_instance_id: run.control_instance_id,
    stream_key: captured.stream_key
  })
  WHERE captured IS NOT NULL
    AND stream.stream_key IN ['crm_deals', 'crm_activities', 'openlines_conversations']
    AND stream.logical_run_id = captured.logical_run_id
    AND stream.ingest_run_id = captured.ingest_run_id
    AND stream.attempt_generation = captured.attempt_generation
    AND stream.stream_generation = captured.stream_generation
    AND stream.fencing_token = captured.fencing_token
    AND stream.status = captured.status
    AND stream.status IN ['active', 'running', 'started', 'queued', 'pending', 'backfilling']
  RETURN count(DISTINCT stream) AS stream_count
}
CALL {
  WITH run
  UNWIND CASE WHEN size($generation_ids) = 0 THEN [NULL] ELSE $generation_ids END AS captured
  OPTIONAL MATCH (generation:BitrixBackfillGeneration {
    control_instance_id: run.control_instance_id, generation_id: captured.generation_id
  })
  WHERE captured IS NOT NULL
    AND generation.status = captured.status
    AND generation.status IN ['active', 'running', 'started', 'queued', 'pending', 'backfilling']
    AND (
      EXISTS {
        MATCH (generation)-[:HAS_LOGICAL_RUN]->(logical:IngestionLogicalRun {
          control_instance_id: run.control_instance_id
        })
        WHERE logical.source_key = 'bitrix_chat'
          AND coalesce(logical.bitrix_stream_key, logical.entity_key) IN
            ['crm_deals', 'crm_activities', 'openlines_conversations']
      }
      OR EXISTS {
        MATCH (generation)-[:HAS_STREAM]->(stream:BitrixIngestionStream {
          source_key: 'bitrix_chat', control_instance_id: run.control_instance_id
        })
        WHERE stream.stream_key IN ['crm_deals', 'crm_activities', 'openlines_conversations']
      }
    )
  RETURN count(DISTINCT generation) AS generation_count
}
CALL {
  WITH run
  UNWIND CASE WHEN size($publication_ids) = 0 THEN [NULL] ELSE $publication_ids END AS captured
  OPTIONAL MATCH (publication:BitrixBackfillDispatchOutbox {
    control_instance_id: run.control_instance_id,
    successor_generation_id: captured.successor_generation_id
  })
  WHERE captured IS NOT NULL
    AND publication.evidence_digest = captured.evidence_digest
    AND publication.occurrence = captured.occurrence
    AND publication.status = captured.status
    AND publication.status IN ['active', 'running', 'started', 'queued', 'pending']
    AND EXISTS {
      MATCH (generation:BitrixBackfillGeneration {
        control_instance_id: run.control_instance_id,
        generation_id: publication.successor_generation_id
      })-[:HAS_STREAM]->(stream:BitrixIngestionStream {
        source_key: 'bitrix_chat', control_instance_id: run.control_instance_id
      })
      WHERE stream.stream_key IN ['crm_deals', 'crm_activities', 'openlines_conversations']
    }
  RETURN count(DISTINCT publication) AS publication_count
}
WITH run, control, dispatch, logical_count, ingest_count, checkpoint_count, stream_count,
  generation_count, publication_count
WHERE logical_count = size($logical_run_ids)
  AND ingest_count = size($ingest_run_ids)
  AND checkpoint_count = size($checkpoint_ids)
  AND stream_count = size($stream_ids)
  AND generation_count = size($generation_ids)
  AND publication_count = size($publication_ids)
CALL {
  WITH run
  UNWIND CASE WHEN size($logical_run_ids) = 0 THEN [NULL] ELSE $logical_run_ids END AS captured
  MATCH (logical:IngestionLogicalRun {
    control_instance_id: run.control_instance_id, logical_run_id: captured.logical_run_id
  })
  WHERE captured IS NOT NULL
    AND logical.source_key = 'bitrix_chat'
    AND coalesce(logical.bitrix_stream_key, logical.entity_key) IN
      ['crm_deals', 'crm_activities', 'openlines_conversations']
    AND logical.status = captured.status
  SET logical.repair_prior_status = logical.status,
      logical.stop_requested = true,
      logical.stop_reason = 'crm_deal_repair_quiescence', logical.updated_at = datetime()
  RETURN count(logical) AS stopped_logical_count
}
CALL {
  WITH run
  UNWIND CASE WHEN size($ingest_run_ids) = 0 THEN [NULL] ELSE $ingest_run_ids END AS captured
  MATCH (attempt:IngestRun {
    control_instance_id: run.control_instance_id, ingest_run_id: captured.ingest_run_id
  })
  WHERE captured IS NOT NULL
    AND attempt.status = captured.status
    AND attempt.generation = captured.generation
    AND attempt.status IN ['active', 'running', 'started', 'queued', 'pending', 'paused']
    AND EXISTS {
      MATCH (logical:IngestionLogicalRun {control_instance_id: run.control_instance_id})
        -[:HAS_ATTEMPT|ACTIVE_ATTEMPT]->(attempt)
      WHERE logical.source_key = 'bitrix_chat'
        AND coalesce(logical.bitrix_stream_key, logical.entity_key) IN
          ['crm_deals', 'crm_activities', 'openlines_conversations']
    }
  SET attempt.repair_prior_status = attempt.status,
      attempt.status = CASE
        WHEN attempt.status IN ['queued', 'started', 'running', 'paused'] THEN 'failed'
        ELSE attempt.status
      END,
      attempt.failure_category = 'crm_deal_repair_quiescence', attempt.updated_at = datetime()
  RETURN count(attempt) AS terminalized_ingest_count
}
CALL {
  WITH run
  UNWIND CASE WHEN size($checkpoint_ids) = 0 THEN [NULL] ELSE $checkpoint_ids END AS captured
  MATCH (checkpoint:IngestionCheckpoint {
    control_instance_id: run.control_instance_id, logical_run_id: captured.logical_run_id,
    phase: captured.phase, generation: captured.generation
  })
  WHERE captured IS NOT NULL
    AND checkpoint.status = captured.status
    AND checkpoint.status IN ['active', 'running', 'started', 'queued', 'pending']
    AND EXISTS {
      MATCH (logical:IngestionLogicalRun {
        control_instance_id: run.control_instance_id, logical_run_id: checkpoint.logical_run_id
      })
      WHERE logical.source_key = 'bitrix_chat'
        AND coalesce(logical.bitrix_stream_key, logical.entity_key) IN
          ['crm_deals', 'crm_activities', 'openlines_conversations']
    }
  SET checkpoint.repair_prior_status = checkpoint.status,
      checkpoint.status = 'superseded', checkpoint.superseded_by = $run_id,
      checkpoint.updated_at = datetime()
  RETURN count(checkpoint) AS superseded_checkpoint_count
}
CALL {
  WITH run
  UNWIND CASE WHEN size($stream_ids) = 0 THEN [NULL] ELSE $stream_ids END AS captured
  MATCH (stream:BitrixIngestionStream {
    source_key: 'bitrix_chat', control_instance_id: run.control_instance_id,
    stream_key: captured.stream_key, logical_run_id: captured.logical_run_id,
    ingest_run_id: captured.ingest_run_id, attempt_generation: captured.attempt_generation,
    stream_generation: captured.stream_generation, fencing_token: captured.fencing_token,
    status: captured.status
  })
  WHERE captured IS NOT NULL
  SET stream.repair_prior_status = stream.status,
      stream.repair_prior_stream_generation = stream.stream_generation,
      stream.repair_prior_fencing_token = stream.fencing_token,
      stream.status = 'superseded', stream.superseded_by = $run_id,
      stream.superseded_fencing_token = captured.fencing_token,
      stream.stream_generation = captured.stream_generation + 1,
      stream.fencing_token = captured.fencing_token + 1,
      stream.fence_lock_version = coalesce(stream.fence_lock_version, 0) + 1,
      stream.updated_at = datetime()
  RETURN count(stream) AS superseded_stream_count
}
CALL {
  WITH run
  UNWIND CASE WHEN size($generation_ids) = 0 THEN [NULL] ELSE $generation_ids END AS captured
  MATCH (generation:BitrixBackfillGeneration {
    control_instance_id: run.control_instance_id, generation_id: captured.generation_id,
    status: captured.status
  })
  WHERE captured IS NOT NULL
    AND (
      EXISTS {
        MATCH (generation)-[:HAS_LOGICAL_RUN]->(logical:IngestionLogicalRun {
          control_instance_id: run.control_instance_id
        })
        WHERE logical.source_key = 'bitrix_chat'
          AND coalesce(logical.bitrix_stream_key, logical.entity_key) IN
            ['crm_deals', 'crm_activities', 'openlines_conversations']
      }
      OR EXISTS {
        MATCH (generation)-[:HAS_STREAM]->(stream:BitrixIngestionStream {
          source_key: 'bitrix_chat', control_instance_id: run.control_instance_id
        })
        WHERE stream.stream_key IN ['crm_deals', 'crm_activities', 'openlines_conversations']
      }
    )
  SET generation.repair_prior_status = generation.status,
      generation.status = 'superseded', generation.superseded_by = $run_id,
      generation.updated_at = datetime()
  RETURN count(generation) AS superseded_generation_count
}
CALL {
  WITH run
  UNWIND CASE WHEN size($publication_ids) = 0 THEN [NULL] ELSE $publication_ids END AS captured
  MATCH (publication:BitrixBackfillDispatchOutbox {
    control_instance_id: run.control_instance_id,
    successor_generation_id: captured.successor_generation_id,
    evidence_digest: captured.evidence_digest, occurrence: captured.occurrence,
    status: captured.status
  })
  WHERE captured IS NOT NULL
    AND EXISTS {
      MATCH (generation:BitrixBackfillGeneration {
        control_instance_id: run.control_instance_id,
        generation_id: publication.successor_generation_id
      })-[:HAS_STREAM]->(stream:BitrixIngestionStream {
        source_key: 'bitrix_chat', control_instance_id: run.control_instance_id
      })
      WHERE stream.stream_key IN ['crm_deals', 'crm_activities', 'openlines_conversations']
    }
  SET publication.repair_prior_status = publication.status,
      publication.status = 'superseded', publication.superseded_by = $run_id,
      publication.updated_at = datetime()
  RETURN count(publication) AS superseded_publication_count
}
WITH run, control, dispatch
WHERE control.run_id = $run_id
  AND control.owner_id = $owner_id
  AND control.token = $token
  AND control.revision = $expected_revision
  AND control.state = 'quiescing'
  AND control.boundary_digest = $boundary_digest
  AND dispatch.control_instance_id = run.control_instance_id
  AND dispatch.repair_control_run_id = $run_id
  AND dispatch.repair_control_owner_id = $owner_id
  AND dispatch.repair_control_token = $token
  AND dispatch.repair_control_revision = $expected_revision
  AND dispatch.repair_control_state = 'quiescing'
  AND dispatch.blocked = true
SET dispatch.repair_control_state = 'quiesced', dispatch.repair_control_revision = $next_revision,
    dispatch.updated_at = datetime(), control.state = 'quiesced', control.revision = $next_revision,
    control.updated_at = datetime()
RETURN control.revision AS revision
"""

VERIFY_QUIESCED_REPAIR_TOPOLOGY = """
MATCH (run:CrmDealRepairRun {run_id: $run_id, status: 'qualified', boundary_digest: $boundary_digest,
  execution_allowed: false})
MATCH (control:CrmDealRepairControl {run_id: $run_id, owner_id: $owner_id, token: $token,
  revision: $expected_revision, state: 'quiesced', boundary_digest: $boundary_digest})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: run.control_instance_id, blocked: true, repair_control_run_id: $run_id,
  repair_control_owner_id: $owner_id, repair_control_token: $token,
  repair_control_revision: $expected_revision, repair_control_state: 'quiesced'})
CALL {
  WITH run
  UNWIND CASE WHEN size($logical_run_ids) = 0 THEN [NULL] ELSE $logical_run_ids END AS captured
  OPTIONAL MATCH (logical:IngestionLogicalRun {control_instance_id: run.control_instance_id,
    logical_run_id: captured.logical_run_id, status: captured.status, repair_prior_status: captured.status, stop_requested: true})
  WHERE captured IS NOT NULL
  RETURN count(DISTINCT logical) AS logical_count
}
CALL {
  WITH run
  UNWIND CASE WHEN size($ingest_run_ids) = 0 THEN [NULL] ELSE $ingest_run_ids END AS captured
  OPTIONAL MATCH (attempt:IngestRun {control_instance_id: run.control_instance_id,
    ingest_run_id: captured.ingest_run_id, generation: captured.generation})
  WHERE captured IS NOT NULL AND attempt.repair_prior_status = captured.status AND attempt.status = CASE
    WHEN captured.status IN ['queued', 'started', 'running', 'paused'] THEN 'failed' ELSE captured.status END
  RETURN count(DISTINCT attempt) AS ingest_count
}
CALL {
  WITH run
  UNWIND CASE WHEN size($checkpoint_ids) = 0 THEN [NULL] ELSE $checkpoint_ids END AS captured
  OPTIONAL MATCH (checkpoint:IngestionCheckpoint {control_instance_id: run.control_instance_id,
    logical_run_id: captured.logical_run_id, phase: captured.phase, generation: captured.generation,
    status: 'superseded', superseded_by: $run_id, repair_prior_status: captured.status})
  WHERE captured IS NOT NULL
  RETURN count(DISTINCT checkpoint) AS checkpoint_count
}
CALL {
  WITH run
  UNWIND CASE WHEN size($stream_ids) = 0 THEN [NULL] ELSE $stream_ids END AS captured
  OPTIONAL MATCH (stream:BitrixIngestionStream {source_key: 'bitrix_chat',
    control_instance_id: run.control_instance_id, stream_key: captured.stream_key,
    logical_run_id: captured.logical_run_id, ingest_run_id: captured.ingest_run_id,
    attempt_generation: captured.attempt_generation, stream_generation: captured.stream_generation + 1,
    fencing_token: captured.fencing_token + 1, status: 'superseded', superseded_by: $run_id, repair_prior_status: captured.status})
  WHERE captured IS NOT NULL
  RETURN count(DISTINCT stream) AS stream_count
}
CALL {
  WITH run
  UNWIND CASE WHEN size($generation_ids) = 0 THEN [NULL] ELSE $generation_ids END AS captured
  OPTIONAL MATCH (generation:BitrixBackfillGeneration {control_instance_id: run.control_instance_id,
    generation_id: captured.generation_id, status: 'superseded', superseded_by: $run_id, repair_prior_status: captured.status})
  WHERE captured IS NOT NULL
  RETURN count(DISTINCT generation) AS generation_count
}
CALL {
  WITH run
  UNWIND CASE WHEN size($publication_ids) = 0 THEN [NULL] ELSE $publication_ids END AS captured
  OPTIONAL MATCH (publication:BitrixBackfillDispatchOutbox {control_instance_id: run.control_instance_id,
    successor_generation_id: captured.successor_generation_id, evidence_digest: captured.evidence_digest,
    occurrence: captured.occurrence, status: 'superseded', superseded_by: $run_id, repair_prior_status: captured.status})
  WHERE captured IS NOT NULL
  RETURN count(DISTINCT publication) AS publication_count
}
WITH logical_count, ingest_count, checkpoint_count, stream_count, generation_count, publication_count
WHERE logical_count = size($logical_run_ids) AND ingest_count = size($ingest_run_ids)
  AND checkpoint_count = size($checkpoint_ids) AND stream_count = size($stream_ids)
  AND generation_count = size($generation_ids) AND publication_count = size($publication_ids)
RETURN true AS verified
"""

TERMINALIZE_STALE_REPAIR_RUN = """
MATCH (run:CrmDealRepairRun {run_id: $run_id, status: 'qualified', boundary_digest: $boundary_digest,
  execution_allowed: false})
MATCH (control:CrmDealRepairControl {run_id: $run_id, owner_id: $owner_id, token: $token,
  revision: $expected_revision, boundary_digest: $boundary_digest})
WHERE control.state IN ['quiesced', 'paused', 'allocated']
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: run.control_instance_id, blocked: true, repair_control_run_id: $run_id,
  repair_control_owner_id: $owner_id, repair_control_token: $token,
  repair_control_revision: $expected_revision})
MATCH (stale:IngestRun {ingest_run_id: $stale_run_id,
  control_instance_id: $stale_control_instance_id, source_key: $stale_source_key, status: $stale_status})
  -[:FROM_SOURCE]->(:SourceSystem {source_key: $stale_source_key})
WHERE stale.control_instance_id = run.control_instance_id
CALL {
  WITH stale
  OPTIONAL MATCH (logical:IngestionLogicalRun)-[:HAS_ATTEMPT|ACTIVE_ATTEMPT]->(stale)
  RETURN [item IN collect(DISTINCT logical.logical_run_id) WHERE item IS NOT NULL | item]
    AS actual_logical_run_ids
}
CALL {
  WITH stale
  OPTIONAL MATCH (checkpoint:IngestionCheckpoint)-[:PRODUCED_BY]->(stale)
  RETURN [item IN collect(DISTINCT checkpoint.logical_run_id + '|' + checkpoint.phase + '|'
    + toString(checkpoint.generation)) WHERE item IS NOT NULL | item] AS produced_checkpoint_ids
}
CALL {
  WITH stale
  OPTIONAL MATCH (logical:IngestionLogicalRun)-[:HAS_ATTEMPT|ACTIVE_ATTEMPT]->(stale)
  OPTIONAL MATCH (checkpoint:IngestionCheckpoint)-[:CHECKPOINT_FOR]->(logical)
  RETURN [item IN collect(DISTINCT checkpoint.logical_run_id + '|' + checkpoint.phase + '|'
    + toString(checkpoint.generation)) WHERE item IS NOT NULL | item] AS logical_checkpoint_ids
}
CALL {
  WITH stale
  OPTIONAL MATCH (logical:IngestionLogicalRun)-[:HAS_ATTEMPT|ACTIVE_ATTEMPT]->(stale)
  OPTIONAL MATCH (stream:BitrixIngestionStream {control_instance_id: logical.control_instance_id,
    logical_run_id: logical.logical_run_id, ingest_run_id: stale.ingest_run_id})
  RETURN [item IN collect(DISTINCT stream.stream_key) WHERE item IS NOT NULL | item] AS actual_stream_keys
}
WITH run, control, dispatch, stale, actual_logical_run_ids, produced_checkpoint_ids,
  logical_checkpoint_ids, actual_stream_keys
WHERE size(actual_logical_run_ids) = size($logical_run_ids)
  AND all(item IN actual_logical_run_ids WHERE item IN $logical_run_ids)
  AND size(actual_logical_run_ids) <= 1
  AND size(produced_checkpoint_ids) = 0
  AND size(logical_checkpoint_ids) = size($checkpoint_ids)
  AND all(item IN logical_checkpoint_ids WHERE item IN $checkpoint_ids)
  AND size(actual_stream_keys) = size($stream_keys)
  AND all(item IN actual_stream_keys WHERE item IN $stream_keys)
  AND ((size($logical_run_ids) = 0 AND size($checkpoint_ids) = 0 AND size($stream_keys) = 0)
    OR size($logical_run_ids) = 1)
SET stale.status = 'failed', stale.failure_category = 'crm_deal_repair_stale_run',
  stale.failure_message = 'terminalized by exact repair control proof',
  stale.repair_control_run_id = $run_id, stale.repair_control_revision = $expected_revision,
  stale.repair_control_evidence = 'exact_owner_or_orphan', stale.updated_at = datetime()
RETURN stale.ingest_run_id AS ingest_run_id
"""

ALLOCATE_REPAIR_UNITS = """
MATCH (run:CrmDealRepairRun {run_id: $run_id, status: 'qualified',
  manifest_digest: $manifest_digest, artifact_id: $artifact_id,
  artifact_manifest_hmac: $artifact_manifest_hmac, inventory_digest: $inventory_digest,
  boundary_digest: $boundary_digest, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, manifest_json: $manifest_json,
  inventory_row_count: $inventory_row_count, eligible_unit_count: $eligible_unit_count,
  negative_control_count: $negative_control_count, execution_allowed: false})
MATCH (run)-[:QUALIFIED_WITH]->(boundary:RepairExecutionBoundary {
  manifest_digest: $manifest_digest, artifact_id: $artifact_id,
  artifact_manifest_hmac: $artifact_manifest_hmac, inventory_digest: $inventory_digest,
  boundary_digest: $boundary_digest, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, manifest_json: $manifest_json,
  inventory_row_count: $inventory_row_count, eligible_unit_count: $eligible_unit_count,
  negative_control_count: $negative_control_count, execution_allowed: false})
WITH run, boundary
WHERE run.source_record_pks_json = boundary.source_record_pks_json
MATCH (control:CrmDealRepairControl {run_id: $run_id, owner_id: $owner_id, token: $token,
  revision: $expected_revision, boundary_digest: $boundary_digest})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: $control_instance_id, blocked: true,
  repair_control_run_id: $run_id, repair_control_owner_id: $owner_id,
  repair_control_token: $token, repair_control_revision: $expected_revision})
OPTIONAL MATCH (existing:CrmDealRepairAllocationCompletion {run_id: $run_id})
CALL {
  WITH run
  UNWIND $approved_rows AS row
  MATCH (qualified:CrmDealRepairQualifiedInventoryRow {
    run_id: run.run_id, inventory_key: row.inventory_key,
    source_record_pk: row.source_record_pk, inventory_fingerprint: row.inventory_fingerprint,
    execution_allowed: false
  })
  RETURN count(qualified) AS qualified_row_match_count
}
CALL {
  WITH $units AS units
  UNWIND CASE WHEN size(units) = 0 THEN [NULL] ELSE units END AS unit
  RETURN count(DISTINCT unit.unit_id) AS distinct_unit_count
}
CALL {
  WITH $approved_rows AS rows
  UNWIND CASE WHEN size(rows) = 0 THEN [NULL] ELSE rows END AS row
  RETURN count(DISTINCT row.inventory_key) AS distinct_inventory_key_count,
    count(DISTINCT row.inventory_fingerprint) AS distinct_fingerprint_count
}
CALL {
  WITH run
  OPTIONAL MATCH (stored:CrmDealRepairUnit {run_id: run.run_id})
  WITH stored ORDER BY stored.sequence
  RETURN collect(CASE WHEN stored IS NULL THEN NULL ELSE {
    run_id: stored.run_id, unit_id: stored.unit_id, generation: stored.generation, sequence: stored.sequence,
    attempt: stored.attempt, boundary_digest: stored.boundary_digest,
    inventory_fingerprint: stored.inventory_fingerprint, state: stored.state
  } END) AS persisted_units
}
WITH run, boundary, control, dispatch, existing, qualified_row_match_count, distinct_unit_count,
  distinct_inventory_key_count, distinct_fingerprint_count, persisted_units
WHERE qualified_row_match_count = size($approved_rows)
  AND size($units) <= size($approved_rows)
  AND size($units) <= $unit_ceiling
  AND size($units) <= run.eligible_unit_count
  AND $unit_ceiling = $manifest_unit_ceiling
  AND size($approved_rows) = run.inventory_row_count
  AND size($approved_rows) = size($qualified_source_record_pks)
  AND size($approved_rows) = distinct_inventory_key_count
  AND size($approved_rows) = distinct_fingerprint_count
  AND all(row IN $approved_rows WHERE row.disposition IN ['executable', 'blocked', 'investigate']
    AND row.source_record_pk IN $qualified_source_record_pks)
  AND all(source_record_pk IN $qualified_source_record_pks
    WHERE single(row IN $approved_rows WHERE row.source_record_pk = source_record_pk))
  AND size($units) = size([row IN $approved_rows WHERE row.disposition = 'executable'])
  AND size($units) = distinct_unit_count
  AND (size($units) = 0 OR all(index IN range(0, size($units) - 1)
    WHERE $units[index].sequence = index))
  AND all(unit IN $units WHERE unit.run_id = $run_id
    AND unit.generation = $generation AND unit.attempt = 1
    AND unit.boundary_digest = $boundary_digest AND unit.state = 'allocated'
    AND unit.inventory_fingerprint IN [row IN $approved_rows
      WHERE row.disposition = 'executable' | row.inventory_fingerprint])
  AND all(row IN $approved_rows WHERE row.disposition <> 'executable'
    OR single(unit IN $units WHERE unit.inventory_fingerprint = row.inventory_fingerprint))
  AND dispatch.repair_control_state IN ['quiesced', 'allocated']
  AND (
    (existing IS NULL AND control.state = 'quiesced' AND dispatch.repair_control_state = 'quiesced'
      AND size(persisted_units) = 0)
    OR
    (existing IS NOT NULL AND control.state = 'allocated'
      AND dispatch.repair_control_state = 'allocated'
      AND existing.allocation_digest = $allocation_digest
      AND existing.overlay_digest = $overlay_digest
      AND existing.approval_reference = $approval_reference
      AND existing.manifest_digest = $manifest_digest
      AND existing.artifact_id = $artifact_id
      AND existing.unit_count = size($units)
      AND existing.executable_count = size([row IN $approved_rows
        WHERE row.disposition = 'executable'])
      AND existing.execution_allowed = false
      AND persisted_units = $units)
  )
WITH run, control, dispatch, existing,
  existing IS NOT NULL AS replay
FOREACH (_ IN CASE WHEN replay THEN [] ELSE [1] END |
  FOREACH (unit IN $units |
    CREATE (:CrmDealRepairUnit {
      run_id: $run_id, unit_id: unit.unit_id, generation: unit.generation,
      sequence: unit.sequence, attempt: unit.attempt, boundary_digest: unit.boundary_digest,
      inventory_fingerprint: unit.inventory_fingerprint, state: 'allocated',
      execution_allowed: false, created_at: datetime()
    })
  )
)
MERGE (completion:CrmDealRepairAllocationCompletion {run_id: $run_id})
ON CREATE SET completion.allocation_digest = $allocation_digest,
  completion.overlay_digest = $overlay_digest, completion.approval_reference = $approval_reference,
  completion.manifest_digest = $manifest_digest, completion.artifact_id = $artifact_id,
  completion.executable_count = size([row IN $approved_rows WHERE row.disposition = 'executable']),
  completion.unit_count = size($units),
  completion.execution_allowed = false, completion.created_at = datetime()
WITH control, dispatch, completion, replay
WHERE completion.allocation_digest = $allocation_digest
  AND completion.overlay_digest = $overlay_digest
  AND completion.approval_reference = $approval_reference
  AND completion.manifest_digest = $manifest_digest
  AND completion.artifact_id = $artifact_id
  AND completion.executable_count = size([row IN $approved_rows
    WHERE row.disposition = 'executable'])
  AND completion.unit_count = size($units)
  AND completion.execution_allowed = false
FOREACH (_ IN CASE WHEN replay THEN [] ELSE [1] END |
  SET control.state = 'allocated', control.revision = $next_revision, control.updated_at = datetime(),
      dispatch.repair_control_state = 'allocated', dispatch.repair_control_revision = $next_revision,
      dispatch.updated_at = datetime()
)
RETURN completion.allocation_digest AS allocation_digest,
  completion.executable_count AS executable_count, completion.unit_count AS unit_count,
  CASE WHEN replay THEN control.revision ELSE $next_revision END AS revision,
  replay AS replayed
"""
