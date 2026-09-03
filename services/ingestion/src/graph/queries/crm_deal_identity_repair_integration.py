"""Parameterized #313 integration CAS queries; no CRM-domain semantics live here."""

CREATE_CRM_DEAL_REPAIR_INTEGRATION_SCHEMA = (
    "CREATE CONSTRAINT crm_deal_repair_rollback_authorization_slot_unique IF NOT EXISTS FOR (n:CrmDealRepairRollbackAuthorization) REQUIRE (n.run_id, n.unit_id, n.rollback_image_id) IS UNIQUE",
    "CREATE CONSTRAINT crm_deal_repair_rollback_receipt_unique IF NOT EXISTS FOR (n:CrmDealRepairRollbackReceipt) REQUIRE (n.run_id, n.receipt_id) IS UNIQUE",
    "CREATE CONSTRAINT crm_deal_repair_acceptance_unique IF NOT EXISTS FOR (n:CrmDealRepairAcceptance) REQUIRE n.run_id IS UNIQUE",
    "CREATE CONSTRAINT crm_deal_repair_release_unique IF NOT EXISTS FOR (n:CrmDealRepairDispatchRelease) REQUIRE n.run_id IS UNIQUE",
)

# Every command repeats this authority match in its own transaction.  The runtime
# separately validates the HMAC over the completion values returned by READ_AUTHORITY.
_BASE_AUTHORITY = """
MATCH (run:CrmDealRepairRun {repair_id: $repair_id, run_id: $run_id,
  qualification_identity: $qualification_identity, manifest_digest: $manifest_digest,
  artifact_id: $artifact_id, artifact_manifest_hmac: $artifact_manifest_hmac,
  manifest_json: $manifest_json, inventory_digest: $inventory_digest,
  inventory_row_count: $inventory_row_count, eligible_unit_count: $eligible_unit_count,
  negative_control_count: $negative_control_count, boundary_digest: $boundary_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  status: 'qualified', execution_allowed: false})-[:QUALIFIED_WITH]->
  (:RepairExecutionBoundary {manifest_digest: $manifest_digest, artifact_id: $artifact_id,
    artifact_manifest_hmac: $artifact_manifest_hmac, manifest_json: $manifest_json,
    inventory_digest: $inventory_digest, inventory_row_count: $inventory_row_count,
    eligible_unit_count: $eligible_unit_count, negative_control_count: $negative_control_count,
    boundary_digest: $boundary_digest, source_instance_id: $source_instance_id,
    control_instance_id: $control_instance_id, execution_allowed: false})
MATCH (control:CrmDealRepairControl {run_id: $run_id, repair_id: $repair_id,
  owner_id: $owner_id, token_digest: $token_digest, revision: $revision,
  boundary_digest: $boundary_digest, state: 'allocated', sealed_revision: $revision,
  sealed_boundary_digest: $sealed_boundary_digest, sealed_inventory_digest: $inventory_digest})
MATCH (completion:CrmDealRepairAllocationCompletion {run_id: $run_id,
  completion_id: $completion_id, boundary_digest: $boundary_digest,
  overlay_digest: $overlay_digest, allocation_digest: $allocation_digest,
  unit_set_digest: $allocation_unit_set_digest,
  request_digest: $allocation_request_digest,
  allocation_origin_key_id: $allocation_origin_key_id,
  allocation_origin_hmac: $allocation_origin_hmac,
  receipt_digest: $allocation_receipt_digest,
  allocation_control_instance_id: $control_instance_id,
  allocation_revision: $allocation_revision, allocation_state: 'allocated',
  allocation_sealed_boundary_digest: $sealed_boundary_digest,
  receipt_control_instance_id: $control_instance_id, receipt_run_id: $run_id,
  receipt_owner_id: $owner_id, receipt_token_digest: $token_digest,
  receipt_revision: $allocation_revision, receipt_state: 'allocated',
  receipt_boundary_digest: $boundary_digest,
  receipt_sealed_boundary_digest: $sealed_boundary_digest})
"""

_AUTHORITY = (
    _BASE_AUTHORITY
    + """
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: $control_instance_id, blocked: true,
  block_reason: 'crm_deal_identity_repair_quiesce', repair_run_id: $run_id,
  repair_owner_id: $owner_id, repair_token_digest: $token_digest,
  repair_revision: $revision})
"""
)

READ_AUTHORITY = """
MATCH (run:CrmDealRepairRun {repair_id: $repair_id, run_id: $run_id,
  status: 'qualified', execution_allowed: false, boundary_digest: $boundary_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id})
MATCH (control:CrmDealRepairControl {run_id: $run_id, repair_id: $repair_id,
  owner_id: $owner_id, token_digest: $token_digest, revision: $revision,
  state: 'allocated', boundary_digest: $boundary_digest, sealed_revision: $revision})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: $control_instance_id, blocked: true,
  block_reason: 'crm_deal_identity_repair_quiesce', repair_run_id: $run_id,
  repair_owner_id: $owner_id, repair_token_digest: $token_digest,
  repair_revision: $revision})
MATCH (completion:CrmDealRepairAllocationCompletion {run_id: $run_id,
  boundary_digest: $boundary_digest, allocation_control_instance_id: $control_instance_id,
  allocation_revision: $allocation_revision, allocation_state: 'allocated',
  receipt_control_instance_id: $control_instance_id, receipt_run_id: $run_id,
  receipt_owner_id: $owner_id, receipt_token_digest: $token_digest,
  receipt_revision: $allocation_revision, receipt_state: 'allocated'})
RETURN properties(completion) AS completion,
  control.sealed_boundary_digest AS sealed_boundary_digest,
  completion.allocation_revision AS allocation_revision
"""


