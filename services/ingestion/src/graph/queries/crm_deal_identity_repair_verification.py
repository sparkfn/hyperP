"""Parameterized verification-only Cypher for an immutable CRM repair bundle."""

from __future__ import annotations

LOCK_AND_READ_VERIFICATION_BUNDLE = """
MATCH (run:CrmDealRepairRun {run_id: $run_id, boundary_digest: $boundary_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id, status: 'qualified',
  execution_allowed: false})
MATCH (unit:CrmDealRepairUnit {run_id: $run_id, unit_id: $unit_id, generation: $generation,
  sequence: $sequence, attempt: $attempt, boundary_digest: $boundary_digest,
  inventory_fingerprint: $unit_fingerprint, inventory_binding_digest: $inventory_binding_digest})
MATCH (fence:CrmDealRepairFence {run_id: $run_id, unit_id: $unit_id, fence_id: $fence_id,
  generation: $generation, sequence: $sequence, attempt: $attempt, owner_id: $owner_id,
  token: $fence_token, boundary_digest: $boundary_digest, state: 'claimed'})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: $control_instance_id, blocked: true})
MATCH (result:CrmDealRepairMutationResult {run_id: $run_id, unit_id: $unit_id,
  mutation_id: $mutation_id, generation: $generation, sequence: $sequence, attempt: $attempt,
  boundary_digest: $boundary_digest})
MATCH (run)-[:HAS_REPAIR_MUTATION]->(result)
MATCH (image:CrmDealRepairRollbackImage {run_id: $run_id, rollback_image_id: $rollback_image_id,
  generation: $generation, sequence: $sequence, attempt: $attempt, boundary_digest: $boundary_digest})
MATCH (run)-[:HAS_REPAIR_ROLLBACK_IMAGE]->(image)
MATCH (checkpoint:CrmDealRepairCheckpoint {run_id: $run_id, checkpoint_id: $checkpoint_id,
  generation: $generation, sequence: $sequence, attempt: $attempt, boundary_digest: $boundary_digest})
MATCH (unit)-[:HAS_REPAIR_CHECKPOINT]->(checkpoint)
MATCH (outbox:CrmDealRepairOutbox {run_id: $run_id, unit_id: $unit_id, event_id: $outbox_event_id,
  generation: $generation, sequence: $sequence, attempt: $attempt, boundary_digest: $boundary_digest})
MATCH (unit)-[:HAS_REPAIR_OUTBOX]->(outbox)
OPTIONAL MATCH (new:SourceRecord {repair_mutation_id: $mutation_id})
WITH run, unit, fence, result, image, checkpoint, outbox, dispatch, collect(new) AS new_sources
RETURN properties(result) AS result, properties(image) AS image, properties(checkpoint) AS checkpoint,
  properties(outbox) AS outbox, [item IN new_sources | item.source_record_pk] AS new_source_pks,
  size(new_sources) AS new_source_count, count(dispatch) AS blocked_dispatch_count
"""

CLAIM_VERIFICATION_OUTBOX = """
MATCH (outbox:CrmDealRepairOutbox {run_id: $run_id, unit_id: $unit_id, event_id: $event_id,
  generation: $generation, sequence: $sequence, attempt: $attempt, boundary_digest: $boundary_digest,
  payload_digest: $outbox_payload_digest, evidence_digest: $outbox_evidence_digest, state: 'pending'})
WHERE outbox.verification_claim_owner_id IS NULL
   OR (outbox.verification_claim_owner_id = $owner_id
       AND outbox.verification_claim_token = $claim_token
       AND outbox.verification_claim_digest = $claim_digest)
SET outbox.verification_claim_owner_id = $owner_id,
    outbox.verification_claim_token = $claim_token,
    outbox.verification_claim_digest = $claim_digest,
    outbox.verification_claimed_at = datetime()
RETURN properties(outbox) AS outbox
"""

READ_EXACT_OUTBOX_STATE = """
MATCH (outbox:CrmDealRepairOutbox {run_id: $run_id, unit_id: $unit_id, event_id: $event_id,
  generation: $generation, sequence: $sequence, attempt: $attempt, boundary_digest: $boundary_digest,
  owner_id: $owner_id, delivery_token: $fence_token, mutation_id: $mutation_id,
  payload_digest: $outbox_payload_digest, evidence_digest: $outbox_evidence_digest})
RETURN outbox.state AS state
"""

READ_ACKNOWLEDGED_VERIFICATION = """
MATCH (outbox:CrmDealRepairOutbox {run_id: $run_id, unit_id: $unit_id, event_id: $event_id,
  state: 'acknowledged', verification_request_digest: $request_digest})
MATCH (verification:CrmDealRepairVerification {run_id: $run_id, unit_id: $unit_id,
  verification_id: $verification_id, request_digest: $request_digest})
WHERE verification.verification_digest = outbox.verification_result_digest
OPTIONAL MATCH (disposition:CrmDealRepairSecondaryDisposition {run_id: $run_id, unit_id: $unit_id,
  verification_id: $verification_id})
RETURN properties(outbox) AS outbox, properties(verification) AS verification,
  collect(properties(disposition)) AS dispositions
"""

