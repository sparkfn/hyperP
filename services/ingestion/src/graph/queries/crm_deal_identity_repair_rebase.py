# #424: one staging-only allocation-boundary rebase.  These statements neither
# create units nor touch CRM graph facts.  The write lock order is dispatch,
# control, then completion; every authority condition is reread after the lock.
READ_REBASE_REPLAY = """
MATCH (run:CrmDealRepairRun {repair_id: $repair_id, run_id: $run_id,
  status: 'qualified', execution_allowed: false})
MATCH (control:CrmDealRepairControl {run_id: $run_id, repair_id: $repair_id,
  owner_id: $owner_id, token_digest: $token_digest, state: 'allocated'})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: control.control_instance_id, blocked: true,
  block_reason: 'crm_deal_identity_repair_quiesce', repair_run_id: $run_id,
  repair_owner_id: $owner_id, repair_token_digest: $token_digest,
  repair_revision: control.revision})
MATCH (completion:CrmDealRepairAllocationCompletion {run_id: $run_id,
  rebase_request_digest: $rebase_request_digest,
  rebase_fresh_artifact_id: $fresh_artifact_id,
  rebase_expected_observed_boundary_digest: $expected_observed_boundary_digest})
WHERE control.control_instance_id = run.control_instance_id
  AND control.revision = completion.rebase_revision
  AND control.sealed_revision = control.revision
  AND control.sealed_boundary_digest = completion.rebase_replacement_boundary_digest
  AND completion.allocation_revision = control.revision
  AND completion.allocation_state = 'allocated'
  AND completion.allocation_sealed_boundary_digest = control.sealed_boundary_digest
  AND completion.receipt_revision = control.revision
  AND completion.receipt_state = 'allocated'
  AND completion.receipt_sealed_boundary_digest = control.sealed_boundary_digest
RETURN properties(control) AS control, properties(completion) AS completion
"""

LOCK_REBASE_AUTHORITY = """
MATCH (run:CrmDealRepairRun {repair_id: $repair_id, run_id: $run_id,
  status: 'qualified', boundary_digest: $boundary_digest, execution_allowed: false})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat'})
WHERE dispatch.control_instance_id = run.control_instance_id
SET dispatch.repair_run_id = dispatch.repair_run_id
WITH run, dispatch
MATCH (control:CrmDealRepairControl {run_id: $run_id, repair_id: $repair_id})
SET control.integration_rebase_lock = $rebase_request_digest
WITH run, dispatch, control
MATCH (completion:CrmDealRepairAllocationCompletion {run_id: $run_id})
SET completion.rebase_lock = $rebase_request_digest
RETURN properties(control) AS control, properties(dispatch) AS dispatch,
       properties(completion) AS completion
"""

