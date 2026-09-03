"""Fixed parameterized Cypher for CAS-protected CRM repair rollback."""

from __future__ import annotations

LOCK_AND_READ_ROLLBACK_BUNDLE = """
MATCH (unit:CrmDealRepairUnit {run_id: $run_id, unit_id: $unit_id, generation: $generation,
  sequence: $sequence, attempt: $attempt, boundary_digest: $boundary_digest,
  inventory_fingerprint: $unit_fingerprint})
MATCH (fence:CrmDealRepairFence {run_id: $run_id, unit_id: $unit_id, fence_id: $fence_id,
  generation: $generation, sequence: $sequence, attempt: $attempt, owner_id: $owner_id,
  token: $fence_token, boundary_digest: $boundary_digest})
MATCH (result:CrmDealRepairMutationResult {run_id: $run_id, unit_id: $unit_id,
  mutation_id: $mutation_id, generation: $generation, sequence: $sequence, attempt: $attempt,
  owner_id: $owner_id, fence_token: $fence_token, boundary_digest: $boundary_digest,
  unit_fingerprint: $unit_fingerprint, rollback_image_id: $rollback_image_id})
MATCH (image:CrmDealRepairRollbackImage {run_id: $run_id, rollback_image_id: $rollback_image_id,
  unit_id: $unit_id, generation: $generation, sequence: $sequence, attempt: $attempt,
  owner_id: $owner_id, fence_token: $fence_token, boundary_digest: $boundary_digest,
  image_digest: $image_digest})
MATCH (authorization:CrmDealRepairRollbackAuthorization {run_id: $run_id, unit_id: $unit_id,
  authorization_transition_id: $authorization_transition_id, authorization_reference: $authorization_reference,
  authorization_token_digest: $authorization_token_digest, predecessor_transition_id: $predecessor_transition_id,
  authorization_policy: $authorization_policy, generation: $generation, sequence: $sequence,
  attempt: $attempt, boundary_digest: $boundary_digest, fence_id: $fence_id, owner_id: $owner_id,
  fence_token: $fence_token, mutation_id: $mutation_id, rollback_image_id: $rollback_image_id,
  image_digest: $image_digest})
WHERE result.rollback_image_digest = image.image_digest AND result.fence_token = fence.token
  AND result.evidence_digest = image.evidence_digest AND result.payload_digest = image.payload_digest
  AND authorization.predecessor_transition_id = result.mutation_id + ':applied:' + image.rollback_image_id
SET unit.unit_id = unit.unit_id, fence.fence_id = fence.fence_id,
  result.mutation_id = result.mutation_id, image.rollback_image_id = image.rollback_image_id,
  authorization.authorization_transition_id = authorization.authorization_transition_id
WITH unit, fence, result, image, authorization
WHERE fence.state = 'claimed' AND image.state = 'available'
  AND authorization.state = 'approved' AND authorization.consumable = true
SET unit.rollback_lock_id = coalesce(unit.rollback_lock_id, $rollback_request_digest),
  authorization.authorization_lock_id = coalesce(authorization.authorization_lock_id, $rollback_request_digest)
WITH unit, fence, result, image, authorization
WHERE unit.rollback_lock_id = $rollback_request_digest
  AND authorization.authorization_lock_id = $rollback_request_digest
OPTIONAL MATCH (checkpoint:CrmDealRepairCheckpoint {run_id: $run_id, checkpoint_id: result.checkpoint_id})
OPTIONAL MATCH (outbox:CrmDealRepairOutbox {run_id: $run_id, event_id: result.outbox_event_id})
OPTIONAL MATCH (disposition:CrmDealRepairSecondaryDisposition {run_id: $run_id, unit_id: $unit_id})
RETURN properties(unit) AS unit, properties(fence) AS fence, properties(result) AS result,
  properties(image) AS image, properties(authorization) AS authorization,
  collect(properties(checkpoint)) AS checkpoints, collect(properties(outbox)) AS outboxes,
  collect(properties(disposition)) AS dispositions
"""