READ_AFFECTED_PERSON_IDS = """
WITH $source_record_pks AS source_record_pks
CALL (source_record_pks) {
  UNWIND source_record_pks AS source_record_pk
  MATCH (:SourceRecord {source_record_pk: source_record_pk})-[link:LINKED_TO]->(person:Person)
  WHERE person.status = 'active'
  RETURN person.person_id AS person_id
  UNION
  UNWIND source_record_pks AS source_record_pk
  MATCH (person:Person)-[projection:IDENTIFIED_BY|LIVES_AT|HAS_FACT]->()
  WHERE projection.source_record_pk = source_record_pk AND person.status = 'active'
  RETURN person.person_id AS person_id
}
RETURN DISTINCT person_id ORDER BY person_id
"""

READ_PRIMARY_POSTCONDITIONS = """
MATCH (new:SourceRecord {source_record_pk: $new_source_record_pk, repair_mutation_id: $mutation_id})
CALL (new) {
  WITH new
  OPTIONAL MATCH (new)-[link:LINKED_TO]->(:Person)
  WITH link
  WHERE link IS NOT NULL
  RETURN count(CASE WHEN coalesce(link.is_active, true) = true
      AND coalesce(link.authoritative, true) = true THEN link END) AS active_links,
    count(CASE WHEN coalesce(link.is_active, true) = true THEN link END) AS active_any_links,
    count(CASE WHEN coalesce(link.is_active, true) = false
      AND coalesce(link.provisional, false) = true THEN link END) AS provisional_links,
    count(link) AS all_links
}
CALL (new) {
  WITH new
  OPTIONAL MATCH (:Person)-[projection:IDENTIFIED_BY|LIVES_AT|HAS_FACT]->()
  WHERE projection.source_record_pk = new.source_record_pk
  AND coalesce(projection.is_active, true) = true
  RETURN count(projection) AS active_new_evidence
}
CALL (new) {
  WITH new
  OPTIONAL MATCH (review:ReviewCase {repair_mutation_id: $mutation_id})
    -[:FOR_DECISION {repair_mutation_id: $mutation_id}]->
    (decision:MatchDecision {repair_mutation_id: $mutation_id})
  OPTIONAL MATCH (decision)-[:ABOUT_LEFT {entity_type: 'source_record', repair_mutation_id: $mutation_id}]
    ->(new)
  WITH review, decision
  WHERE review IS NOT NULL AND decision IS NOT NULL
  RETURN count(DISTINCT review) AS repair_review_count,
    count(DISTINCT decision) AS repair_decision_count
}
CALL {
  WITH $retired_source_record_pks AS retired_source_record_pks
  MATCH (left)-[relationship:LINKED_TO|IDENTIFIED_BY|LIVES_AT|HAS_FACT|DESCRIBES_ADDRESS]->()
  WHERE (type(relationship) IN ['LINKED_TO', 'DESCRIBES_ADDRESS']
         AND left:SourceRecord AND left.source_record_pk IN retired_source_record_pks)
     OR (type(relationship) IN ['IDENTIFIED_BY', 'LIVES_AT', 'HAS_FACT']
         AND relationship.source_record_pk IN retired_source_record_pks)
  RETURN count(relationship) AS retired_relationship_count,
    count(CASE WHEN coalesce(relationship.is_active, true) = true THEN relationship END)
      AS active_retired_relationship_count
}
CALL {
  WITH $closure_source_record_pks AS closure_source_record_pks
  MATCH (:Person)-[projection:IDENTIFIED_BY]->(identifier:Identifier)
  WHERE projection.source_record_pk IN closure_source_record_pks
    AND coalesce(projection.is_active, true) = true
    AND (identifier.identifier_type IN ['phone', 'email']
      OR toLower(coalesce(identifier.normalized_value, '')) CONTAINS '@g.us')
  RETURN count(projection) AS forbidden_projection_count
}
RETURN new.link_status AS link_status, active_links, active_any_links, provisional_links, all_links,
  active_new_evidence, repair_review_count, repair_decision_count,
  retired_relationship_count,
  active_retired_relationship_count + $retirement_snapshot_failure_count
    AS retirement_stamp_failure_count,
  forbidden_projection_count
"""

