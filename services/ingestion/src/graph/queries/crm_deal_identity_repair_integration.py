"""Parameterized #313 integration CAS queries; no CRM-domain semantics live here."""

CREATE_CRM_DEAL_REPAIR_INTEGRATION_SCHEMA = (
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

_AUTHORITY = _BASE_AUTHORITY + """
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: $control_instance_id, blocked: true,
  block_reason: 'crm_deal_identity_repair_quiesce', repair_run_id: $run_id,
  repair_owner_id: $owner_id, repair_token_digest: $token_digest,
  repair_revision: $revision})
"""

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

READ_ALLOCATED_UNIT = _AUTHORITY + """
MATCH (unit:CrmDealRepairUnit {run_id: $run_id, unit_id: $unit_id,
  boundary_digest: $boundary_digest})
WHERE unit.unit_id IN completion.unit_ids
OPTIONAL MATCH (stored:CrmDealRepairFence {run_id: $run_id, unit_id: $unit_id})
WITH unit, collect(stored) AS fences
WHERE (unit.state = 'allocated' AND size(fences) = 0)
  OR (size(fences) = 1 AND fences[0].generation = unit.generation
    AND fences[0].sequence = unit.sequence AND fences[0].attempt = unit.attempt
    AND fences[0].owner_id = $owner_id AND fences[0].token = $token_digest
    AND fences[0].boundary_digest = $boundary_digest AND fences[0].state = 'claimed')
RETURN properties(unit) AS unit
"""

CLAIM_FENCE = _AUTHORITY + """
MATCH (unit:CrmDealRepairUnit {run_id: $run_id, unit_id: $unit_id,
  generation: $generation, sequence: $sequence, attempt: $attempt,
  boundary_digest: $boundary_digest, inventory_fingerprint: $inventory_fingerprint,
  inventory_binding_digest: $inventory_binding_digest})
WHERE unit.unit_id IN completion.unit_ids
OPTIONAL MATCH (accepted:CrmDealRepairAcceptance {run_id: $run_id})
OPTIONAL MATCH (all_fences:CrmDealRepairFence {run_id: $run_id, unit_id: $unit_id})
WITH unit, completion, accepted, collect(all_fences) AS stored
WHERE accepted IS NULL
  AND ((unit.state = 'allocated' AND size(stored) = 0) OR (size(stored) = 1 AND stored[0].fence_id = $fence_id
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

READ_FENCE = _AUTHORITY + """
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

CREATE_AND_READ_ROLLBACK_AUTHORIZATION = _AUTHORITY + """
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
  fence_token: $fence_token, boundary_digest: $boundary_digest, state: 'available'})
WHERE unit.unit_id IN completion.unit_ids AND result.rollback_image_id = image.rollback_image_id
  AND result.rollback_image_digest = image.image_digest
MERGE (authorization:CrmDealRepairRollbackAuthorization {run_id: $run_id,
  authorization_transition_id: $authorization_transition_id})
ON CREATE SET authorization.unit_id = $unit_id,
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
  AND authorization.image_digest = image.image_digest AND authorization.state = 'approved'
  AND authorization.consumable = true
RETURN properties(unit) AS unit, properties(fence) AS fence, properties(result) AS result,
  properties(image) AS image, properties(authorization) AS authorization
"""

STORE_ROLLBACK_RECEIPT = _AUTHORITY + """
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

READ_RUN_RECEIPTS = _BASE_AUTHORITY + """
MATCH (completion:CrmDealRepairAllocationCompletion {run_id: $run_id,
  completion_id: $completion_id, boundary_digest: $boundary_digest})
CALL {
  WITH completion
  OPTIONAL MATCH (receipt:CrmDealRepairRollbackReceipt {run_id: completion.run_id})
  RETURN [item IN collect(receipt) WHERE item IS NOT NULL | properties(item)] AS receipts
}
RETURN completion.unit_count AS unit_count, receipts
"""

RELEASE_TERMINAL_FENCE = _AUTHORITY + """
MATCH (fence:CrmDealRepairFence {run_id: $run_id, unit_id: $unit_id, fence_id: $fence_id,
  owner_id: $owner_id, token: $token_digest, state: 'claimed'})
MATCH (image:CrmDealRepairRollbackImage {run_id: $run_id, unit_id: $unit_id,
  image_digest: $image_digest})
MATCH (authorization:CrmDealRepairRollbackAuthorization {run_id: $run_id,
  authorization_transition_id: $authorization_transition_id, unit_id: $unit_id,
  fence_id: $fence_id, state: 'consumed', consumable: false,
  consumed_result_digest: $result_digest, rollback_image_id: image.rollback_image_id})
MATCH (disposition:CrmDealRepairSecondaryDisposition {run_id: $run_id,
  disposition_id: authorization.consumed_disposition_id, result_digest: $result_digest,
  authorization_transition_id: $authorization_transition_id})
WHERE image.state IN ['restored', 'review_required']
SET fence.state = 'released', fence.released_at = datetime(), fence.release_result_digest = $result_digest
RETURN fence.fence_id AS fence_id
"""

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
  OPTIONAL MATCH (unit:CrmDealRepairUnit {run_id: completion.run_id})
  RETURN [item IN collect(unit) WHERE item IS NOT NULL | properties(item)] AS units
}
CALL {
  WITH completion
  OPTIONAL MATCH (fence:CrmDealRepairFence {run_id: completion.run_id})
  RETURN [item IN collect(fence) WHERE item IS NOT NULL | properties(item)] AS fences
}
RETURN completion.unit_count AS unit_count, completion.unit_ids AS unit_ids, units, fences
"""

ACCEPT_AND_RELEASE = _BASE_AUTHORITY + """
OPTIONAL MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: $control_instance_id, blocked: true,
  block_reason: 'crm_deal_identity_repair_quiesce', repair_run_id: $run_id,
  repair_owner_id: $owner_id, repair_token_digest: $token_digest,
  repair_revision: $revision})
OPTIONAL MATCH (prior:CrmDealRepairAcceptance {run_id: $run_id})
WITH run, control, completion, dispatch, prior
WHERE completion.unit_set_digest = $computed_allocation_unit_set_digest
  AND (prior IS NULL OR (prior.request_digest = $request_digest
    AND prior.unit_set_digest = $unit_set_digest AND prior.fence_set_digest = $fence_set_digest
    AND prior.equation_digest = $equation_digest AND prior.control_revision = $revision
    AND prior.receipt_digest = $acceptance_receipt_digest))
  AND (prior IS NOT NULL OR dispatch IS NOT NULL)
CALL {
  WITH completion
  OPTIONAL MATCH (unit:CrmDealRepairUnit {run_id: completion.run_id})
  RETURN collect(unit) AS units
}
CALL {
  WITH completion
  OPTIONAL MATCH (fence:CrmDealRepairFence {run_id: completion.run_id})
  RETURN collect(fence) AS fences
}
CALL {
  WITH completion
  OPTIONAL MATCH (mutation:CrmDealRepairMutationResult {run_id: completion.run_id})
  RETURN count(mutation) AS all_mutations
}
CALL {
  WITH completion
  OPTIONAL MATCH (image:CrmDealRepairRollbackImage {run_id: completion.run_id})
  RETURN count(image) AS all_images
}
CALL {
  WITH completion
  OPTIONAL MATCH (verification:CrmDealRepairVerification {run_id: completion.run_id})
  RETURN count(verification) AS all_verifications
}
CALL {
  WITH completion
  OPTIONAL MATCH (authorization:CrmDealRepairRollbackAuthorization {run_id: completion.run_id})
  RETURN count(authorization) AS all_authorizations
}
CALL {
  WITH completion
  OPTIONAL MATCH (receipt:CrmDealRepairRollbackReceipt {run_id: completion.run_id})
  RETURN count(receipt) AS all_receipts
}
CALL {
  WITH completion, $receipt_bindings AS bindings
  UNWIND CASE WHEN size(bindings) = 0 THEN [NULL] ELSE bindings END AS binding
  OPTIONAL MATCH (receipt:CrmDealRepairRollbackReceipt {run_id: completion.run_id,
    receipt_id: binding.receipt_id, receipt_digest: binding.receipt_digest})
  RETURN count(receipt) AS bound_receipts, count(DISTINCT receipt) AS distinct_bound_receipts
}
CALL {
  WITH completion
  MATCH (mutation:CrmDealRepairMutationResult {run_id: completion.run_id})
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
  OPTIONAL MATCH (receipt:CrmDealRepairRollbackReceipt {run_id: completion.run_id,
    unit_id: mutation.unit_id, image_digest: mutation.rollback_image_digest,
    mutation_id: mutation.mutation_id, generation: mutation.generation,
    sequence: mutation.sequence, attempt: mutation.attempt, fence_id: authorization.fence_id,
    authorization_transition_id: authorization.authorization_transition_id,
    authorization_digest: authorization.authorization_digest, control_revision: $revision,
    allocation_revision: $allocation_revision, completion_id: $completion_id, state: 'available'})
  WHERE receipt.status_digest STARTS WITH 'sha256:' AND size(receipt.status_digest) = 71
  RETURN count(DISTINCT mutation) AS mutations, count(DISTINCT image) AS images,
    count(DISTINCT verification) AS verifications, count(DISTINCT authorization) AS authorizations,
    count(DISTINCT receipt) AS receipts
}
CALL {
  WITH completion
  OPTIONAL MATCH (bad:CrmDealRepairRollbackImage {run_id: completion.run_id})
  WHERE bad.state IN ['restored', 'review_required']
  RETURN count(bad) AS terminal_rollbacks
}
CALL {
  WITH completion
  OPTIONAL MATCH (bad:CrmDealRepairVerification {run_id: completion.run_id})
  WHERE bad.outcome IN ['drifted', 'failed', 'pending']
  RETURN count(bad) AS bad_verifications
}
CALL {
  WITH completion
  OPTIONAL MATCH (bad:CrmDealRepairSecondaryDisposition {run_id: completion.run_id})
  WHERE bad.outcome IN ['pending', 'failed']
  RETURN count(bad) AS bad_secondaries
}
WITH completion, prior, units, fences, all_mutations, all_images, all_verifications,
  all_authorizations, all_receipts, bound_receipts, distinct_bound_receipts, mutations, images,
  verifications, authorizations, receipts,
  terminal_rollbacks, bad_verifications, bad_secondaries
WHERE size(units) = completion.unit_count
  AND size([unit IN units WHERE unit.unit_id IN completion.unit_ids]) = completion.unit_count
  AND size(fences) = completion.unit_count
  AND (prior IS NOT NULL OR all(fence IN fences WHERE fence.state = 'claimed'
    AND fence.unit_id IN completion.unit_ids AND fence.owner_id = $owner_id
    AND fence.token = $token_digest AND fence.boundary_digest = $boundary_digest))
  AND all_mutations = completion.unit_count AND all_images = completion.unit_count
  AND all_verifications = completion.unit_count AND all_authorizations = completion.unit_count
  AND all_receipts = completion.unit_count AND bound_receipts = completion.unit_count
  AND distinct_bound_receipts = completion.unit_count
  AND size($receipt_bindings) = completion.unit_count AND mutations = completion.unit_count
  AND images = completion.unit_count AND verifications = completion.unit_count
  AND authorizations = completion.unit_count AND receipts = completion.unit_count
  AND terminal_rollbacks = 0 AND bad_verifications = 0 AND bad_secondaries = 0
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

READ_ACCEPTANCE = """
MATCH (acceptance:CrmDealRepairAcceptance {run_id: $run_id})
RETURN acceptance.receipt_digest AS receipt_digest, acceptance.fence_set_digest AS fence_set_digest
"""

RELEASE_DISPATCH = _BASE_AUTHORITY + """
MATCH (acceptance:CrmDealRepairAcceptance {run_id: $run_id,
  receipt_digest: $acceptance_receipt_digest, fence_set_digest: $fence_set_digest,
  control_revision: $revision})
OPTIONAL MATCH (prior:CrmDealRepairDispatchRelease {run_id: $run_id})
WITH acceptance, control, prior
WHERE prior IS NULL OR (prior.request_digest = $request_digest
  AND prior.acceptance_receipt_digest = $acceptance_receipt_digest
  AND prior.fence_set_digest = $fence_set_digest AND prior.control_revision = $revision)
CALL {
  WITH prior
  WITH prior WHERE prior IS NULL
  MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
    control_instance_id: $control_instance_id, blocked: true,
    block_reason: 'crm_deal_identity_repair_quiesce', repair_run_id: $run_id,
    repair_owner_id: $owner_id, repair_token_digest: $token_digest,
    repair_revision: $revision})
  SET dispatch.blocked = false, dispatch.block_reason = NULL, dispatch.repair_run_id = NULL,
    dispatch.repair_owner_id = NULL, dispatch.repair_token_digest = NULL,
    dispatch.repair_revision = NULL, dispatch.updated_at = datetime()
  RETURN 1 AS released
  UNION
  WITH prior
  WITH prior WHERE prior IS NOT NULL
  RETURN 1 AS released
}
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