READ_REBASE_GUARDS = """
MATCH (run:CrmDealRepairRun {repair_id: $repair_id, run_id: $run_id,
  status: 'qualified', boundary_digest: $boundary_digest, execution_allowed: false})
MATCH (control:CrmDealRepairControl {run_id: $run_id, repair_id: $repair_id,
  owner_id: $owner_id, token_digest: $token_digest, revision: $expected_revision,
  state: 'allocated', boundary_digest: $boundary_digest,
  sealed_revision: $expected_revision})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  blocked: true,
  block_reason: 'crm_deal_identity_repair_quiesce', repair_run_id: $run_id,
  repair_owner_id: $owner_id, repair_token_digest: $token_digest,
  repair_revision: $expected_revision})
MATCH (completion:CrmDealRepairAllocationCompletion {run_id: $run_id,
  completion_id: $completion_id, boundary_digest: $boundary_digest,
  overlay_digest: $overlay_digest, allocation_digest: $allocation_digest,
  unit_count: $unit_count, unit_ids: $unit_ids, unit_set_digest: $unit_set_digest,
  allocation_revision: $expected_revision, allocation_state: 'allocated',
  receipt_run_id: $run_id,
  receipt_owner_id: $owner_id, receipt_token_digest: $token_digest,
  receipt_revision: $expected_revision, receipt_state: 'allocated'})
WHERE control.control_instance_id = run.control_instance_id
  AND dispatch.control_instance_id = run.control_instance_id
  AND completion.allocation_control_instance_id = run.control_instance_id
  AND completion.receipt_control_instance_id = run.control_instance_id
  AND completion.rebase_request_digest IS NULL
  AND completion.allocation_sealed_boundary_digest = control.sealed_boundary_digest
  AND completion.receipt_sealed_boundary_digest = control.sealed_boundary_digest
  AND completion.receipt_boundary_digest = $boundary_digest
  AND completion.receipt_digest IS NOT NULL
  AND completion.allocation_origin_key_id = $approval_key_id
  AND completion.allocation_origin_hmac IS NOT NULL
  AND NOT EXISTS { MATCH (:CrmDealRepairAcceptance {run_id: $run_id}) }
  AND NOT EXISTS { MATCH (:CrmDealRepairDispatchRelease {run_id: $run_id}) }
  AND NOT EXISTS { MATCH (reservation:CrmDealRepairPublicationReservation)
    WHERE reservation.control_instance_id = run.control_instance_id
      AND reservation.state IN ['preparing', 'publishing'] }
  AND NOT EXISTS { MATCH (:CrmDealRepairMutationResult {run_id: $run_id}) }
  AND NOT EXISTS { MATCH (:CrmDealRepairVerification {run_id: $run_id}) }
  AND NOT EXISTS { MATCH (:CrmDealRepairSecondaryDisposition {run_id: $run_id}) }
  AND NOT EXISTS { MATCH (:CrmDealRepairFence {run_id: $run_id, state: 'claimed'}) }
  AND size($units) = $unit_count
  AND size($unit_ids) = $unit_count
CALL {
  WITH completion
  MATCH (candidate:CrmDealRepairAllocationCompletion {run_id: completion.run_id})
  RETURN count(candidate) AS completion_count
}
CALL {
  WITH completion
  OPTIONAL MATCH (unit:CrmDealRepairUnit {run_id: completion.run_id})
  RETURN count(unit) AS stored_unit_count
}
WITH run, control, dispatch, completion, completion_count, stored_unit_count
WHERE completion_count = 1 AND stored_unit_count = completion.unit_count
  AND all(unit_id IN completion.unit_ids WHERE EXISTS {
    MATCH (:CrmDealRepairUnit {run_id: $run_id, unit_id: unit_id})
  })
  AND all(expected IN $units WHERE EXISTS {
    MATCH (stored:CrmDealRepairUnit {run_id: $run_id, unit_id: expected.unit_id})
    WHERE stored.generation = expected.generation AND stored.sequence = expected.sequence
      AND stored.attempt = expected.attempt AND stored.boundary_digest = expected.boundary_digest
      AND stored.inventory_fingerprint = expected.inventory_fingerprint
      AND stored.state = expected.state AND stored.inventory_key = expected.inventory_key
      AND stored.source_record_pk = expected.source_record_pk
      AND stored.inventory_graph_fingerprint = expected.inventory_graph_fingerprint
      AND stored.inventory_stored_payload_fingerprint = expected.inventory_stored_payload_fingerprint
      AND stored.inventory_binding_digest = expected.inventory_binding_digest
  })
RETURN properties(control) AS control, properties(dispatch) AS dispatch,
       properties(completion) AS completion
"""

ADVANCE_REBASE_CONTROL = """
MATCH (run:CrmDealRepairRun {repair_id: $repair_id, run_id: $run_id,
  status: 'qualified', execution_allowed: false})
MATCH (control:CrmDealRepairControl {run_id: $run_id, repair_id: $repair_id,
  owner_id: $owner_id, token_digest: $token_digest, revision: $expected_revision,
  state: 'allocated', sealed_revision: $expected_revision})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: control.control_instance_id, blocked: true,
  block_reason: 'crm_deal_identity_repair_quiesce', repair_run_id: $run_id,
  repair_owner_id: $owner_id, repair_token_digest: $token_digest,
  repair_revision: $expected_revision})
WHERE control.control_instance_id = run.control_instance_id
MATCH (completion:CrmDealRepairAllocationCompletion {run_id: $run_id,
  completion_id: $completion_id})
WHERE control.control_instance_id = run.control_instance_id
  AND dispatch.control_instance_id = run.control_instance_id
  AND completion.allocation_control_instance_id = run.control_instance_id
  AND completion.receipt_control_instance_id = run.control_instance_id
  AND completion.rebase_request_digest IS NULL
SET control.revision = control.revision + 1, control.updated_at = datetime(),
    control.last_transition = 'rebase-boundary',
    control.last_transition_expected_revision = $expected_revision,
    dispatch.repair_revision = control.revision, dispatch.updated_at = datetime()
RETURN control.revision AS revision
"""