READ_SECONDARY_CONTEXT = """
WITH $source_record_pks AS source_record_pks
CALL (source_record_pks) {
  UNWIND source_record_pks AS source_record_pk
  MATCH (child:SourceRecord)-[:CHILD_OF*1..2]->(:SourceRecord {source_record_pk: source_record_pk})
  OPTIONAL MATCH (child)-[link:LINKED_TO]->(owner:Person)
  RETURN DISTINCT 'descendant' AS kind, child.source_record_pk AS stable_id, {
    record_type: child.record_type,
    source_record_pk: child.source_record_pk,
    source_record_id: child.source_record_id,
    lifecycle_status: child.lifecycle_status,
    relationship_type: type(link),
    relationship_is_active: coalesce(link.is_active, true),
    retired_by_repair_mutation_id: link.retired_by_repair_mutation_id,
    owner_person_id: owner.person_id
  } AS evidence
  UNION
  UNWIND source_record_pks AS source_record_pk
  MATCH (:SourceRecord {source_record_pk: source_record_pk})-[:LINKED_TO]->(owner:Person)
  MATCH (owner)-[lock:NO_MATCH_LOCK]-(other:Person)
  RETURN DISTINCT 'no_match_lock' AS kind, coalesce(lock.lock_id, elementId(lock)) AS stable_id, {
    evidence_type: 'no_match_lock',
    owner_person_id: owner.person_id,
    no_match_lock_id: lock.lock_id,
    lock_other_person_id: other.person_id
  } AS evidence
  UNION
  UNWIND source_record_pks AS source_record_pk
  MATCH (:SourceRecord {source_record_pk: source_record_pk})-[:LINKED_TO]->(owner:Person)
  MATCH (owner)-[outgoing_rel:MERGED_INTO]->(survivor:Person)
  RETURN DISTINCT 'merge_lineage' AS kind, outgoing_rel.merge_event_id AS stable_id, {
    evidence_type: 'merge_lineage',
    direction: 'outgoing',
    owner_person_id: owner.person_id,
    merge_event_id: outgoing_rel.merge_event_id,
    absorbed_person_id: owner.person_id,
    survivor_person_id: survivor.person_id
  } AS evidence
  UNION
  UNWIND source_record_pks AS source_record_pk
  MATCH (:SourceRecord {source_record_pk: source_record_pk})-[:LINKED_TO]->(owner:Person)
  MATCH (absorbed:Person)-[incoming_rel:MERGED_INTO]->(owner)
  RETURN DISTINCT 'merge_lineage' AS kind, incoming_rel.merge_event_id AS stable_id, {
    evidence_type: 'merge_lineage',
    direction: 'incoming',
    owner_person_id: owner.person_id,
    merge_event_id: incoming_rel.merge_event_id,
    absorbed_person_id: absorbed.person_id,
    survivor_person_id: owner.person_id
  } AS evidence
}
RETURN kind, stable_id, evidence ORDER BY kind, stable_id
"""

READ_PAIR_AUDIT_CASES = """
UNWIND $review_case_ids AS review_case_id
MATCH (review:ReviewCase {review_case_id: review_case_id})-[:FOR_DECISION]->
  (decision:MatchDecision {engine_type: 'pair_audit'})
MATCH (decision)-[:ABOUT_LEFT {entity_type: 'person'}]->(left:Person)
MATCH (decision)-[:ABOUT_RIGHT {entity_type: 'person'}]->(right:Person)
RETURN review.review_case_id AS review_case_id, review.queue_state AS queue_state,
  review.resolution AS resolution,
  left.person_id AS left_person_id, right.person_id AS right_person_id,
  decision.confidence AS confidence, decision.reasons AS reasons,
  decision.feature_snapshot AS feature_snapshot, decision.engine_version AS engine_version,
  decision.policy_version AS policy_version
ORDER BY review.review_case_id
"""

READ_PAIR_BRIDGE = """
MATCH (left:Person {person_id: $left_person_id})-[a:IDENTIFIED_BY]->(identifier:Identifier)<-[b:IDENTIFIED_BY]-(right:Person {person_id: $right_person_id})
WHERE coalesce(a.is_active, true) = true AND coalesce(b.is_active, true) = true
RETURN count(identifier) AS bridge_count
"""

CANCEL_STALE_PAIR_AUDIT_CASE = """
MATCH (review:ReviewCase {review_case_id: $review_case_id, queue_state: 'open'})-[:FOR_DECISION]->
  (decision:MatchDecision {engine_type: 'pair_audit'})
SET review.queue_state = 'resolved', review.resolution = 'cancelled_stale_repair_bridge',
  review.resolved_at = datetime(), review.updated_at = datetime(),
  decision.updated_at = datetime()
RETURN review.review_case_id AS review_case_id
"""