READ_RELEASE_AUTHORITY = """
MATCH (run:CrmDealRepairRun {repair_id: $repair_id, run_id: $run_id,
  qualification_identity: $qualification_identity, manifest_digest: $manifest_digest,
  artifact_id: $artifact_id, artifact_manifest_hmac: $artifact_manifest_hmac,
  manifest_json: $manifest_json, inventory_digest: $inventory_digest,
  inventory_row_count: $inventory_row_count, eligible_unit_count: $eligible_unit_count,
  negative_control_count: $negative_control_count, status: 'qualified', execution_allowed: false,
  boundary_digest: $boundary_digest, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id})
MATCH (control:CrmDealRepairControl {run_id: $run_id, repair_id: $repair_id,
  owner_id: $owner_id, token_digest: $token_digest, revision: $revision,
  state: 'allocated', boundary_digest: $boundary_digest, sealed_revision: $revision})
MATCH (completion:CrmDealRepairAllocationCompletion {run_id: $run_id,
  boundary_digest: $boundary_digest, allocation_control_instance_id: $control_instance_id,
  allocation_state: 'allocated', receipt_control_instance_id: $control_instance_id,
  receipt_run_id: $run_id, receipt_owner_id: $owner_id,
  receipt_token_digest: $token_digest, receipt_state: 'allocated'})
OPTIONAL MATCH (acceptance:CrmDealRepairAcceptance {run_id: $run_id,
  request_digest: $request_digest, control_revision: $revision})
OPTIONAL MATCH (release:CrmDealRepairDispatchRelease {run_id: $run_id,
  request_digest: $request_digest, control_revision: $revision})
OPTIONAL MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: $control_instance_id, blocked: true,
  block_reason: 'crm_deal_identity_repair_quiesce', repair_run_id: $run_id,
  repair_owner_id: $owner_id, repair_token_digest: $token_digest,
  repair_revision: $revision})
WITH control, completion, acceptance, release, dispatch
WHERE acceptance IS NOT NULL OR release IS NOT NULL OR dispatch IS NOT NULL
RETURN properties(completion) AS completion,
  control.sealed_boundary_digest AS sealed_boundary_digest,
  completion.allocation_revision AS allocation_revision
"""

READ_UNIT_FOR_ADMISSION = """
MATCH (unit:CrmDealRepairUnit {run_id: $run_id, unit_id: $unit_id})
RETURN properties(unit) AS unit
"""

CLAIM_ADMITTED_FENCE = (
    _AUTHORITY
    + """
MATCH (unit:CrmDealRepairUnit {run_id: $run_id, unit_id: $unit_id,
  generation: $generation, sequence: $sequence, attempt: $attempt,
  boundary_digest: $boundary_digest, inventory_fingerprint: $inventory_fingerprint,
  inventory_binding_digest: $inventory_binding_digest})
WHERE unit.unit_id IN completion.unit_ids
// Serialize competing admissions on the run control in this same transaction.
SET control.integration_admission_updated_at = datetime()
WITH control, completion, unit
OPTIONAL MATCH (accepted:CrmDealRepairAcceptance {run_id: $run_id})
OPTIONAL MATCH (all_fences:CrmDealRepairFence {run_id: $run_id, unit_id: $unit_id})
CALL {
  WITH completion
  OPTIONAL MATCH (prior:CrmDealRepairUnit {run_id: completion.run_id})
  WHERE prior.sequence < $sequence
  OPTIONAL MATCH (prior_fence:CrmDealRepairFence {run_id: completion.run_id, unit_id: prior.unit_id})
  OPTIONAL MATCH (prior_result:CrmDealRepairMutationResult {run_id: completion.run_id,
    unit_id: prior.unit_id})
  OPTIONAL MATCH (prior_image:CrmDealRepairRollbackImage {run_id: completion.run_id,
    unit_id: prior.unit_id})
  OPTIONAL MATCH (prior_authorization:CrmDealRepairRollbackAuthorization {run_id: completion.run_id,
    unit_id: prior.unit_id})
  WITH prior, prior_fence, prior_result, prior_image, prior_authorization
  ORDER BY prior.sequence, prior.unit_id, prior_authorization.authorization_transition_id,
    prior_fence.fence_id, prior_result.mutation_id, prior_image.rollback_image_id
  FOREACH (_ IN CASE WHEN prior IS NULL THEN [] ELSE [1] END | SET prior.unit_id = prior.unit_id)
  FOREACH (_ IN CASE WHEN prior_fence IS NULL THEN [] ELSE [1] END |
    SET prior_fence.fence_id = prior_fence.fence_id)
  FOREACH (_ IN CASE WHEN prior_result IS NULL THEN [] ELSE [1] END |
    SET prior_result.mutation_id = prior_result.mutation_id)
  FOREACH (_ IN CASE WHEN prior_image IS NULL THEN [] ELSE [1] END |
    SET prior_image.rollback_image_id = prior_image.rollback_image_id)
  FOREACH (_ IN CASE WHEN prior_authorization IS NULL THEN [] ELSE [1] END |
    SET prior_authorization.authorization_transition_id = prior_authorization.authorization_transition_id)
  RETURN count(DISTINCT prior) AS locked_prior_count
}
WITH unit, completion, accepted, all_fences
CALL {
  WITH completion
  OPTIONAL MATCH (prior:CrmDealRepairUnit {run_id: completion.run_id})
  WHERE prior.sequence < $sequence
  WITH completion, collect(prior) AS prior_units
  UNWIND CASE WHEN size(prior_units) = 0 THEN [NULL] ELSE prior_units END AS prior
  CALL {
    WITH completion, prior
    MATCH (prior_fence:CrmDealRepairFence {run_id: completion.run_id,
      unit_id: prior.unit_id})
    RETURN count(prior_fence) AS fence_count
  }
  CALL {
    WITH completion, prior
    MATCH (mutation:CrmDealRepairMutationResult {run_id: completion.run_id,
      unit_id: prior.unit_id, generation: prior.generation, sequence: prior.sequence,
      attempt: prior.attempt, owner_id: $owner_id, fence_token: $token_digest,
      boundary_digest: $boundary_digest})
    RETURN count(mutation) AS mutation_count
  }
  CALL {
    WITH completion, prior
    MATCH (verification:CrmDealRepairVerification {run_id: completion.run_id,
      unit_id: prior.unit_id, generation: prior.generation, sequence: prior.sequence,
      attempt: prior.attempt, owner_id: $owner_id, fence_token: $token_digest,
      boundary_digest: $boundary_digest, outcome: 'verified'})
    RETURN count(verification) AS verification_count
  }
  CALL {
    WITH completion, prior
    MATCH (receipt:CrmDealRepairRollbackReceipt {run_id: completion.run_id,
      unit_id: prior.unit_id, generation: prior.generation, sequence: prior.sequence,
      attempt: prior.attempt, control_revision: $revision,
      allocation_revision: $allocation_revision, completion_id: $completion_id,
      state: 'available'})
    RETURN count(receipt) AS receipt_count
  }
  CALL {
    WITH completion, prior
    MATCH (prior_fence:CrmDealRepairFence {run_id: completion.run_id,
      unit_id: prior.unit_id, generation: prior.generation, sequence: prior.sequence,
      attempt: prior.attempt, owner_id: $owner_id, token: $token_digest,
      boundary_digest: $boundary_digest, state: 'claimed'})
    MATCH (mutation:CrmDealRepairMutationResult {run_id: completion.run_id,
      unit_id: prior.unit_id, generation: prior.generation, sequence: prior.sequence,
      attempt: prior.attempt, owner_id: $owner_id, fence_token: $token_digest,
      boundary_digest: $boundary_digest})
    MATCH (image:CrmDealRepairRollbackImage {run_id: completion.run_id,
      unit_id: prior.unit_id, rollback_image_id: mutation.rollback_image_id,
      image_digest: mutation.rollback_image_digest, generation: prior.generation,
      sequence: prior.sequence, attempt: prior.attempt, owner_id: $owner_id,
      fence_token: $token_digest, boundary_digest: $boundary_digest, state: 'available'})
    MATCH (authorization:CrmDealRepairRollbackAuthorization {run_id: completion.run_id,
      unit_id: prior.unit_id, fence_id: prior_fence.fence_id, mutation_id: mutation.mutation_id,
      rollback_image_id: image.rollback_image_id, image_digest: image.image_digest,
      generation: prior.generation, sequence: prior.sequence, attempt: prior.attempt,
      owner_id: $owner_id, fence_token: $token_digest, boundary_digest: $boundary_digest,
      state: 'approved', consumable: true})
    MATCH (receipt:CrmDealRepairRollbackReceipt {run_id: completion.run_id,
      unit_id: prior.unit_id, fence_id: prior_fence.fence_id, mutation_id: mutation.mutation_id,
      image_digest: image.image_digest,
      authorization_transition_id: authorization.authorization_transition_id,
      authorization_digest: authorization.authorization_digest, generation: prior.generation,
      sequence: prior.sequence, attempt: prior.attempt, control_revision: $revision,
      allocation_revision: $allocation_revision, completion_id: $completion_id,
      state: 'available'})
    MATCH (verification:CrmDealRepairVerification {run_id: completion.run_id,
      unit_id: prior.unit_id, generation: prior.generation, sequence: prior.sequence,
      attempt: prior.attempt, owner_id: $owner_id, fence_token: $token_digest,
      boundary_digest: $boundary_digest, outcome: 'verified'})
    WHERE receipt.status_digest STARTS WITH 'sha256:' AND size(receipt.status_digest) = 71
      AND authorization.authorization_digest STARTS WITH 'sha256:'
      AND size(authorization.authorization_digest) = 71
      AND mutation.rollback_image_digest = image.image_digest
      AND authorization.predecessor_transition_id =
        mutation.mutation_id + ':applied:' + image.rollback_image_id
    RETURN count(*) AS exact_chain_count
  }
  WITH prior_units, prior, fence_count, mutation_count, verification_count, receipt_count,
    exact_chain_count
  RETURN prior_units, collect(CASE WHEN prior IS NOT NULL
      AND prior.state IN ['applied', 'review_required']
      AND fence_count = 1 AND mutation_count = 1 AND verification_count = 1
      AND receipt_count = 1 AND exact_chain_count = 1
    THEN prior.unit_id END) AS settled_prior_unit_ids
}
WITH unit, completion, accepted, collect(all_fences) AS stored, prior_units,
  settled_prior_unit_ids
WHERE accepted IS NULL
  AND ((unit.state = 'allocated' AND size(stored) = 0
      AND size(prior_units) = $sequence
      AND size(settled_prior_unit_ids) = size(prior_units))
    OR (size(stored) = 1 AND stored[0].fence_id = $fence_id
      AND stored[0].generation = $generation AND stored[0].sequence = $sequence
      AND stored[0].attempt = $attempt AND stored[0].owner_id = $owner_id
      AND stored[0].token = $token_digest AND stored[0].boundary_digest = $boundary_digest
      AND stored[0].fence_fingerprint = $fence_fingerprint AND stored[0].state = 'claimed'))
MERGE (fence:CrmDealRepairFence {run_id: $run_id, fence_id: $fence_id})
ON CREATE SET fence.unit_id = $unit_id, fence.generation = $generation,
  fence.sequence = $sequence, fence.attempt = $attempt, fence.owner_id = $owner_id,
  fence.token = $token_digest, fence.boundary_digest = $boundary_digest,
  fence.fence_fingerprint = $fence_fingerprint, fence.state = 'claimed',
  fence.claimed_at = datetime()
WITH unit, fence
WHERE fence.unit_id = $unit_id AND fence.generation = $generation
  AND fence.sequence = $sequence AND fence.attempt = $attempt AND fence.owner_id = $owner_id
  AND fence.token = $token_digest AND fence.boundary_digest = $boundary_digest
  AND fence.fence_fingerprint = $fence_fingerprint AND fence.state = 'claimed'
RETURN properties(unit) AS unit, properties(fence) AS fence
"""
)