LOCK_AND_ASSERT_ROLLBACK_DOMAIN_GUARD = """
MATCH (run:CrmDealRepairRun {run_id: $run_id, boundary_digest: $boundary_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  status: 'qualified', execution_allowed: false})
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
    rollback_authority_reference: boundary.rollback_authority_reference,
    rollback_authority_policy: boundary.rollback_authority_policy,
    execution_allowed: boundary.execution_allowed
  } END) AS boundaries
MATCH (result:CrmDealRepairMutationResult {run_id: $run_id, unit_id: $unit_id,
  mutation_id: $mutation_id, new_source_record_pk: $replacement_source_record_pk})
MATCH (:BitrixDispatchControl {source_key: 'bitrix_chat', control_instance_id: $control_instance_id,
  blocked: true})
MATCH (source_instance:BitrixSourceInstance {source_key: 'bitrix_chat',
  source_instance_id: $source_instance_id, status: 'active'})-[:INSTANCE_OF]->
  (:SourceSystem {source_key: 'bitrix_chat', is_active: true})
MATCH (source_instance)-[:OWNS_BITRIX_CONTROL]->
  (:BitrixExecutionSourceBinding {source_key: 'bitrix_chat', source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id})
MATCH (:BitrixSourceInstance {source_key: 'bitrix_chat', source_instance_id: $control_instance_id,
  status: 'active'})-[:INSTANCE_OF]->(:SourceSystem {source_key: 'bitrix_chat', is_active: true})
MATCH (old:SourceRecord {source_record_pk: $original_source_record_pk,
  source_record_id: $source_record_id, source_instance_id: $source_instance_id})
MATCH (replacement:SourceRecord {source_record_pk: $replacement_source_record_pk,
  repair_mutation_id: $mutation_id, is_latest: true})
MERGE (lock:SourceRecordIdentityLock {source_system: 'bitrix_chat',
  source_instance_id: $source_instance_id, source_record_id: $source_record_id})
SET lock.locked_at = datetime(), run.run_id = run.run_id,
  source_instance.source_instance_id = source_instance.source_instance_id,
  old.source_record_pk = old.source_record_pk,
  replacement.source_record_pk = replacement.source_record_pk
RETURN old.source_record_pk AS original_source_record_pk, properties(run) AS run,
  qualification_link_count, boundaries
"""


READ_ROLLBACK_CURRENT_STATE = """
MATCH (source:SourceRecord {source_record_pk: $source_record_pk})
WITH source, [source.source_record_pk] + $retired_source_record_pks AS affected_pks
CALL {
  WITH source
  OPTIONAL MATCH (node)
  WHERE (node:SourceRecord OR node:MatchDecision OR node:ReviewCase OR node:Identifier)
    AND (node.repair_mutation_id = $mutation_id OR node.source_record_pk IN $retired_source_record_pks
      OR node.source_record_pk = source.source_record_pk)
  WITH node ORDER BY coalesce(node.source_record_pk, node.match_decision_id, node.review_case_id,
    node.identifier_type, '')
  SET node.source_record_pk = node.source_record_pk
  RETURN collect(DISTINCT {
    object_kind: CASE WHEN node:SourceRecord THEN 'SourceRecord' WHEN node:MatchDecision THEN 'MatchDecision'
      WHEN node:ReviewCase THEN 'ReviewCase' ELSE 'Identifier' END,
    identity: CASE WHEN node:SourceRecord THEN {source_record_pk: node.source_record_pk}
      WHEN node:MatchDecision THEN {match_decision_id: node.match_decision_id}
      WHEN node:ReviewCase THEN {review_case_id: node.review_case_id} ELSE {identifier_type: node.identifier_type,
        identifier_scope: node.identifier_scope, normalized_value: node.normalized_value} END,
    properties: properties(node)
  }) AS nodes
}
CALL {
  WITH affected_pks
  MATCH (left)-[relationship]->(right)
  WHERE (left:SourceRecord AND left.source_record_pk IN affected_pks)
     OR (right:SourceRecord AND right.source_record_pk IN affected_pks)
     OR relationship.source_record_pk IN affected_pks
     OR relationship.repair_mutation_id = $mutation_id
  WITH left, relationship, right ORDER BY type(relationship),
    coalesce(left.source_record_pk, left.person_id, left.identifier_key, left.address_id, ''),
    coalesce(right.source_record_pk, right.person_id, right.identifier_key, right.address_id, ''),
    elementId(relationship)
  SET left.source_record_pk = left.source_record_pk,
    right.source_record_pk = right.source_record_pk,
    relationship.source_record_pk = relationship.source_record_pk
  RETURN collect({
    direction: 'outgoing', left_labels: labels(left), left_properties: properties(left),
    relationship_type: type(relationship), relationship_properties: properties(relationship),
    right_labels: labels(right), right_properties: properties(right)
  }) AS relationships
}
RETURN nodes, relationships
"""