PERSIST_VERIFICATION = """
MATCH (outbox:CrmDealRepairOutbox {run_id: $run_id, unit_id: $unit_id, event_id: $event_id,
  state: 'pending', verification_claim_owner_id: $owner_id, verification_claim_token: $claim_token,
  verification_claim_digest: $claim_digest})
MERGE (verification:CrmDealRepairVerification {run_id: $run_id, verification_id: $verification_id})
ON CREATE SET verification.unit_id = $unit_id, verification.generation = $generation,
  verification.sequence = $sequence, verification.attempt = $attempt, verification.owner_id = $owner_id,
  verification.fence_token = $fence_token, verification.boundary_digest = $boundary_digest,
  verification.subject_fingerprint = $subject_fingerprint, verification.verification_digest = $verification_digest,
  verification.evidence_digest = $evidence_digest, verification.payload_digest = $payload_digest,
  verification.request_digest = $request_digest,
  verification.expected_disposition_count = $expected_disposition_count,
  verification.outcome = 'verified', verification.created_at = datetime()
WITH outbox, verification
WHERE verification.unit_id = $unit_id AND verification.generation = $generation
  AND verification.sequence = $sequence AND verification.attempt = $attempt
  AND verification.owner_id = $owner_id AND verification.fence_token = $fence_token
  AND verification.boundary_digest = $boundary_digest
  AND verification.subject_fingerprint = $subject_fingerprint
  AND verification.verification_digest = $verification_digest
  AND verification.evidence_digest = $evidence_digest AND verification.payload_digest = $payload_digest
  AND verification.request_digest = $request_digest
  AND verification.expected_disposition_count = $expected_disposition_count
  AND verification.outcome = 'verified'
UNWIND $dispositions AS item
MERGE (disposition:CrmDealRepairSecondaryDisposition {run_id: $run_id, disposition_id: item.disposition_id})
ON CREATE SET disposition.unit_id = $unit_id, disposition.generation = $generation,
  disposition.sequence = $sequence, disposition.attempt = $attempt, disposition.owner_id = $owner_id,
  disposition.control_token = $claim_token, disposition.boundary_digest = $boundary_digest,
  disposition.subject_fingerprint = item.subject_fingerprint, disposition.evidence_digest = item.evidence_digest,
  disposition.payload_digest = item.payload_digest, disposition.outcome = item.outcome,
  disposition.verification_id = $verification_id, disposition.action = item.action,
  disposition.subject_kind = item.subject_kind, disposition.subject_stable_id = item.subject_stable_id,
  disposition.created_at = datetime()
WITH outbox, verification, disposition, item
WHERE disposition.unit_id = $unit_id AND disposition.generation = $generation
  AND disposition.sequence = $sequence AND disposition.attempt = $attempt
  AND disposition.owner_id = $owner_id AND disposition.control_token = $claim_token
  AND disposition.boundary_digest = $boundary_digest
  AND disposition.subject_fingerprint = item.subject_fingerprint
  AND disposition.evidence_digest = item.evidence_digest
  AND disposition.payload_digest = item.payload_digest
  AND disposition.outcome = item.outcome AND disposition.verification_id = $verification_id
  AND disposition.action = item.action AND disposition.subject_kind = item.subject_kind
  AND disposition.subject_stable_id = item.subject_stable_id
WITH outbox, verification, collect(disposition) AS persisted
WHERE size(persisted) = $expected_disposition_count
  AND all(item IN persisted WHERE item.unit_id = $unit_id AND item.generation = $generation
    AND item.sequence = $sequence AND item.attempt = $attempt AND item.owner_id = $owner_id
    AND item.control_token = $claim_token AND item.boundary_digest = $boundary_digest
  AND item.verification_id = $verification_id AND item.subject_kind IS NOT NULL
    AND item.subject_stable_id IS NOT NULL)
SET outbox.state = 'acknowledged', outbox.acknowledged_at = datetime(),
  outbox.verification_request_digest = $request_digest, outbox.verification_result_digest = $verification_digest
RETURN properties(verification) AS verification, properties(outbox) AS outbox,
  [item IN persisted | properties(item)] AS dispositions
"""

READ_RUN_VERIFICATION_COUNTS = """
MATCH (run:CrmDealRepairRun {repair_id: $repair_id, run_id: $run_id,
  boundary_digest: $boundary_digest, inventory_digest: $inventory_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  source_record_pks_json: $source_record_pks_json, status: 'qualified', execution_allowed: false})
OPTIONAL MATCH (unit:CrmDealRepairUnit {run_id: run.run_id})
OPTIONAL MATCH (verification:CrmDealRepairVerification {run_id: run.run_id, unit_id: unit.unit_id})
OPTIONAL MATCH (outbox:CrmDealRepairOutbox {run_id: run.run_id, unit_id: unit.unit_id})
OPTIONAL MATCH (disposition:CrmDealRepairSecondaryDisposition {
  run_id: run.run_id, unit_id: unit.unit_id
})
WITH run, collect(DISTINCT unit) AS units, collect(DISTINCT verification) AS verifications,
  collect(DISTINCT outbox) AS outboxes, collect(DISTINCT disposition) AS dispositions
RETURN run.eligible_unit_count AS eligible_unit_count,
  size([unit IN units WHERE unit.state = 'applied']) AS applied_units,
  size([unit IN units WHERE unit.state = 'review_required']) AS review_required_units,
  size([unit IN units WHERE unit.state IN ['allocated', 'quiesced', 'failed']]) AS incomplete_units,
  size([verification IN verifications WHERE verification.outcome = 'verified']) AS verified_units,
  size([verification IN verifications WHERE verification.outcome = 'drifted']) AS drifted_units,
  size([unit IN units WHERE unit.state = 'failed'])
    + size([verification IN verifications WHERE verification.outcome = 'failed']) AS failed_units,
  size([outbox IN outboxes WHERE outbox.state = 'acknowledged'
    AND outbox.verification_result_digest IS NOT NULL]) AS committed_attempts,
  size([outbox IN outboxes WHERE outbox.state = 'acknowledged'
    AND $replay_request_digest IS NOT NULL
    AND outbox.verification_request_digest = $replay_request_digest]) AS replay_no_op_attempts,
  coalesce(reduce(total = 0, verification IN verifications |
    total + coalesce(verification.expected_disposition_count, 0)), 0) AS expected_secondary_count,
  size(dispositions) AS observed_secondary_count,
  size([item IN dispositions WHERE item.outcome = 'reconciled']) AS reconciled_secondaries,
  size([item IN dispositions WHERE item.outcome = 'review_required']) AS review_required_secondaries,
  size([item IN dispositions WHERE item.outcome = 'failed']) AS failed_secondaries,
  size([item IN dispositions WHERE item.outcome = 'pending']) AS pending_secondaries
"""