READ_FENCE = (
    _AUTHORITY
    + """
MATCH (unit:CrmDealRepairUnit {run_id: $run_id, unit_id: $unit_id,
  boundary_digest: $boundary_digest})
MATCH (fence:CrmDealRepairFence {run_id: $run_id, unit_id: $unit_id,
  owner_id: $owner_id, token: $token_digest, boundary_digest: $boundary_digest,
  state: 'claimed'})
OPTIONAL MATCH (extra:CrmDealRepairFence {run_id: $run_id, unit_id: $unit_id})
WITH unit, fence, collect(extra) AS fences, completion
WHERE size(fences) = 1 AND unit.unit_id IN completion.unit_ids
RETURN properties(unit) AS unit, properties(fence) AS fence
"""
)

READ_TERMINAL_ROLLBACK_REPLAY = (
    _BASE_AUTHORITY
    + """
MATCH (unit:CrmDealRepairUnit {run_id: $run_id, unit_id: $unit_id,
  boundary_digest: $boundary_digest})
MATCH (fence:CrmDealRepairFence {run_id: $run_id, unit_id: $unit_id,
  fence_id: $fence_id, generation: unit.generation, sequence: unit.sequence,
  attempt: unit.attempt, owner_id: $owner_id, token: $token_digest,
  boundary_digest: $boundary_digest, fence_fingerprint: $fence_fingerprint,
  state: 'released'})
OPTIONAL MATCH (extra:CrmDealRepairFence {run_id: $run_id, unit_id: $unit_id})
WITH unit, fence, completion, collect(extra) AS fences
WHERE size(fences) = 1
MATCH (result:CrmDealRepairMutationResult {run_id: $run_id, unit_id: $unit_id,
  generation: unit.generation, sequence: unit.sequence, attempt: unit.attempt,
  owner_id: $owner_id, fence_token: $token_digest, boundary_digest: $boundary_digest})
MATCH (image:CrmDealRepairRollbackImage {run_id: $run_id, unit_id: $unit_id,
  rollback_image_id: result.rollback_image_id, image_digest: result.rollback_image_digest,
  generation: unit.generation, sequence: unit.sequence, attempt: unit.attempt,
  owner_id: $owner_id, fence_token: $token_digest, boundary_digest: $boundary_digest})
MATCH (authorization:CrmDealRepairRollbackAuthorization {run_id: $run_id,
  unit_id: $unit_id, fence_id: fence.fence_id, mutation_id: result.mutation_id,
  rollback_image_id: image.rollback_image_id, image_digest: image.image_digest,
  generation: unit.generation, sequence: unit.sequence, attempt: unit.attempt,
  owner_id: $owner_id, fence_token: $token_digest, boundary_digest: $boundary_digest,
  authorization_transition_id: $authorization_transition_id,
  authorization_reference: $authorization_reference,
  authorization_token_digest: $authorization_token_digest,
  predecessor_transition_id: $predecessor_transition_id,
  authorization_policy: $authorization_policy, state: 'consumed', consumable: false})
MATCH (disposition:CrmDealRepairSecondaryDisposition {run_id: $run_id,
  unit_id: $unit_id, disposition_id: authorization.consumed_disposition_id,
  generation: unit.generation, sequence: unit.sequence, attempt: unit.attempt,
  owner_id: $owner_id, control_token: $token_digest, boundary_digest: $boundary_digest,
  subject_fingerprint: image.image_digest, rollback_image_id: image.rollback_image_id,
  authorization_reference: $authorization_reference,
  authorization_token_digest: $authorization_token_digest,
  authorization_transition_id: $authorization_transition_id,
  predecessor_transition_id: $predecessor_transition_id,
  authorization_policy: $authorization_policy,
  rollback_request_digest: authorization.consumed_request_digest,
  result_digest: authorization.consumed_result_digest})
WHERE unit.unit_id IN completion.unit_ids
  AND unit.rollback_disposition_id = disposition.disposition_id
  AND image.state IN ['restored', 'review_required']
  AND image.rollback_disposition_id = disposition.disposition_id
  AND image.rollback_result_digest = authorization.consumed_result_digest
  AND image.rollback_status_digest = disposition.rollback_status_digest
  AND result.rollback_image_digest = image.image_digest
  AND result.evidence_digest = image.evidence_digest
  AND result.payload_digest = image.payload_digest
  AND authorization.predecessor_transition_id = result.mutation_id + ':applied:' + image.rollback_image_id
  AND ((unit.state = 'rolled_back' AND image.state = 'restored'
      AND disposition.outcome = 'reconciled')
    OR (unit.state = 'review_required' AND image.state = 'review_required'
      AND disposition.outcome = 'review_required'))
RETURN properties(unit) AS unit, properties(fence) AS fence, properties(result) AS result,
  properties(image) AS image, properties(authorization) AS authorization
"""
)