COMMIT_REBASE_BOUNDARY = """
MATCH (run:CrmDealRepairRun {repair_id: $repair_id, run_id: $run_id,
  status: 'qualified', execution_allowed: false})
MATCH (control:CrmDealRepairControl {run_id: $run_id, repair_id: $repair_id,
  owner_id: $owner_id, token_digest: $token_digest, revision: $revision,
  state: 'allocated'})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: control.control_instance_id, blocked: true,
  block_reason: 'crm_deal_identity_repair_quiesce', repair_run_id: $run_id,
  repair_owner_id: $owner_id, repair_token_digest: $token_digest,
  repair_revision: $revision})
WHERE control.control_instance_id = run.control_instance_id
MATCH (completion:CrmDealRepairAllocationCompletion {run_id: $run_id,
  completion_id: $completion_id, allocation_state: 'allocated'})
WHERE control.control_instance_id = run.control_instance_id
  AND dispatch.control_instance_id = run.control_instance_id
  AND completion.allocation_control_instance_id = run.control_instance_id
  AND completion.receipt_control_instance_id = run.control_instance_id
  AND completion.rebase_request_digest IS NULL
SET control.sealed_revision = $revision,
    control.sealed_boundary_digest = $replacement_boundary_digest,
    control.sealed_source_records_digest = $replacement_source_records_digest,
    control.sealed_source_instance_digest = $replacement_source_instance_digest,
    control.sealed_stale_run_evidence_digest = $replacement_stale_run_evidence_digest,
    control.sealed_control_digest = $replacement_control_digest,
    control.sealed_inventory_digest = $replacement_inventory_digest,
    control.sealed_inventory_row_count = $replacement_inventory_row_count,
    control.sealed_eligible_unit_count = $replacement_eligible_unit_count,
    control.sealed_negative_control_count = $replacement_negative_control_count,
    completion.rebase_request_digest = $rebase_request_digest,
    completion.rebase_approval_id = $approval_id,
    completion.rebase_fresh_artifact_id = $fresh_artifact_id,
    completion.rebase_fresh_artifact_manifest_hmac = $fresh_artifact_manifest_hmac,
    completion.rebase_fresh_inventory_digest = $fresh_inventory_digest,
    completion.rebase_fresh_producer_repository_sha = $fresh_producer_repository_sha,
    completion.rebase_fresh_producer_image_digest = $fresh_producer_image_digest,
    completion.rebase_expected_observed_boundary_digest = $expected_observed_boundary_digest,
    completion.rebase_previous_boundary_digest = $previous_boundary_digest,
    completion.rebase_previous_receipt_digest = completion.receipt_digest,
    completion.rebase_previous_origin_hmac = completion.allocation_origin_hmac,
    completion.rebase_previous_revision = completion.allocation_revision,
    completion.rebase_replacement_boundary_digest = $replacement_boundary_digest,
    completion.rebase_revision = $revision,
    completion.rebase_receipt_digest = $rebase_receipt_digest,
    completion.rebase_audit_digest = $rebase_audit_digest,
    completion.rebase_key_id = $approval_key_id,
    completion.rebase_hmac = $rebase_hmac,
    completion.rebase_created_at = datetime(),
    completion.allocation_revision = $revision,
    completion.allocation_sealed_boundary_digest = $replacement_boundary_digest,
    completion.allocation_origin_hmac = $replacement_origin_hmac,
    completion.receipt_revision = $revision,
    completion.receipt_sealed_boundary_digest = $replacement_boundary_digest,
    completion.receipt_digest = $replacement_receipt_digest,
    completion.receipt_created_at = datetime()
RETURN properties(control) AS control, properties(completion) AS completion
"""

READ_EFFECTIVE_REBASE_BOUNDARY = """
MATCH (control:CrmDealRepairControl {run_id: $run_id})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: control.control_instance_id})
WHERE control.state = 'allocated' AND control.sealed_revision = control.revision
  AND dispatch.blocked = true AND dispatch.block_reason = 'crm_deal_identity_repair_quiesce'
  AND dispatch.repair_run_id = control.run_id AND dispatch.repair_owner_id = control.owner_id
  AND dispatch.repair_token_digest = control.token_digest AND dispatch.repair_revision = control.revision
OPTIONAL MATCH (completion:CrmDealRepairAllocationCompletion {run_id: $run_id})
RETURN properties(control) AS control, properties(dispatch) AS dispatch,
       collect(properties(completion)) AS completions
"""