READ_NEGATIVE_CONTROL_SNAPSHOT = """
UNWIND $items AS item
OPTIONAL MATCH (source:SourceRecord {source_record_pk: item.source_record_pk})
OPTIONAL MATCH (source)-[link:LINKED_TO]->(:Person)
WHERE coalesce(link.is_active, true) = true
OPTIONAL MATCH (person:Person)-[projection:IDENTIFIED_BY|LIVES_AT|HAS_FACT]->()
WHERE projection.source_record_pk = item.source_record_pk AND coalesce(projection.is_active, true) = true
OPTIONAL MATCH (mutation:CrmDealRepairMutationResult)
WHERE mutation.unit_id = item.unit_id OR mutation.mutation_id = source.repair_mutation_id
OPTIONAL MATCH (verification:CrmDealRepairVerification {unit_id: item.unit_id})
RETURN item.source_record_pk AS source_record_pk, properties(source) AS source_properties,
  count(DISTINCT link) AS active_link_count, count(DISTINCT projection) AS active_projection_count,
  count(DISTINCT mutation) + count(DISTINCT verification) AS repair_stamp_count
ORDER BY source_record_pk
"""

READ_RUN_GRAPH_TOTALS = """
WITH $frozen_source_record_pks AS frozen_source_record_pks
CALL (frozen_source_record_pks) {
  UNWIND frozen_source_record_pks AS source_record_pk
  OPTIONAL MATCH (old:SourceRecord {source_record_pk: source_record_pk})
    -[:PREVIOUS_VERSION_OF]->(replacement:SourceRecord)
  WHERE replacement.repair_mutation_id IS NOT NULL
  RETURN collect(DISTINCT replacement) AS replacements
}
WITH replacements, frozen_source_record_pks,
  frozen_source_record_pks + [replacement IN replacements | replacement.source_record_pk] AS closure_pks
CALL (replacements) {
  UNWIND replacements AS replacement
  OPTIONAL MATCH (replacement)-[link:LINKED_TO]->(:Person)
  WHERE coalesce(link.is_active, true) = true AND coalesce(link.authoritative, true) = true
  WITH replacement, count(link) AS active_link_count
  RETURN coalesce(sum(active_link_count), 0) AS active_links,
    coalesce(sum(CASE WHEN active_link_count > 1 THEN 1 ELSE 0 END), 0) AS unsupported_multi_links
}
CALL (closure_pks) {
  UNWIND closure_pks AS source_record_pk
  OPTIONAL MATCH (:Person)-[projection:IDENTIFIED_BY]->(identifier:Identifier)
  WHERE projection.source_record_pk = source_record_pk AND coalesce(projection.is_active, true) = true
  RETURN coalesce(sum(CASE WHEN identifier.identifier_type = 'phone' THEN 1 ELSE 0 END), 0) AS phones,
    coalesce(sum(CASE WHEN identifier.identifier_type = 'email' THEN 1 ELSE 0 END), 0) AS emails,
    coalesce(sum(CASE WHEN toLower(identifier.normalized_value) CONTAINS '@g.us' THEN 1 ELSE 0 END), 0) AS groups
}
RETURN active_links, unsupported_multi_links, phones, emails, groups
"""

READ_APPLIED_REPLACEMENT_OWNER = """
MATCH (:SourceRecord {source_record_pk: $source_record_pk, repair_mutation_id: $mutation_id})
  -[link:LINKED_TO]->(person:Person)
WHERE coalesce(link.is_active, true) = true AND coalesce(link.authoritative, true) = true
RETURN person.person_id AS person_id, toString(coalesce(link.linked_at, datetime())) AS effective_at
ORDER BY person.person_id
"""