CREATE_AND_READ_ROLLBACK_AUTHORIZATION = (
    _AUTHORITY
    + """
MATCH (unit:CrmDealRepairUnit {run_id: $run_id, unit_id: $unit_id, generation: $generation,
  sequence: $sequence, attempt: $attempt, boundary_digest: $boundary_digest,
  inventory_fingerprint: $inventory_fingerprint, inventory_binding_digest: $inventory_binding_digest})
MATCH (fence:CrmDealRepairFence {run_id: $run_id, unit_id: $unit_id, fence_id: $fence_id,
  generation: $generation, sequence: $sequence, attempt: $attempt, owner_id: $owner_id,
  token: $fence_token, boundary_digest: $boundary_digest,
  fence_fingerprint: $fence_fingerprint, state: 'claimed'})
MATCH (result:CrmDealRepairMutationResult {run_id: $run_id, unit_id: $unit_id,
  generation: $generation, sequence: $sequence, attempt: $attempt, owner_id: $owner_id,
  fence_token: $fence_token, boundary_digest: $boundary_digest})
MATCH (image:CrmDealRepairRollbackImage {run_id: $run_id, unit_id: $unit_id,
  generation: $generation, sequence: $sequence, attempt: $attempt, owner_id: $owner_id,
  fence_token: $fence_token, boundary_digest: $boundary_digest})
WHERE unit.unit_id IN completion.unit_ids AND result.rollback_image_id = image.rollback_image_id
  AND result.rollback_image_digest = image.image_digest
MERGE (authorization:CrmDealRepairRollbackAuthorization {run_id: $run_id,
  unit_id: $unit_id, rollback_image_id: image.rollback_image_id})
ON CREATE SET authorization.unit_id = $unit_id,
  authorization.authorization_transition_id = $authorization_transition_id,
  authorization.authorization_reference = $authorization_reference,
  authorization.authorization_token_digest = $authorization_token_digest,
  authorization.predecessor_transition_id = $predecessor_transition_id,
  authorization.authorization_policy = $authorization_policy,
  authorization.generation = $generation, authorization.sequence = $sequence,
  authorization.attempt = $attempt, authorization.boundary_digest = $boundary_digest,
  authorization.fence_id = $fence_id, authorization.owner_id = $owner_id,
  authorization.fence_token = $fence_token, authorization.mutation_id = result.mutation_id,
  authorization.rollback_image_id = image.rollback_image_id, authorization.image_digest = image.image_digest,
  authorization.state = 'approved', authorization.consumable = true, authorization.created_at = datetime()
WITH unit, fence, result, image, authorization
WHERE authorization.unit_id = $unit_id AND authorization.authorization_reference = $authorization_reference
  AND authorization.authorization_token_digest = $authorization_token_digest
  AND authorization.predecessor_transition_id = $predecessor_transition_id
  AND authorization.authorization_policy = $authorization_policy
  AND authorization.fence_id = $fence_id AND authorization.mutation_id = result.mutation_id
  AND authorization.rollback_image_id = image.rollback_image_id
  AND authorization.image_digest = image.image_digest
  AND ((image.state = 'available' AND authorization.state = 'approved'
      AND authorization.consumable = true)
    OR (image.state IN ['restored', 'review_required'] AND authorization.state = 'consumed'
      AND authorization.consumable = false))
RETURN properties(unit) AS unit, properties(fence) AS fence, properties(result) AS result,
  properties(image) AS image, properties(authorization) AS authorization
"""
)