RESTORE_ORIGINAL_SOURCE = """
UNWIND $sources AS item
MATCH (source:SourceRecord {source_record_pk: item.source_record_pk})
SET source = item.properties
RETURN count(source) AS restored_count
"""

RESTORE_PREEXISTING_RELATIONSHIPS = """
UNWIND $relationships AS item
MATCH (left)-[relationship]->(right)
WHERE type(relationship) = item.relationship_type
  AND CASE item.left_mode
    WHEN 'source_record_pk' THEN left:SourceRecord AND left.source_record_pk = item.left_value
    WHEN 'person_id' THEN left:Person AND left.person_id = item.left_value
    WHEN 'match_decision_id' THEN left:MatchDecision AND left.match_decision_id = item.left_value
    WHEN 'review_case_id' THEN left:ReviewCase AND left.review_case_id = item.left_value
    WHEN 'identifier_key' THEN left:Identifier AND left.identifier_key = item.left_value
    WHEN 'identifier_composite' THEN left:Identifier AND left.identifier_type = item.left_value
      AND left.identifier_scope = item.left_value_2 AND left.normalized_value = item.left_value_3
    WHEN 'address_id' THEN left:Address AND left.address_id = item.left_value
    WHEN 'fact_id' THEN left:Fact AND left.fact_id = item.left_value
    WHEN 'entity_key' THEN left:Entity AND left.entity_key = item.left_value
    WHEN 'source_system' THEN left:SourceSystem AND left.source_key = item.left_value
    ELSE false END
  AND CASE item.right_mode
    WHEN 'source_record_pk' THEN right:SourceRecord AND right.source_record_pk = item.right_value
    WHEN 'person_id' THEN right:Person AND right.person_id = item.right_value
    WHEN 'match_decision_id' THEN right:MatchDecision AND right.match_decision_id = item.right_value
    WHEN 'review_case_id' THEN right:ReviewCase AND right.review_case_id = item.right_value
    WHEN 'identifier_key' THEN right:Identifier AND right.identifier_key = item.right_value
    WHEN 'identifier_composite' THEN right:Identifier AND right.identifier_type = item.right_value
      AND right.identifier_scope = item.right_value_2 AND right.normalized_value = item.right_value_3
    WHEN 'address_id' THEN right:Address AND right.address_id = item.right_value
    WHEN 'fact_id' THEN right:Fact AND right.fact_id = item.right_value
    WHEN 'entity_key' THEN right:Entity AND right.entity_key = item.right_value
    WHEN 'source_system' THEN right:SourceSystem AND right.source_key = item.right_value
    ELSE false END
  AND ((item.source_record_pk IS NULL AND relationship.source_record_pk IS NULL)
    OR relationship.source_record_pk = item.source_record_pk)
WITH item, relationship ORDER BY item.restore_group, elementId(relationship)
WITH item, collect(relationship) AS relationships
WHERE size(relationships) = item.group_size
UNWIND range(0, item.group_size - 1) AS ordinal
WITH ordinal, item.assignments[ordinal] AS assignment, relationships[ordinal] AS relationship
WHERE relationship IS NOT NULL AND assignment.restore_ordinal = ordinal
SET relationship = assignment.properties
RETURN count(assignment) AS restored_count
"""