READ_NEGATIVE_CONTROL_FULL_STATE = """
UNWIND $items AS item
WITH item.source_record_pk AS source_record_pk,
  item.closure_source_record_pks AS closure_source_record_pks
OPTIONAL MATCH (source:SourceRecord {source_record_pk: source_record_pk, record_type: 'crm_deal'})
OPTIONAL MATCH (source)-[:FROM_SOURCE]->(source_system:SourceSystem {source_key: 'bitrix_chat'})
WITH source_record_pk, closure_source_record_pks, source, count(source_system) AS source_system_matches
CALL (source) {
  WITH source
  OPTIONAL MATCH (source)-[link:LINKED_TO]->(owner:Person)
  WITH link, owner
  WHERE link IS NOT NULL
  RETURN collect({
    person_id: owner.person_id,
    is_active: coalesce(link.is_active, true),
    relationship_type: type(link),
    relationship_properties: properties(link)
  }) AS linked_people,
  count(link) AS link_row_count
}
CALL (source) {
  WITH source
  OPTIONAL MATCH (start)-[projection]->(target)
  WHERE (projection.source_record_pk = source.source_record_pk
         OR (start = source AND type(projection) = 'DESCRIBES_ADDRESS'
             AND projection.source_record_pk IS NULL))
    AND NOT (start = source AND target:Person AND type(projection) = 'LINKED_TO')
  WITH projection, start, target
  WHERE projection IS NOT NULL
  RETURN collect({
    relationship_type: type(projection),
    is_active: coalesce(projection.is_active, true),
    relationship_properties: properties(projection),
    owner_person_id: start.person_id,
    identifier_type: target.identifier_type,
    identifier_value: target.normalized_value,
    address_id: target.address_id,
    target_source_record_pk: target.source_record_pk,
    source_record_pk: projection.source_record_pk
  }) AS projections,
  count(projection) AS projection_row_count
}
CALL (source) {
  WITH source
  OPTIONAL MATCH (version:SourceRecord {source_record_id: source.source_record_id,
    record_type: 'crm_deal'})-[:FROM_SOURCE]->(:SourceSystem {source_key: 'bitrix_chat'})
  WITH version
  WHERE version IS NOT NULL
  RETURN collect({
    source_record_pk: version.source_record_pk,
    source_record_version: version.source_record_version,
    lifecycle_status: version.lifecycle_status,
    is_latest: version.is_latest,
    raw_payload: version.raw_payload,
    normalized_payload: version.normalized_payload
  }) AS logical_versions
}
CALL (source) {
  WITH source
  OPTIONAL MATCH (descendant:SourceRecord)-[:CHILD_OF*1..2]->(source)
  OPTIONAL MATCH (descendant)-[descendant_link:LINKED_TO]->(descendant_owner:Person)
  WITH descendant, descendant_link, descendant_owner
  WHERE descendant IS NOT NULL
  RETURN collect({
    record_type: descendant.record_type,
    source_record_pk: descendant.source_record_pk,
    source_record_id: descendant.source_record_id,
    lifecycle_status: descendant.lifecycle_status,
    relationship_type: type(descendant_link),
    relationship_is_active: coalesce(descendant_link.is_active, true),
    owner_person_id: descendant_owner.person_id
  }) AS descendants
}
CALL (source) {
  WITH source
  OPTIONAL MATCH (decision:MatchDecision)-[:ABOUT_LEFT|ABOUT_RIGHT]->(source)
  OPTIONAL MATCH (review:ReviewCase)-[:FOR_DECISION]->(decision)
  WITH decision, review
  WHERE decision IS NOT NULL OR review IS NOT NULL
  RETURN collect(DISTINCT {
    evidence_type: 'record_to_person',
    match_decision_id: decision.match_decision_id,
    decision: decision.decision,
    policy_version: decision.policy_version,
    engine_type: decision.engine_type,
    review_case_id: review.review_case_id,
    review_resolution: review.resolution,
    review_queue_state: review.queue_state
  }) AS record_decisions_and_reviews
}
CALL (source) {
  WITH source
  OPTIONAL MATCH (source)-[:LINKED_TO]->(owner:Person)
  OPTIONAL MATCH (pair_decision:MatchDecision)-[owner_about:ABOUT_LEFT|ABOUT_RIGHT]->(owner)
  OPTIONAL MATCH (pair_decision)-[counterpart_about:ABOUT_LEFT|ABOUT_RIGHT]->(counterpart:Person)
  OPTIONAL MATCH (pair_review:ReviewCase)-[:FOR_DECISION]->(pair_decision)
  WITH owner, pair_decision, owner_about, counterpart, counterpart_about, pair_review
  WHERE pair_decision IS NOT NULL
    AND pair_decision.engine_type = 'pair_audit'
    AND owner_about.entity_type = 'person'
    AND counterpart_about.entity_type = 'person'
    AND counterpart <> owner
  RETURN collect(DISTINCT {
    evidence_type: 'pair_audit',
    owner_person_id: owner.person_id,
    counterpart_person_id: counterpart.person_id,
    owner_about_relationship_type: type(owner_about),
    counterpart_about_relationship_type: type(counterpart_about),
    match_decision_id: pair_decision.match_decision_id,
    decision: pair_decision.decision,
    policy_version: pair_decision.policy_version,
    engine_type: pair_decision.engine_type,
    review_case_id: pair_review.review_case_id,
    review_resolution: pair_review.resolution,
    review_queue_state: pair_review.queue_state
  }) AS pair_decisions_and_reviews
}
CALL (source) {
  WITH source
  OPTIONAL MATCH (source)-[:LINKED_TO]->(owner:Person)
  WITH owner
  WHERE owner IS NOT NULL
  RETURN collect({
    evidence_type: 'owner_profile',
    owner_person_id: owner.person_id,
    survivorship_overrides: owner.survivorship_overrides,
    crm_deal_count: owner.crm_deal_count,
    golden_profile_version: owner.golden_profile_version
  }) AS owner_profiles
}
CALL (source) {
  WITH source
  OPTIONAL MATCH (source)-[:LINKED_TO]->(owner:Person)
  OPTIONAL MATCH (owner)-[lock:NO_MATCH_LOCK]-(other:Person)
  WITH owner, lock, other
  WHERE lock IS NOT NULL
  RETURN collect({
    evidence_type: 'no_match_lock',
    owner_person_id: owner.person_id,
    no_match_lock_id: lock.lock_id,
    lock_other_person_id: other.person_id
  }) AS owner_locks
}
CALL (source) {
  WITH source
  OPTIONAL MATCH (source)-[:LINKED_TO]->(owner:Person)
  OPTIONAL MATCH (owner)-[outgoing_rel:MERGED_INTO]->(survivor:Person)
  WITH owner, outgoing_rel, survivor
  WHERE outgoing_rel IS NOT NULL
  RETURN collect(DISTINCT {
    evidence_type: 'merge_lineage',
    direction: 'outgoing',
    owner_person_id: owner.person_id,
    merge_event_id: outgoing_rel.merge_event_id,
    absorbed_person_id: owner.person_id,
    survivor_person_id: survivor.person_id
  }) AS outgoing_owner_merges
}
CALL (source) {
  WITH source
  OPTIONAL MATCH (source)-[:LINKED_TO]->(owner:Person)
  OPTIONAL MATCH (absorbed:Person)-[incoming_rel:MERGED_INTO]->(owner)
  WITH owner, incoming_rel, absorbed
  WHERE incoming_rel IS NOT NULL
  RETURN collect(DISTINCT {
    evidence_type: 'merge_lineage',
    direction: 'incoming',
    owner_person_id: owner.person_id,
    merge_event_id: incoming_rel.merge_event_id,
    absorbed_person_id: absorbed.person_id,
    survivor_person_id: owner.person_id
  }) AS incoming_owner_merges
}
CALL (closure_source_record_pks) {
  UNWIND closure_source_record_pks AS closure_source_record_pk
  OPTIONAL MATCH (closure_source:SourceRecord {source_record_pk: closure_source_record_pk})
  WITH closure_source_record_pks, collect(DISTINCT closure_source) AS closure_sources
  OPTIONAL MATCH (left)-[relationship]->()
  WHERE relationship.source_record_pk IN closure_source_record_pks
     OR (type(relationship) = 'DESCRIBES_ADDRESS'
         AND left:SourceRecord AND left.source_record_pk IN closure_source_record_pks)
  WITH closure_sources, collect(DISTINCT relationship) AS relationships
  RETURN size([node IN closure_sources WHERE node.repair_mutation_id IS NOT NULL
      OR node.retired_by_repair_mutation_id IS NOT NULL])
    + size([relationship IN relationships WHERE relationship.repair_mutation_id IS NOT NULL
      OR relationship.retired_by_repair_mutation_id IS NOT NULL]) AS graph_stamp_count
}
CALL (source_record_pk) {
  OPTIONAL MATCH (unit:CrmDealRepairUnit {source_record_pk: source_record_pk})
  OPTIONAL MATCH (run:CrmDealRepairRun {run_id: unit.run_id})
    -[:HAS_REPAIR_MUTATION]->(mutation:CrmDealRepairMutationResult {unit_id: unit.unit_id})
  OPTIONAL MATCH (verification:CrmDealRepairVerification {run_id: unit.run_id, unit_id: unit.unit_id})
  OPTIONAL MATCH (disposition:CrmDealRepairSecondaryDisposition {run_id: unit.run_id,
    unit_id: unit.unit_id})
  RETURN count(DISTINCT mutation) + count(DISTINCT verification)
    + count(DISTINCT disposition) AS ledger_stamp_count
}
RETURN source_record_pk, source_system_matches,
  source {.*, observed_at: toString(source.observed_at)} AS source_properties,
  linked_people, projections, logical_versions, descendants,
  record_decisions_and_reviews + pair_decisions_and_reviews AS decisions_and_reviews,
  owner_profiles + owner_locks + outgoing_owner_merges + incoming_owner_merges AS owner_impacts,
  link_row_count, projection_row_count, graph_stamp_count, ledger_stamp_count
ORDER BY source_record_pk
"""