STORE_ROLLBACK_RECEIPT = (
    _AUTHORITY
    + """
MATCH (fence:CrmDealRepairFence {run_id: $run_id, unit_id: $unit_id, fence_id: $fence_id,
  owner_id: $owner_id, token: $token_digest, state: 'claimed'})
MATCH (image:CrmDealRepairRollbackImage {run_id: $run_id, unit_id: $unit_id,
  image_digest: $image_digest, state: 'available', generation: $generation,
  sequence: $sequence, attempt: $attempt})
MATCH (result:CrmDealRepairMutationResult {run_id: $run_id, unit_id: $unit_id,
  mutation_id: $mutation_id, rollback_image_digest: $image_digest})
MATCH (authorization:CrmDealRepairRollbackAuthorization {run_id: $run_id,
  authorization_transition_id: $authorization_transition_id, unit_id: $unit_id,
  mutation_id: $mutation_id, rollback_image_id: image.rollback_image_id,
  image_digest: $image_digest, state: 'approved', consumable: true})
WHERE authorization.authorization_digest IS NULL
  OR authorization.authorization_digest = $authorization_digest
SET authorization.authorization_digest = $authorization_digest
MERGE (receipt:CrmDealRepairRollbackReceipt {run_id: $run_id, receipt_id: $receipt_id})
ON CREATE SET receipt.unit_id = $unit_id, receipt.fence_id = $fence_id,
  receipt.request_digest = $request_digest, receipt.status_digest = $status_digest,
  receipt.image_digest = $image_digest, receipt.mutation_id = $mutation_id,
  receipt.authorization_transition_id = $authorization_transition_id,
  receipt.authorization_digest = $authorization_digest, receipt.receipt_digest = $receipt_digest,
  receipt.generation = $generation,
  receipt.sequence = $sequence, receipt.attempt = $attempt,
  receipt.control_revision = $revision, receipt.allocation_revision = $allocation_revision,
  receipt.completion_id = $completion_id, receipt.state = 'available', receipt.created_at = datetime()
WITH receipt
WHERE receipt.unit_id = $unit_id AND receipt.fence_id = $fence_id
  AND receipt.request_digest = $request_digest AND receipt.status_digest = $status_digest
  AND receipt.image_digest = $image_digest AND receipt.mutation_id = $mutation_id
  AND receipt.authorization_transition_id = $authorization_transition_id
  AND receipt.authorization_digest = $authorization_digest AND receipt.receipt_digest = $receipt_digest
  AND receipt.generation = $generation
  AND receipt.sequence = $sequence AND receipt.attempt = $attempt
  AND receipt.control_revision = $revision AND receipt.allocation_revision = $allocation_revision
  AND receipt.completion_id = $completion_id AND receipt.state = 'available'
RETURN receipt.receipt_id AS receipt_id, receipt.receipt_digest AS receipt_digest
"""
)

READ_RUN_RECEIPTS = (
    _BASE_AUTHORITY
    + """
MATCH (completion:CrmDealRepairAllocationCompletion {run_id: $run_id,
  completion_id: $completion_id, boundary_digest: $boundary_digest})
CALL {
  WITH completion
  OPTIONAL MATCH (receipt:CrmDealRepairRollbackReceipt {run_id: completion.run_id})
  RETURN [item IN collect(receipt) WHERE item IS NOT NULL | properties(item)] AS receipts
}
RETURN completion.unit_count AS unit_count, receipts
"""
)

RELEASE_TERMINAL_FENCE = (
    _BASE_AUTHORITY
    + """
MATCH (fence:CrmDealRepairFence {run_id: $run_id, unit_id: $unit_id, fence_id: $fence_id,
  owner_id: $owner_id, token: $token_digest, boundary_digest: $boundary_digest})
MATCH (image:CrmDealRepairRollbackImage {run_id: $run_id, unit_id: $unit_id,
  image_digest: $image_digest})
MATCH (authorization:CrmDealRepairRollbackAuthorization {run_id: $run_id,
  authorization_transition_id: $authorization_transition_id, unit_id: $unit_id,
  fence_id: $fence_id, state: 'consumed', consumable: false,
  consumed_result_digest: $result_digest, rollback_image_id: image.rollback_image_id})
MATCH (disposition:CrmDealRepairSecondaryDisposition {run_id: $run_id,
  disposition_id: authorization.consumed_disposition_id, result_digest: $result_digest,
  authorization_transition_id: $authorization_transition_id})
WHERE image.state IN ['restored', 'review_required'] AND fence.state IN ['claimed', 'released']
FOREACH (_ IN CASE WHEN fence.state = 'claimed' THEN [1] ELSE [] END |
  SET fence.state = 'released', fence.released_at = datetime(),
    fence.release_result_digest = $result_digest)
RETURN fence.fence_id AS fence_id
"""
)

READ_UNIT_EXECUTION_EVIDENCE = """
MATCH (unit:CrmDealRepairUnit {run_id: $run_id, unit_id: $unit_id})
OPTIONAL MATCH (fence:CrmDealRepairFence {run_id: $run_id, unit_id: unit.unit_id})
OPTIONAL MATCH (mutation:CrmDealRepairMutationResult {run_id: $run_id, unit_id: unit.unit_id})
RETURN count(fence) + count(mutation) > 0 AS exists
"""

READ_ANY_EXECUTION_EVIDENCE = """
MATCH (unit:CrmDealRepairUnit {run_id: $run_id})
OPTIONAL MATCH (fence:CrmDealRepairFence {run_id: $run_id, unit_id: unit.unit_id})
OPTIONAL MATCH (mutation:CrmDealRepairMutationResult {run_id: $run_id, unit_id: unit.unit_id})
RETURN count(fence) + count(mutation) > 0 AS exists
"""

READ_RUN_SETS = """
MATCH (completion:CrmDealRepairAllocationCompletion {run_id: $run_id,
  boundary_digest: $boundary_digest, unit_set_digest: $allocation_unit_set_digest})
CALL {
  WITH completion
  WITH completion, completion.unit_ids AS allocated_unit_ids
  OPTIONAL MATCH (unit:CrmDealRepairUnit {run_id: completion.run_id})
  WITH allocated_unit_ids, collect(unit) AS all_units
  RETURN [item IN all_units WHERE item IS NOT NULL AND item.unit_id IN allocated_unit_ids
    | properties(item)] AS units
}
CALL {
  WITH completion
  WITH completion, completion.unit_ids AS allocated_unit_ids
  OPTIONAL MATCH (fence:CrmDealRepairFence {run_id: completion.run_id})
  WITH allocated_unit_ids, collect(fence) AS all_fences
  RETURN [item IN all_fences WHERE item IS NOT NULL AND item.unit_id IN allocated_unit_ids
    | properties(item)] AS fences
}
RETURN completion.unit_count AS unit_count, completion.unit_ids AS unit_ids, units, fences
"""