MAKE_MUTATION_EVIDENCE_HISTORICAL = """
MATCH (source:SourceRecord {source_record_pk: $replacement_source_record_pk,
  repair_mutation_id: $mutation_id})
SET source.lifecycle_status = 'rolled_back', source.is_latest = false, source.link_status = 'rolled_back',
  source.repair_rollback_id = $rollback_image_id
WITH source
OPTIONAL MATCH (node)
WHERE (node:MatchDecision OR node:ReviewCase) AND node.repair_mutation_id = $mutation_id
SET node.lifecycle_status = 'historical', node.repair_rollback_id = $rollback_image_id
WITH DISTINCT source
OPTIONAL MATCH ()-[relationship]->()
WHERE relationship.repair_mutation_id = $mutation_id
  AND type(relationship) IN ['LINKED_TO', 'IDENTIFIED_BY', 'LIVES_AT', 'HAS_FACT', 'DESCRIBES_ADDRESS']
SET relationship.is_active = false, relationship.authoritative = false,
  relationship.repair_rollback_id = $rollback_image_id
RETURN DISTINCT source.source_record_pk AS source_record_pk
"""

PERSIST_ROLLBACK_TERMINAL = """
MATCH (unit:CrmDealRepairUnit {run_id: $run_id, unit_id: $unit_id, generation: $generation,
  sequence: $sequence, attempt: $attempt, boundary_digest: $boundary_digest,
  rollback_lock_id: $rollback_request_digest})
MATCH (image:CrmDealRepairRollbackImage {run_id: $run_id, rollback_image_id: $rollback_image_id,
  image_digest: $image_digest, state: 'available'})
MATCH (result:CrmDealRepairMutationResult {run_id: $run_id, unit_id: $unit_id,
  mutation_id: $mutation_id, generation: $generation, sequence: $sequence, attempt: $attempt,
  owner_id: $owner_id, fence_token: $fence_token, boundary_digest: $boundary_digest,
  unit_fingerprint: $unit_fingerprint, rollback_image_id: $rollback_image_id})
MATCH (authorization:CrmDealRepairRollbackAuthorization {run_id: $run_id, unit_id: $unit_id,
  authorization_transition_id: $authorization_transition_id, authorization_reference: $authorization_reference,
  authorization_token_digest: $authorization_token_digest, predecessor_transition_id: $predecessor_transition_id,
  authorization_policy: $authorization_policy, generation: $generation, sequence: $sequence,
  attempt: $attempt, boundary_digest: $boundary_digest, fence_id: $fence_id, owner_id: $owner_id,
  fence_token: $fence_token, mutation_id: $mutation_id, rollback_image_id: $rollback_image_id,
  image_digest: $image_digest, state: 'approved', consumable: true,
  authorization_lock_id: $rollback_request_digest})
WHERE unit.state IN ['applied', 'review_required']
  AND result.rollback_image_digest = image.image_digest
  AND authorization.predecessor_transition_id = result.mutation_id + ':applied:' + image.rollback_image_id
CREATE (disposition:CrmDealRepairSecondaryDisposition {run_id: $run_id, unit_id: $unit_id,
  disposition_id: $disposition_id, generation: $generation, sequence: $sequence, attempt: $attempt,
  owner_id: $owner_id, control_token: $fence_token, boundary_digest: $boundary_digest,
  subject_fingerprint: $image_digest, evidence_digest: $evidence_digest,
  payload_digest: $payload_digest, outcome: $outcome, rollback_request_digest: $rollback_request_digest,
  authorization_reference: $authorization_reference, authorization_token_digest: $authorization_token_digest,
  authorization_transition_id: $authorization_transition_id,
  predecessor_transition_id: $predecessor_transition_id, authorization_policy: $authorization_policy,
  rollback_image_id: $rollback_image_id, result_digest: $result_digest,
  drift_total_mismatch_count: $drift_total_mismatch_count, drift_summaries_json: $drift_summaries_json,
  drift_complete_digest: $drift_complete_digest, rollback_status_digest: $status_digest,
  created_at: datetime()})
SET image.state = $image_state, image.rollback_disposition_id = $disposition_id,
  image.rollback_result_digest = $result_digest, image.rollback_status_digest = $status_digest,
  authorization.state = 'consumed', authorization.consumable = false,
  authorization.consumed_disposition_id = $disposition_id,
  authorization.consumed_request_digest = $rollback_request_digest,
  authorization.consumed_result_digest = $result_digest,
  authorization.consumed_at = datetime()
SET unit.state = $unit_state, unit.rollback_disposition_id = $disposition_id
RETURN properties(disposition) AS disposition, properties(authorization) AS authorization
"""