READ_AFFECTED_PERSON_DERIVED_STATE = """
UNWIND $person_ids AS person_id
MATCH (person:Person {person_id: person_id, status: 'active'})
RETURN person.person_id AS person_id, person.crm_deal_count AS crm_deal_count,
  person.analysis_input_revision AS analysis_input_revision,
  person.survivorship_overrides AS survivorship_overrides,
  person.preferred_full_name AS preferred_full_name, person.preferred_dob AS preferred_dob,
  person.preferred_phone AS preferred_phone, person.preferred_email AS preferred_email,
  person.preferred_address_id AS preferred_address_id, person.preferred_nric AS preferred_nric,
  person.preferred_race_ethnicity AS preferred_race_ethnicity,
  person.profile_completeness_score AS profile_completeness_score,
  person.golden_profile_version AS golden_profile_version
ORDER BY person_id
"""

READ_EXPECTED_AFFECTED_CRM_DEAL_COUNTS = """
UNWIND $person_ids AS person_id
MATCH (person:Person {person_id: person_id, status: 'active'})
CALL (person) {
  MATCH (deal:SourceRecord {record_type: 'crm_deal'})-[link:LINKED_TO]->(person)
  WHERE coalesce(link.is_active, true) = true
    AND (deal.history_family IS NULL OR deal.history_family = 'activity')
    AND (deal.lifecycle_status = 'active'
      OR (deal.lifecycle_status IS NULL AND deal.is_latest = true))
    AND EXISTS {
      MATCH (deal)-[:FROM_SOURCE]->(:SourceSystem {source_key: 'bitrix_chat'})
    }
  RETURN count(DISTINCT deal) AS expected_crm_deal_count
}
RETURN person.person_id AS person_id, expected_crm_deal_count
ORDER BY person_id
"""

READ_EXISTING_VERIFICATION_DISPOSITIONS = """
OPTIONAL MATCH (verification:CrmDealRepairVerification {run_id: $run_id, unit_id: $unit_id})
OPTIONAL MATCH (disposition:CrmDealRepairSecondaryDisposition {run_id: $run_id, unit_id: $unit_id})
RETURN count(DISTINCT verification) AS verification_count,
  count(DISTINCT disposition) AS disposition_count
"""

READ_IDENTITY_LINK_REVISION_CAUSE = """
MATCH (revision:IdentityLinkRevision {cause_key: $cause_key})
RETURN revision.source_system AS source_system,
  revision.source_instance_id AS source_instance_id,
  revision.source_entity_type AS source_entity_type,
  revision.source_entity_id AS source_entity_id,
  revision.identity_policy_version AS identity_policy_version,
  revision.link_status AS link_status,
  revision.hyperp_person_id AS person_id,
  revision.resolution_kind AS resolution_kind,
  toString(revision.effective_at) AS effective_at,
  revision.cause_key AS cause_key
"""