_ACCEPTANCE_LOCKS = """
// The first write locks the integration control; the ordered bundle locks below
// contend with #312 on the same unit, authorization, fence, result, and image nodes.
SET control.integration_acceptance_lock_id = $request_digest
WITH control, completion
CALL {
  WITH completion
  OPTIONAL MATCH (unit:CrmDealRepairUnit {run_id: completion.run_id})
  WHERE unit IS NULL OR unit.unit_id IN completion.unit_ids
  OPTIONAL MATCH (fence:CrmDealRepairFence {run_id: completion.run_id, unit_id: unit.unit_id})
  OPTIONAL MATCH (result:CrmDealRepairMutationResult {run_id: completion.run_id,
    unit_id: unit.unit_id})
  OPTIONAL MATCH (image:CrmDealRepairRollbackImage {run_id: completion.run_id,
    unit_id: unit.unit_id})
  OPTIONAL MATCH (authorization:CrmDealRepairRollbackAuthorization {run_id: completion.run_id,
    unit_id: unit.unit_id})
  WITH unit, fence, result, image, authorization
  ORDER BY unit.sequence, unit.unit_id, authorization.authorization_transition_id, fence.fence_id,
    result.mutation_id, image.rollback_image_id
  FOREACH (_ IN CASE WHEN unit IS NULL THEN [] ELSE [1] END |
    SET unit.unit_id = unit.unit_id)
  FOREACH (_ IN CASE WHEN authorization IS NULL THEN [] ELSE [1] END |
    SET authorization.authorization_transition_id = authorization.authorization_transition_id)
  FOREACH (_ IN CASE WHEN fence IS NULL THEN [] ELSE [1] END |
    SET fence.fence_id = fence.fence_id)
  FOREACH (_ IN CASE WHEN result IS NULL THEN [] ELSE [1] END |
    SET result.mutation_id = result.mutation_id)
  FOREACH (_ IN CASE WHEN image IS NULL THEN [] ELSE [1] END |
    SET image.rollback_image_id = image.rollback_image_id)
  RETURN count(DISTINCT unit) AS locked_unit_count
}
"""

LOCK_ACCEPTANCE_SCOPE = (
    _BASE_AUTHORITY
    + _ACCEPTANCE_LOCKS
    + """
WITH completion, locked_unit_count
WHERE locked_unit_count = completion.unit_count
RETURN locked_unit_count
"""
)