READ_ROLLBACK_TERMINAL = """
MATCH (unit:CrmDealRepairUnit {run_id: $run_id, unit_id: $unit_id, generation: $generation,
  sequence: $sequence, attempt: $attempt, boundary_digest: $boundary_digest,
  inventory_fingerprint: $unit_fingerprint})
MATCH (fence:CrmDealRepairFence {run_id: $run_id, unit_id: $unit_id, fence_id: $fence_id,
  generation: $generation, sequence: $sequence, attempt: $attempt, owner_id: $owner_id,
  token: $fence_token, boundary_digest: $boundary_digest})
MATCH (result:CrmDealRepairMutationResult {run_id: $run_id, unit_id: $unit_id,
  mutation_id: $mutation_id, generation: $generation, sequence: $sequence, attempt: $attempt,
  owner_id: $owner_id, fence_token: $fence_token, boundary_digest: $boundary_digest,
  unit_fingerprint: $unit_fingerprint, rollback_image_id: $rollback_image_id})
MATCH (image:CrmDealRepairRollbackImage {run_id: $run_id, unit_id: $unit_id,
  rollback_image_id: $rollback_image_id, generation: $generation, sequence: $sequence,
  attempt: $attempt, owner_id: $owner_id, fence_token: $fence_token,
  boundary_digest: $boundary_digest, image_digest: $image_digest})
MATCH (authorization:CrmDealRepairRollbackAuthorization {run_id: $run_id, unit_id: $unit_id,
  authorization_transition_id: $authorization_transition_id, authorization_reference: $authorization_reference,
  authorization_token_digest: $authorization_token_digest, predecessor_transition_id: $predecessor_transition_id,
  authorization_policy: $authorization_policy, generation: $generation, sequence: $sequence,
  attempt: $attempt, boundary_digest: $boundary_digest, fence_id: $fence_id, owner_id: $owner_id,
  fence_token: $fence_token, mutation_id: $mutation_id, rollback_image_id: $rollback_image_id,
  image_digest: $image_digest})
WHERE result.rollback_image_digest = image.image_digest AND result.evidence_digest = image.evidence_digest
  AND result.payload_digest = image.payload_digest
  AND authorization.predecessor_transition_id = result.mutation_id + ':applied:' + image.rollback_image_id
OPTIONAL MATCH (checkpoint:CrmDealRepairCheckpoint {run_id: $run_id, checkpoint_id: result.checkpoint_id})
OPTIONAL MATCH (outbox:CrmDealRepairOutbox {run_id: $run_id, event_id: result.outbox_event_id})
OPTIONAL MATCH (disposition:CrmDealRepairSecondaryDisposition {run_id: $run_id,
  disposition_id: image.rollback_disposition_id})
RETURN properties(unit) AS unit, properties(fence) AS fence, properties(result) AS result,
  properties(image) AS image, properties(authorization) AS authorization,
  collect(properties(checkpoint)) AS checkpoints, collect(properties(outbox)) AS outboxes,
  collect(properties(disposition)) AS dispositions
"""