READ_REPLACEMENT_EFFECTIVE_TIME = """
MATCH (source:SourceRecord {source_record_pk: $source_record_pk, repair_mutation_id: $mutation_id})
RETURN toString(coalesce(source.activated_at, source.review_staged_at, source.observed_at, datetime()))
  AS effective_at
"""

READ_REPAIR_PAIR_AUDIT_CASES = """
UNWIND $review_case_ids AS review_case_id
MATCH (review:ReviewCase {review_case_id: review_case_id})-[:FOR_DECISION]->
  (decision:MatchDecision {engine_type: 'pair_audit'})
MATCH (decision)-[:ABOUT_LEFT {entity_type: 'person'}]->(left:Person)
MATCH (decision)-[:ABOUT_RIGHT {entity_type: 'person'}]->(right:Person)
RETURN review.review_case_id AS review_case_id, review.queue_state AS queue_state,
  review.resolution AS resolution,
  left.person_id AS left_person_id, right.person_id AS right_person_id,
  decision.confidence AS confidence, decision.reasons AS reasons,
  decision.feature_snapshot AS feature_snapshot, decision.engine_version AS engine_version,
  decision.policy_version AS policy_version
ORDER BY review_case_id
"""

READ_PAIR_AUDIT_CASE_STATE = """
MATCH (review:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->
  (decision:MatchDecision {engine_type: 'pair_audit'})
RETURN review.review_case_id AS review_case_id, review.queue_state AS queue_state,
  review.resolution AS resolution, decision.reasons AS reasons,
  decision.confidence AS confidence, decision.feature_snapshot AS feature_snapshot,
  decision.engine_version AS engine_version, decision.policy_version AS policy_version
"""

READ_RETIRED_RELATIONSHIP_SNAPSHOTS = """
WITH $retired_source_record_pks AS retired_source_record_pks
MATCH (left)-[relationship:LINKED_TO|IDENTIFIED_BY|LIVES_AT|HAS_FACT|DESCRIBES_ADDRESS]->(right)
WHERE (type(relationship) IN ['LINKED_TO', 'DESCRIBES_ADDRESS']
       AND left:SourceRecord AND left.source_record_pk IN retired_source_record_pks)
   OR (type(relationship) IN ['IDENTIFIED_BY', 'LIVES_AT', 'HAS_FACT']
       AND relationship.source_record_pk IN retired_source_record_pks)
RETURN type(relationship) AS relationship_type,
  labels(left) AS left_labels,
  properties(left) AS left_properties,
  CASE
    WHEN left.source_record_pk IS NOT NULL THEN {labels: labels(left), key: 'source_record_pk', value: left.source_record_pk}
    WHEN left.person_id IS NOT NULL THEN {labels: labels(left), key: 'person_id', value: left.person_id}
    WHEN left.match_decision_id IS NOT NULL THEN {labels: labels(left), key: 'match_decision_id', value: left.match_decision_id}
    WHEN left.review_case_id IS NOT NULL THEN {labels: labels(left), key: 'review_case_id', value: left.review_case_id}
    WHEN left.identifier_key IS NOT NULL THEN {labels: labels(left), key: 'identifier_key', value: left.identifier_key}
    WHEN left.address_id IS NOT NULL THEN {labels: labels(left), key: 'address_id', value: left.address_id}
    WHEN left.fact_id IS NOT NULL THEN {labels: labels(left), key: 'fact_id', value: left.fact_id}
    WHEN left.entity_key IS NOT NULL THEN {labels: labels(left), key: 'entity_key', value: left.entity_key}
    ELSE null
  END AS left_identity,
  labels(right) AS right_labels,
  properties(right) AS right_properties,
  CASE
    WHEN right.source_record_pk IS NOT NULL THEN {labels: labels(right), key: 'source_record_pk', value: right.source_record_pk}
    WHEN right.person_id IS NOT NULL THEN {labels: labels(right), key: 'person_id', value: right.person_id}
    WHEN right.match_decision_id IS NOT NULL THEN {labels: labels(right), key: 'match_decision_id', value: right.match_decision_id}
    WHEN right.review_case_id IS NOT NULL THEN {labels: labels(right), key: 'review_case_id', value: right.review_case_id}
    WHEN right.identifier_key IS NOT NULL THEN {labels: labels(right), key: 'identifier_key', value: right.identifier_key}
    WHEN right.address_id IS NOT NULL THEN {labels: labels(right), key: 'address_id', value: right.address_id}
    WHEN right.fact_id IS NOT NULL THEN {labels: labels(right), key: 'fact_id', value: right.fact_id}
    WHEN right.entity_key IS NOT NULL THEN {labels: labels(right), key: 'entity_key', value: right.entity_key}
    ELSE null
  END AS right_identity,
  properties(relationship) AS relationship_properties
ORDER BY relationship_type, elementId(relationship)
"""

READ_ACTIVE_PERSON_IDS = """
UNWIND $person_ids AS person_id
MATCH (person:Person {person_id: person_id, status: 'active'})
RETURN person.person_id AS person_id ORDER BY person_id
"""