ACCEPT_AND_RELEASE = (
    _BASE_AUTHORITY
    + _ACCEPTANCE_LOCKS
    + """
WITH completion, locked_unit_count
WHERE locked_unit_count = completion.unit_count
OPTIONAL MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: $control_instance_id, blocked: true,
  block_reason: 'crm_deal_identity_repair_quiesce', repair_run_id: $run_id,
  repair_owner_id: $owner_id, repair_token_digest: $token_digest,
  repair_revision: $revision})
OPTIONAL MATCH (prior:CrmDealRepairAcceptance {run_id: $run_id})
WITH completion, dispatch, prior
WHERE completion.unit_set_digest = $computed_allocation_unit_set_digest
  AND (prior IS NULL OR (prior.request_digest = $request_digest
    AND prior.unit_set_digest = $unit_set_digest AND prior.fence_set_digest = $fence_set_digest
    AND prior.equation_digest = $equation_digest AND prior.control_revision = $revision
    AND prior.receipt_digest = $acceptance_receipt_digest))
  AND (prior IS NOT NULL OR dispatch IS NOT NULL)
CALL {
  WITH completion
  WITH completion, completion.unit_ids AS allocated_unit_ids
  OPTIONAL MATCH (unit:CrmDealRepairUnit {run_id: completion.run_id})
  WITH completion, allocated_unit_ids, collect(unit) AS all_unit_nodes
  RETURN [item IN all_unit_nodes
    WHERE item IS NOT NULL AND item.unit_id IN allocated_unit_ids | item] AS units,
    size([item IN all_unit_nodes WHERE item IS NOT NULL]) AS all_units,
    size([item IN all_unit_nodes
      WHERE item IS NOT NULL AND item.unit_id IN allocated_unit_ids]) AS allocated_units
}
CALL {
  WITH completion
  WITH completion, completion.unit_ids AS allocated_unit_ids
  OPTIONAL MATCH (fence:CrmDealRepairFence {run_id: completion.run_id})
  WITH allocated_unit_ids, collect(fence) AS all_fence_nodes
  RETURN [item IN all_fence_nodes
    WHERE item IS NOT NULL AND item.unit_id IN allocated_unit_ids | item] AS fences,
    size([item IN all_fence_nodes WHERE item IS NOT NULL]) AS all_fences,
    size([item IN all_fence_nodes
      WHERE item IS NOT NULL AND item.unit_id IN allocated_unit_ids]) AS allocated_fences
}
CALL {
  WITH completion
  WITH completion, completion.unit_ids AS allocated_unit_ids
  OPTIONAL MATCH (mutation:CrmDealRepairMutationResult {run_id: completion.run_id})
  WITH allocated_unit_ids, collect(mutation) AS nodes
  RETURN size([item IN nodes WHERE item IS NOT NULL]) AS all_mutations,
    size([item IN nodes WHERE item IS NOT NULL AND item.unit_id IN allocated_unit_ids])
      AS allocated_mutations
}
CALL {
  WITH completion
  WITH completion, completion.unit_ids AS allocated_unit_ids
  OPTIONAL MATCH (image:CrmDealRepairRollbackImage {run_id: completion.run_id})
  WITH allocated_unit_ids, collect(image) AS nodes
  RETURN size([item IN nodes WHERE item IS NOT NULL]) AS all_images,
    size([item IN nodes WHERE item IS NOT NULL AND item.unit_id IN allocated_unit_ids])
      AS allocated_images
}
CALL {
  WITH completion
  WITH completion, completion.unit_ids AS allocated_unit_ids
  OPTIONAL MATCH (verification:CrmDealRepairVerification {run_id: completion.run_id})
  WITH allocated_unit_ids, collect(verification) AS nodes
  RETURN size([item IN nodes WHERE item IS NOT NULL]) AS all_verifications,
    size([item IN nodes WHERE item IS NOT NULL AND item.unit_id IN allocated_unit_ids])
      AS allocated_verifications
}
CALL {
  WITH completion
  WITH completion, completion.unit_ids AS allocated_unit_ids
  OPTIONAL MATCH (authorization:CrmDealRepairRollbackAuthorization {run_id: completion.run_id})
  WITH allocated_unit_ids, collect(authorization) AS nodes
  RETURN size([item IN nodes WHERE item IS NOT NULL]) AS all_authorizations,
    size([item IN nodes WHERE item IS NOT NULL AND item.unit_id IN allocated_unit_ids])
      AS allocated_authorizations
}
CALL {
  WITH completion
  WITH completion, completion.unit_ids AS allocated_unit_ids
  OPTIONAL MATCH (receipt:CrmDealRepairRollbackReceipt {run_id: completion.run_id})
  WITH allocated_unit_ids, collect(receipt) AS nodes
  RETURN size([item IN nodes WHERE item IS NOT NULL]) AS all_receipts,
    size([item IN nodes WHERE item IS NOT NULL AND item.unit_id IN allocated_unit_ids])
      AS allocated_receipts
}
CALL {
  WITH completion
  WITH completion, completion.unit_ids AS allocated_unit_ids
  OPTIONAL MATCH (checkpoint:CrmDealRepairCheckpoint {run_id: completion.run_id})
  WITH allocated_unit_ids, collect(checkpoint) AS nodes
  RETURN size([item IN nodes WHERE item IS NOT NULL]) AS all_checkpoints,
    size([item IN nodes WHERE item IS NOT NULL AND item.unit_id IN allocated_unit_ids])
      AS allocated_checkpoints
}
CALL {
  WITH completion
  WITH completion, completion.unit_ids AS allocated_unit_ids
  OPTIONAL MATCH (outbox:CrmDealRepairOutbox {run_id: completion.run_id})
  WITH allocated_unit_ids, collect(outbox) AS nodes
  RETURN size([item IN nodes WHERE item IS NOT NULL]) AS all_outboxes,
    size([item IN nodes WHERE item IS NOT NULL AND item.unit_id IN allocated_unit_ids])
      AS allocated_outboxes
}
CALL {
  WITH completion
  WITH completion, completion.unit_ids AS allocated_unit_ids
  OPTIONAL MATCH (disposition:CrmDealRepairSecondaryDisposition {run_id: completion.run_id})
  WITH allocated_unit_ids, collect(disposition) AS nodes
  RETURN size([item IN nodes WHERE item IS NOT NULL]) AS all_dispositions,
    size([item IN nodes WHERE item IS NOT NULL AND item.unit_id IN allocated_unit_ids])
      AS allocated_dispositions,
    size([item IN nodes WHERE item IS NOT NULL AND item.unit_id IN allocated_unit_ids
      AND item.outcome = 'reconciled']) AS reconciled_dispositions,
    size([item IN nodes WHERE item IS NOT NULL AND item.unit_id IN allocated_unit_ids
      AND item.outcome = 'review_required']) AS review_required_dispositions,
    size([item IN nodes WHERE item IS NOT NULL AND item.unit_id IN allocated_unit_ids
      AND item.outcome = 'failed']) AS failed_dispositions,
    size([item IN nodes WHERE item IS NOT NULL AND item.unit_id IN allocated_unit_ids
      AND item.outcome = 'pending']) AS pending_dispositions
}
CALL {
  WITH completion
  WITH completion, $receipt_bindings AS bindings
  UNWIND CASE WHEN size(bindings) = 0 THEN [NULL] ELSE bindings END AS binding
  OPTIONAL MATCH (receipt:CrmDealRepairRollbackReceipt {run_id: completion.run_id,
    receipt_id: binding.receipt_id, receipt_digest: binding.receipt_digest})
  RETURN count(receipt) AS bound_receipts, count(DISTINCT receipt) AS distinct_bound_receipts
}
CALL {
  WITH completion
  WITH completion, completion.unit_ids AS allocated_unit_ids
  OPTIONAL MATCH (candidate:CrmDealRepairMutationResult {run_id: completion.run_id})
  WITH completion, allocated_unit_ids, collect(candidate) AS all_candidates
  WITH completion, [item IN all_candidates
    WHERE item IS NOT NULL AND item.unit_id IN allocated_unit_ids] AS candidate_mutations
  UNWIND CASE WHEN size(candidate_mutations) = 0 THEN [NULL] ELSE candidate_mutations END AS mutation
  OPTIONAL MATCH (image:CrmDealRepairRollbackImage {run_id: completion.run_id,
    unit_id: mutation.unit_id, rollback_image_id: mutation.rollback_image_id,
    image_digest: mutation.rollback_image_digest, generation: mutation.generation,
    sequence: mutation.sequence, attempt: mutation.attempt, owner_id: mutation.owner_id,
    fence_token: mutation.fence_token, boundary_digest: mutation.boundary_digest})
  OPTIONAL MATCH (verification:CrmDealRepairVerification {run_id: completion.run_id,
    unit_id: mutation.unit_id, generation: mutation.generation, sequence: mutation.sequence,
    attempt: mutation.attempt, owner_id: mutation.owner_id, fence_token: mutation.fence_token,
    boundary_digest: mutation.boundary_digest, outcome: 'verified'})
  OPTIONAL MATCH (authorization:CrmDealRepairRollbackAuthorization {run_id: completion.run_id,
    unit_id: mutation.unit_id, mutation_id: mutation.mutation_id,
    rollback_image_id: mutation.rollback_image_id, image_digest: mutation.rollback_image_digest,
    generation: mutation.generation, sequence: mutation.sequence, attempt: mutation.attempt,
    boundary_digest: mutation.boundary_digest, owner_id: mutation.owner_id,
    fence_token: mutation.fence_token, state: 'approved', consumable: true})
  OPTIONAL MATCH (checkpoint:CrmDealRepairCheckpoint {run_id: completion.run_id,
    unit_id: mutation.unit_id, checkpoint_id: mutation.checkpoint_id,
    generation: mutation.generation, sequence: mutation.sequence, attempt: mutation.attempt,
    owner_id: mutation.owner_id, fence_token: mutation.fence_token,
    boundary_digest: mutation.boundary_digest, state: 'written'})
  OPTIONAL MATCH (outbox:CrmDealRepairOutbox {run_id: completion.run_id,
    unit_id: mutation.unit_id, event_id: mutation.outbox_event_id,
    generation: mutation.generation, sequence: mutation.sequence, attempt: mutation.attempt,
    owner_id: mutation.owner_id, delivery_token: mutation.fence_token,
    boundary_digest: mutation.boundary_digest, mutation_id: mutation.mutation_id,
    state: 'acknowledged'})
  WITH completion, mutation, image, verification, authorization, checkpoint, outbox,
    CASE WHEN outbox.verification_request_digest = verification.request_digest
      AND outbox.verification_result_digest = verification.verification_digest
    THEN outbox END AS acknowledged_outbox
  OPTIONAL MATCH (receipt:CrmDealRepairRollbackReceipt {run_id: completion.run_id,
    unit_id: mutation.unit_id, image_digest: mutation.rollback_image_digest,
    mutation_id: mutation.mutation_id, generation: mutation.generation,
    sequence: mutation.sequence, attempt: mutation.attempt, fence_id: authorization.fence_id,
    authorization_transition_id: authorization.authorization_transition_id,
    authorization_digest: authorization.authorization_digest, control_revision: $revision,
    allocation_revision: $allocation_revision, completion_id: $completion_id, state: 'available'})
  WITH mutation, image, verification, authorization, checkpoint, acknowledged_outbox,
    CASE WHEN receipt.status_digest STARTS WITH 'sha256:' AND size(receipt.status_digest) = 71
      THEN receipt END AS receipt
  RETURN count(DISTINCT mutation) AS mutations, count(DISTINCT image) AS images,
    count(DISTINCT verification) AS verifications, count(DISTINCT authorization) AS authorizations,
    count(DISTINCT checkpoint) AS checkpoints, count(DISTINCT acknowledged_outbox) AS outboxes,
    count(DISTINCT receipt) AS receipts
}
WITH completion, prior, units, fences, all_units, allocated_units, all_fences, allocated_fences,
  all_mutations, allocated_mutations, all_images, allocated_images, all_verifications,
  allocated_verifications, all_authorizations, allocated_authorizations, all_receipts,
  allocated_receipts, all_checkpoints, allocated_checkpoints, all_outboxes, allocated_outboxes,
  all_dispositions, allocated_dispositions, reconciled_dispositions,
  review_required_dispositions, failed_dispositions, pending_dispositions, bound_receipts,
  distinct_bound_receipts, mutations, images, verifications, authorizations, checkpoints, outboxes, receipts
WHERE all_units = completion.unit_count AND allocated_units = completion.unit_count
  AND all_fences = completion.unit_count AND allocated_fences = completion.unit_count
  AND all_mutations = completion.unit_count AND allocated_mutations = completion.unit_count
  AND all_images = completion.unit_count AND allocated_images = completion.unit_count
  AND all_verifications = completion.unit_count
  AND allocated_verifications = completion.unit_count
  AND all_authorizations = completion.unit_count
  AND allocated_authorizations = completion.unit_count
  AND all_receipts = completion.unit_count AND allocated_receipts = completion.unit_count
  AND all_checkpoints = completion.unit_count AND allocated_checkpoints = completion.unit_count
  AND all_outboxes = completion.unit_count AND allocated_outboxes = completion.unit_count
  AND all_dispositions = allocated_dispositions
  AND allocated_dispositions = $observed_secondary_count
  AND $expected_secondary_count = $observed_secondary_count
  AND reconciled_dispositions = $reconciled_secondaries
  AND review_required_dispositions = $review_required_secondaries
  AND failed_dispositions = $failed_secondaries AND pending_dispositions = $pending_secondaries
  AND allocated_dispositions = reconciled_dispositions + review_required_dispositions
    + failed_dispositions + pending_dispositions
  AND size(units) = completion.unit_count
  AND size([unit IN units WHERE unit.unit_id IN completion.unit_ids]) = completion.unit_count
  AND size(fences) = completion.unit_count
  AND (prior IS NOT NULL OR all(fence IN fences WHERE fence.state = 'claimed'
    AND fence.unit_id IN completion.unit_ids AND fence.owner_id = $owner_id
    AND fence.token = $token_digest AND fence.boundary_digest = $boundary_digest))
  AND bound_receipts = completion.unit_count AND distinct_bound_receipts = completion.unit_count
  AND size($receipt_bindings) = completion.unit_count AND mutations = completion.unit_count
  AND images = completion.unit_count AND verifications = completion.unit_count
  AND authorizations = completion.unit_count AND checkpoints = completion.unit_count
  AND outboxes = completion.unit_count AND receipts = completion.unit_count
FOREACH (fence IN CASE WHEN prior IS NULL THEN fences ELSE [] END |
  SET fence.state = 'released', fence.released_at = datetime(), fence.release_reason = 'accepted')
MERGE (acceptance:CrmDealRepairAcceptance {run_id: $run_id})
ON CREATE SET acceptance.request_digest = $request_digest, acceptance.unit_set_digest = $unit_set_digest,
  acceptance.fence_set_digest = $fence_set_digest, acceptance.equation_digest = $equation_digest,
  acceptance.control_revision = $revision, acceptance.receipt_digest = $acceptance_receipt_digest,
  acceptance.created_at = datetime()
WITH acceptance
WHERE acceptance.request_digest = $request_digest AND acceptance.unit_set_digest = $unit_set_digest
  AND acceptance.fence_set_digest = $fence_set_digest AND acceptance.equation_digest = $equation_digest
  AND acceptance.control_revision = $revision
  AND acceptance.receipt_digest = $acceptance_receipt_digest
RETURN acceptance.receipt_digest AS receipt_digest
"""
)