READ_RESTORED_ROLLBACK_STATE = """
MATCH (source:SourceRecord {source_record_pk: $source_record_pk})
OPTIONAL MATCH (left)-[relationship]->(right)
WHERE relationship.source_record_pk IN $retired_source_record_pks
RETURN properties(source) AS source, collect({relationship_type: type(relationship),
  left_value: coalesce(left.source_record_pk, left.person_id, left.match_decision_id, left.review_case_id,
    left.identifier_key, left.address_id, left.entity_key),
  right_value: coalesce(right.source_record_pk, right.person_id, right.match_decision_id, right.review_case_id,
    right.identifier_key, right.address_id, right.entity_key), properties: properties(relationship)}) AS relationships
"""

READ_ROLLBACK_POSTCONDITION = """
MATCH (root:SourceRecord {source_record_pk: $source_record_pk})
WITH root, [root.source_record_pk] + $retired_source_record_pks AS affected_pks
CALL {
  WITH root
  OPTIONAL MATCH (descendant:SourceRecord)-[:CHILD_OF*1..2]->(root)
  WHERE descendant.source_record_pk IN $retired_source_record_pks
  RETURN collect({object_kind: 'SourceRecord', identity: {source_record_pk: root.source_record_pk},
    properties: properties(root)}) + collect(CASE WHEN descendant IS NULL THEN null ELSE
    {object_kind: 'SourceRecord', identity: {source_record_pk: descendant.source_record_pk},
    properties: properties(descendant)} END) AS sources
}
CALL {
  WITH affected_pks
  MATCH (left)-[relationship]->(right)
  WHERE ((left:SourceRecord AND left.source_record_pk IN affected_pks)
     OR (right:SourceRecord AND right.source_record_pk IN affected_pks)
     OR relationship.source_record_pk IN affected_pks)
    AND coalesce(relationship.repair_mutation_id, '') <> $mutation_id
  WITH left, relationship, right ORDER BY type(relationship),
    coalesce(left.source_record_pk, left.person_id, left.identifier_key, left.address_id, ''),
    coalesce(right.source_record_pk, right.person_id, right.identifier_key, right.address_id, ''),
    elementId(relationship)
  RETURN collect({
    direction: 'outgoing', left_labels: labels(left), left_properties: properties(left),
    relationship_type: type(relationship), relationship_properties: properties(relationship),
    right_labels: labels(right), right_properties: properties(right)
  }) AS relationships
}
CALL {
  WITH root
  OPTIONAL MATCH (replacement:SourceRecord {source_record_pk: $replacement_source_record_pk,
    repair_mutation_id: $mutation_id})
  RETURN properties(replacement) AS replacement
}
CALL {
  WITH root
  OPTIONAL MATCH (node)
  WHERE node.repair_mutation_id = $mutation_id
    AND (node:SourceRecord OR node:MatchDecision OR node:ReviewCase OR node:Identifier)
  RETURN collect(CASE WHEN node IS NULL THEN null ELSE {
    object_kind: CASE WHEN node:SourceRecord THEN 'SourceRecord' WHEN node:MatchDecision THEN 'MatchDecision'
      WHEN node:ReviewCase THEN 'ReviewCase' ELSE 'Identifier' END,
    properties: properties(node)
  } END) AS mutation_nodes
}
CALL {
  WITH root
  OPTIONAL MATCH (left)-[relationship]->(right)
  WHERE relationship.repair_mutation_id = $mutation_id
  RETURN collect(CASE WHEN relationship IS NULL THEN null ELSE {
    relationship_type: type(relationship), relationship_properties: properties(relationship),
    left_labels: labels(left), left_properties: properties(left),
    right_labels: labels(right), right_properties: properties(right)
  } END) AS mutation_relationships
}
RETURN sources, relationships, replacement, mutation_nodes, mutation_relationships
"""