READ_ACCEPTANCE = """
MATCH (acceptance:CrmDealRepairAcceptance {run_id: $run_id})
RETURN acceptance.receipt_digest AS receipt_digest, acceptance.fence_set_digest AS fence_set_digest
"""

RELEASE_DISPATCH = (
    _BASE_AUTHORITY
    + """
MATCH (acceptance:CrmDealRepairAcceptance {run_id: $run_id,
  receipt_digest: $acceptance_receipt_digest, fence_set_digest: $fence_set_digest,
  control_revision: $revision})
OPTIONAL MATCH (prior:CrmDealRepairDispatchRelease {run_id: $run_id})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: $control_instance_id})
// Lock by stable dispatch identity without changing a replacement block's properties.
SET dispatch.control_instance_id = dispatch.control_instance_id
WITH acceptance, prior, dispatch
WHERE (prior IS NULL OR (prior.request_digest = $request_digest
  AND prior.acceptance_receipt_digest = $acceptance_receipt_digest
  AND prior.fence_set_digest = $fence_set_digest AND prior.control_revision = $revision))
  AND (prior IS NOT NULL OR (dispatch.blocked = true
    AND dispatch.block_reason = 'crm_deal_identity_repair_quiesce'
    AND dispatch.repair_run_id = $run_id AND dispatch.repair_owner_id = $owner_id
    AND dispatch.repair_token_digest = $token_digest AND dispatch.repair_revision = $revision))
FOREACH (_ IN CASE WHEN prior IS NULL THEN [1] ELSE [] END |
  SET dispatch.blocked = false, dispatch.block_reason = NULL, dispatch.repair_run_id = NULL,
    dispatch.repair_owner_id = NULL, dispatch.repair_token_digest = NULL,
    dispatch.repair_revision = NULL, dispatch.updated_at = datetime())
MERGE (release:CrmDealRepairDispatchRelease {run_id: $run_id})
ON CREATE SET release.request_digest = $request_digest,
  release.acceptance_receipt_digest = $acceptance_receipt_digest,
  release.fence_set_digest = $fence_set_digest, release.control_revision = $revision,
  release.created_at = datetime()
WITH release
WHERE release.request_digest = $request_digest
  AND release.acceptance_receipt_digest = $acceptance_receipt_digest
  AND release.fence_set_digest = $fence_set_digest AND release.control_revision = $revision
RETURN release.request_digest AS request_digest
"""
)
