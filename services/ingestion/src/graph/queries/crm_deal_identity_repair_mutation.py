"""Parameterized queries for one atomic CRM-deal repair mutation."""

from __future__ import annotations

LOCK_REPAIR_MUTATION_UNIT = """
MATCH (unit:CrmDealRepairUnit {run_id: $run_id, unit_id: $unit_id})
SET unit.mutation_lock_id = coalesce(unit.mutation_lock_id, $mutation_id)
WITH unit
WHERE unit.mutation_lock_id = $mutation_id
RETURN unit.state AS state
"""

FIND_COMMITTED_REPAIR_MUTATION = """
MATCH (result:CrmDealRepairMutationResult {run_id: $run_id, unit_id: $unit_id})
OPTIONAL MATCH (image:CrmDealRepairRollbackImage {
  run_id: result.run_id, rollback_image_id: result.rollback_image_id
})
OPTIONAL MATCH (checkpoint:CrmDealRepairCheckpoint {
  run_id: result.run_id, checkpoint_id: result.checkpoint_id
})
OPTIONAL MATCH (outbox:CrmDealRepairOutbox {
  run_id: result.run_id, event_id: result.outbox_event_id
})
RETURN properties(result) AS result, properties(image) AS image,
       properties(checkpoint) AS checkpoint, properties(outbox) AS outbox
"""

LOCK_AND_ASSERT_REPAIR_MUTATION_GUARD = """
MATCH (run:CrmDealRepairRun {
  run_id: $run_id, boundary_digest: $boundary_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  status: 'qualified', execution_allowed: false
})
MATCH (unit:CrmDealRepairUnit {
  run_id: $run_id, unit_id: $unit_id, generation: $generation, sequence: $sequence,
  attempt: $attempt, boundary_digest: $boundary_digest,
  inventory_fingerprint: $unit_fingerprint, mutation_lock_id: $mutation_id
})
WHERE unit.state IN ['allocated', 'quiesced']
MATCH (fence:CrmDealRepairFence {
  run_id: $run_id, unit_id: $unit_id, fence_id: $fence_id,
  generation: $generation, sequence: $sequence, attempt: $attempt,
  owner_id: $owner_id, token: $fence_token,
  boundary_digest: $boundary_digest, state: 'claimed'
})
MATCH (dispatch:BitrixDispatchControl {
  source_key: 'bitrix_chat', control_instance_id: $control_instance_id, blocked: true
})
MATCH (source_instance:BitrixSourceInstance {
  source_key: 'bitrix_chat', source_instance_id: $source_instance_id, status: 'active'
})-[:INSTANCE_OF]->(:SourceSystem {source_key: 'bitrix_chat', is_active: true})
MATCH (control_instance:BitrixSourceInstance {
  source_key: 'bitrix_chat', source_instance_id: $control_instance_id, status: 'active'
})-[:INSTANCE_OF]->(:SourceSystem {source_key: 'bitrix_chat', is_active: true})
MATCH (source_instance)-[:OWNS_BITRIX_CONTROL]->(binding:BitrixExecutionSourceBinding {
  source_key: 'bitrix_chat', source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id
})
MATCH (record:SourceRecord {
  source_record_pk: $source_record_pk, source_record_id: $source_record_id,
  source_instance_id: $source_instance_id, record_type: 'crm_deal'
})-[:FROM_SOURCE]->(:SourceSystem {source_key: 'bitrix_chat'})
WHERE record.lifecycle_status = 'active'
   OR (record.lifecycle_status IS NULL AND record.is_latest = true)
MERGE (lock:SourceRecordIdentityLock {
  source_system: 'bitrix_chat', source_instance_id: $source_instance_id,
  source_record_id: $source_record_id
})
SET lock.locked_at = datetime(), lock.repair_mutation_id = $mutation_id
RETURN properties(record) AS source, record.entity_key AS entity_key
"""

READ_MUTATION_GRAPH_SNAPSHOT = """
MATCH (source:SourceRecord {source_record_pk: $source_record_pk})
CALL {
  WITH source
  OPTIONAL MATCH path=(descendant:SourceRecord)-[:CHILD_OF*1..2]->(source)
  WHERE descendant.source_record_pk IN $retired_source_record_pks
  WITH path, descendant ORDER BY descendant.source_record_pk,
    [node IN nodes(path) | node.source_record_pk]
  RETURN collect(CASE WHEN descendant IS NULL THEN NULL ELSE {
    source_record_pk: descendant.source_record_pk,
    properties: properties(descendant),
    ancestry_path: [node IN nodes(path) | node.source_record_pk]
  } END) AS descendants
}
CALL {
  WITH source
  WITH source, [source.source_record_pk] + $retired_source_record_pks AS affected_pks
  MATCH (left)-[relationship]->(right)
  WHERE (left:SourceRecord AND left.source_record_pk IN affected_pks)
     OR (right:SourceRecord AND right.source_record_pk IN affected_pks)
     OR relationship.source_record_pk IN affected_pks
  WITH left, relationship, right ORDER BY type(relationship),
    coalesce(left.source_record_pk, left.person_id, left.identifier_key, left.address_id, ''),
    coalesce(right.source_record_pk, right.person_id, right.identifier_key, right.address_id, ''),
    toString(properties(relationship))
  RETURN collect({
    direction: 'outgoing', left_labels: labels(left), left_properties: properties(left),
    relationship_type: type(relationship), relationship_properties: properties(relationship),
    right_labels: labels(right), right_properties: properties(right)
  }) AS relationships
}
CALL {
  WITH source
  OPTIONAL MATCH (decision:MatchDecision)-[:ABOUT_LEFT|ABOUT_RIGHT]->(source)
  OPTIONAL MATCH (review:ReviewCase)-[:FOR_DECISION]->(decision)
  WITH decision, review ORDER BY decision.match_decision_id, review.review_case_id
  RETURN collect(CASE WHEN decision IS NULL THEN NULL ELSE {
    decision_properties: properties(decision),
    review_properties: CASE WHEN review IS NULL THEN null ELSE properties(review) END
  } END) AS decisions_and_reviews
}
RETURN properties(source) AS source, descendants, relationships, decisions_and_reviews
"""

READ_LOCKED_REPAIR_AUTHORITY = """
MATCH (deal:SourceRecord {source_record_pk: $source_record_pk})
MATCH (deal)-[current:LINKED_TO]->(person:Person)
WHERE coalesce(current.is_active, true) = true
CALL {
  WITH deal, person
  OPTIONAL MATCH (support:SourceRecord)-[support_link:LINKED_TO]->(person)
  WHERE coalesce(support_link.is_active, true) = true
    AND support.source_record_pk <> deal.source_record_pk
    AND support.source_instance_id = $source_instance_id
    AND support.source_entity_type IN ['contact', 'lead']
    AND support.record_type <> 'crm_deal'
    AND (support.lifecycle_status = 'active'
      OR (support.lifecycle_status IS NULL AND support.is_latest = true))
    AND support.identity_policy_version IN ['crm_contact_identity_v1', 'crm_lead_identity_v1']
  OPTIONAL MATCH (person)-[identifier_link:IDENTIFIED_BY]->(identifier:Identifier)
  WHERE identifier_link.source_record_pk = support.source_record_pk
    AND coalesce(identifier_link.is_active, true) = true
    AND identifier.identifier_type IN ['crm_contact_id', 'crm_lead_id']
    AND identifier.source_instance_id = $source_instance_id
  RETURN collect(CASE WHEN support IS NULL OR identifier IS NULL THEN NULL ELSE {
    source_record_pk: support.source_record_pk, source_record_id: support.source_record_id,
    source_instance_id: support.source_instance_id, record_type: support.record_type,
    source_entity_type: support.source_entity_type,
    identity_policy_version: support.identity_policy_version,
    lifecycle_status: support.lifecycle_status,
    identifier_type: identifier.identifier_type,
    identifier_scope: identifier.identifier_scope,
    identifier_source_instance_id: identifier.source_instance_id,
    identifier_link_source_record_pk: identifier_link.source_record_pk
  } END) AS independent_rows
}
CALL {
  WITH deal, person
  OPTIONAL MATCH (decision:MatchDecision)-[:ABOUT_RIGHT {entity_type: 'person'}]->(person)
  OPTIONAL MATCH (decision)-[:ABOUT_LEFT {entity_type: 'source_record'}]->(reviewed:SourceRecord)
  OPTIONAL MATCH (review:ReviewCase)-[:FOR_DECISION]->(decision)
  WHERE decision.policy_version = 'crm_deal_identity_v2'
    AND decision.decision = 'merge'
    AND reviewed.source_record_pk <> deal.source_record_pk
    AND review.resolution IN ['approved', 'merge', 'matched']
  RETURN collect(CASE WHEN reviewed IS NULL THEN NULL ELSE {
    source_record_pk: reviewed.source_record_pk,
    match_decision_id: decision.match_decision_id,
    review_case_id: review.review_case_id,
    resolution: review.resolution
  } END) AS reviewed_rows
}
CALL {
  WITH deal, person
  OPTIONAL MATCH (historical:SourceRecord)-[historical_link:LINKED_TO]->(person)
  WHERE historical.record_type = 'crm_deal'
    AND historical.source_record_pk <> deal.source_record_pk
    AND coalesce(historical_link.is_active, true) = true
  RETURN collect(CASE WHEN historical IS NULL THEN NULL ELSE {
    source_record_pk: historical.source_record_pk,
    source_record_id: historical.source_record_id
  } END) AS historical_rows
}
CALL {
  WITH deal, person
  OPTIONAL MATCH (self_support:SourceRecord)-[:CHILD_OF*0..2]->(deal)
  OPTIONAL MATCH (self_support)-[self_link:LINKED_TO]->(person)
  WHERE coalesce(self_link.is_active, true) = true
  RETURN collect(CASE WHEN self_support IS NULL THEN NULL ELSE {
    source_record_pk: self_support.source_record_pk
  } END) AS self_rows
}
CALL {
  WITH person
  OPTIONAL MATCH (person)-[lock:NO_MATCH_LOCK]-(other:Person)
  WHERE lock.expires_at IS NULL OR lock.expires_at > datetime()
  RETURN count(lock) AS active_no_match_locks
}
RETURN person.person_id AS person_id, independent_rows, reviewed_rows,
       historical_rows, self_rows, active_no_match_locks
ORDER BY person_id
"""

LOCK_SUPPORT_SOURCE_RECORDS = """
UNWIND $support_rows AS support_row
MATCH (support:SourceRecord {source_record_pk: support_row.source_record_pk})
MERGE (lock:SourceRecordIdentityLock {
  source_system: 'bitrix_chat', source_instance_id: support.source_instance_id,
  source_record_id: support.source_record_id
})
SET lock.locked_at = datetime(), lock.repair_mutation_id = $mutation_id
RETURN count(lock) AS locked_count
"""

CREATE_REPAIRED_SOURCE_RECORD = """
MATCH (source:SourceSystem {source_key: 'bitrix_chat'})
MATCH (old:SourceRecord {source_record_pk: $old_source_record_pk})
WHERE old.lifecycle_status = 'active'
  OR (old.lifecycle_status IS NULL AND old.is_latest = true)
OPTIONAL MATCH (conflict:SourceRecord {source_version_key: $source_version_key})
WITH source, old, conflict
WHERE conflict IS NULL OR conflict.source_record_pk = $new_source_record_pk
MERGE (new:SourceRecord {source_record_pk: $new_source_record_pk})
ON CREATE SET new.source_record_id = $source_record_id,
  new.source_instance_id = $source_instance_id,
  new.source_record_version = $source_record_version,
  new.source_version_key = $source_version_key, new.entity_key = $entity_key,
  new.expected_active_source_record_pk = $old_source_record_pk,
  new.lifecycle_status = 'pending_review', new.is_latest = false,
  new.record_type = 'crm_deal', new.observed_at = $observed_at,
  new.ingested_at = datetime(), new.record_hash = $record_hash,
  new.raw_payload = $raw_payload, new.normalized_payload = $normalized_payload,
  new.source_entity_type = 'deal', new.source_entity_id = $deal_id,
  new.identity_policy_version = 'crm_deal_identity_v2',
  new.identity_link_key = $identity_link_key, new.repair_mutation_id = $mutation_id
MERGE (new)-[:FROM_SOURCE]->(source)
MERGE (old)-[:PREVIOUS_VERSION_OF]->(new)
RETURN new.source_record_pk AS source_record_pk
"""

RETIRE_EXACT_CONTAMINATION = """
UNWIND $retired_source_record_pks AS source_record_pk
CALL {
  WITH source_record_pk
  MATCH (:SourceRecord {source_record_pk: source_record_pk})-[link:LINKED_TO]->(:Person)
  WHERE coalesce(link.is_active, true) = true
  SET link.is_active = false, link.retired_by_repair_mutation_id = $mutation_id,
      link.updated_at = datetime()
  RETURN count(link) AS links
}
CALL {
  WITH source_record_pk
  MATCH (:Person)-[projection:IDENTIFIED_BY|LIVES_AT|HAS_FACT]->()
  WHERE projection.source_record_pk = source_record_pk
    AND coalesce(projection.is_active, true) = true
  SET projection.is_active = false,
      projection.retired_by_repair_mutation_id = $mutation_id,
      projection.updated_at = datetime()
  RETURN count(projection) AS projections
}
CALL {
  WITH source_record_pk
  MATCH (:SourceRecord {source_record_pk: source_record_pk})-[address:DESCRIBES_ADDRESS]->()
  WHERE coalesce(address.is_active, true) = true
  SET address.is_active = false,
      address.retired_by_repair_mutation_id = $mutation_id,
      address.updated_at = datetime()
  RETURN count(address) AS addresses
}
RETURN sum(links) AS retired_links, sum(projections) AS retired_projections,
       sum(addresses) AS retired_addresses
"""

CREATE_REPAIR_DECISION = """
MATCH (source:SourceRecord {source_record_pk: $new_source_record_pk})
CREATE (decision:MatchDecision {
  match_decision_id: $match_decision_id, engine_type: 'deterministic',
  engine_version: 'crm_deal_identity_repair_v1', decision: $decision,
  confidence: 1.0, reasons: $reason_codes, blocking_conflicts: [],
  review_candidate_person_ids: $review_candidate_person_ids,
  feature_snapshot: $feature_snapshot, policy_version: 'crm_deal_identity_v2',
  repair_mutation_id: $mutation_id, created_at: datetime(), retention_expires_at: null
})
CREATE (decision)-[:ABOUT_LEFT {entity_type: 'source_record'}]->(source)
RETURN decision.match_decision_id AS match_decision_id
"""

STAGE_ACTIVE_REPAIR_LINK = """
MATCH (source:SourceRecord {source_record_pk: $new_source_record_pk})
MATCH (decision:MatchDecision {match_decision_id: $match_decision_id})
MATCH (person:Person {person_id: $person_id})
CREATE (decision)-[:ABOUT_RIGHT {entity_type: 'person'}]->(person)
CREATE (source)-[:LINKED_TO {
  is_active: true, provisional: false, authoritative: true,
  source_record_pk: $new_source_record_pk, repair_mutation_id: $mutation_id,
  linked_at: datetime()
}]->(person)
RETURN person.person_id AS person_id
"""

STAGE_PROVISIONAL_REPAIR_LINK = """
MATCH (source:SourceRecord {source_record_pk: $new_source_record_pk})
MATCH (decision:MatchDecision {match_decision_id: $match_decision_id})
MATCH (person:Person {person_id: $person_id})
CREATE (decision)-[:ABOUT_RIGHT {entity_type: 'person'}]->(person)
CREATE (source)-[:LINKED_TO {
  is_active: false, provisional: true, authoritative: false,
  source_record_pk: $new_source_record_pk, repair_mutation_id: $mutation_id,
  linked_at: datetime()
}]->(person)
RETURN person.person_id AS person_id
"""

CREATE_REPAIR_REVIEW_CASE = """
MATCH (decision:MatchDecision {match_decision_id: $match_decision_id})
CREATE (review:ReviewCase {
  review_case_id: $review_case_id, priority: 100, queue_state: 'open',
  assigned_to: null, follow_up_at: null, sla_due_at: datetime($sla_due_at),
  resolution: null, resolved_at: null, actions: [], created_at: datetime(),
  updated_at: datetime(), repair_mutation_id: $mutation_id
})-[:FOR_DECISION]->(decision)
RETURN review.review_case_id AS review_case_id
"""

STAGE_REPAIR_IDENTIFIERS = """
UNWIND $identifiers AS identifier_row
MATCH (person:Person {person_id: $person_id})
MERGE (identifier:Identifier {
  identifier_type: identifier_row.identifier_type,
  identifier_scope: identifier_row.identifier_scope,
  normalized_value: identifier_row.normalized_value
})
ON CREATE SET identifier.source_instance_id = identifier_row.source_instance_id,
  identifier.created_at = datetime()
MERGE (person)-[link:IDENTIFIED_BY {
  source_system_key: 'bitrix_chat', source_record_pk: $source_record_pk
}]->(identifier)
ON CREATE SET link.is_verified = identifier_row.is_verified,
  link.verification_method = null, link.is_active = true,
  link.quality_flag = identifier_row.quality_flag,
  link.first_seen_at = datetime(), link.last_seen_at = datetime(),
  link.last_confirmed_at = datetime()
ON MATCH SET link.is_active = true, link.last_seen_at = datetime(),
  link.last_confirmed_at = datetime()
RETURN count(link) AS identifier_count
"""

STAGE_REPAIR_FACTS = """
UNWIND $facts AS fact
MATCH (person:Person {person_id: $person_id})
MATCH (source:SourceRecord {source_record_pk: $source_record_pk})
CREATE (person)-[:HAS_FACT {
  attribute_name: fact.attribute_name, attribute_value: fact.attribute_value,
  source_record_pk: $source_record_pk, source_trust_tier: 2,
  confidence: 1.0, quality_flag: fact.quality_flag, is_active: true,
  is_current_hint: false, observed_at: datetime($observed_at), created_at: datetime()
}]->(source)
RETURN count(*) AS fact_count
"""

STAGE_REVIEW_SOURCE_RECORD = """
MATCH (old:SourceRecord {source_record_pk: $old_source_record_pk})
MATCH (new:SourceRecord {
  source_record_pk: $new_source_record_pk, lifecycle_status: 'pending_review'
})
WHERE old.lifecycle_status = 'active'
   OR (old.lifecycle_status IS NULL AND old.is_latest = true)
SET old.lifecycle_status = 'superseded', old.is_latest = false,
    old.superseded_at = datetime(), new.is_latest = true,
    new.review_staged_at = datetime()
RETURN new.source_record_pk AS source_record_pk
"""
ACTIVATE_REPAIRED_SOURCE_RECORD = """
MATCH (old:SourceRecord {source_record_pk: $old_source_record_pk})
MATCH (new:SourceRecord {
  source_record_pk: $new_source_record_pk, lifecycle_status: 'pending_review'
})
WHERE old.lifecycle_status = 'active'
   OR (old.lifecycle_status IS NULL AND old.is_latest = true)
SET old.lifecycle_status = 'superseded', old.is_latest = false,
    old.superseded_at = datetime(), new.lifecycle_status = 'active',
    new.is_latest = true, new.activated_at = datetime()
RETURN new.source_record_pk AS source_record_pk
"""

PERSIST_REPAIR_MUTATION_LEDGER = """
MATCH (run:CrmDealRepairRun {run_id: $run_id, boundary_digest: $boundary_digest})
MATCH (unit:CrmDealRepairUnit {
  run_id: $run_id, unit_id: $unit_id, generation: $generation,
  sequence: $sequence, attempt: $attempt, boundary_digest: $boundary_digest,
  inventory_fingerprint: $unit_fingerprint, mutation_lock_id: $mutation_id
})
WHERE unit.state = $expected_unit_state
MATCH (fence:CrmDealRepairFence {
  run_id: $run_id, unit_id: $unit_id, fence_id: $fence_id,
  generation: $generation, sequence: $sequence, attempt: $attempt,
  owner_id: $owner_id, token: $fence_token,
  boundary_digest: $boundary_digest, state: 'claimed'
})
MATCH (:BitrixDispatchControl {
  source_key: 'bitrix_chat', control_instance_id: $control_instance_id, blocked: true
})
CREATE (image:CrmDealRepairRollbackImage {
  run_id: $run_id, unit_id: $unit_id, rollback_image_id: $rollback_image_id,
  generation: $generation, sequence: $sequence, attempt: $attempt,
  owner_id: $owner_id, fence_token: $fence_token,
  boundary_digest: $boundary_digest, source_fingerprint: $source_fingerprint,
  image_digest: $image_digest, expected_repaired_digest: $repaired_state_digest,
  evidence_digest: $evidence_digest, payload_digest: $payload_digest,
  state: 'available', payload_json: $rollback_payload_json, created_at: datetime()
})
CREATE (result:CrmDealRepairMutationResult {
  run_id: $run_id, unit_id: $unit_id, mutation_id: $mutation_id,
  generation: $generation, sequence: $sequence, attempt: $attempt,
  owner_id: $owner_id, fence_token: $fence_token,
  boundary_digest: $boundary_digest, unit_fingerprint: $unit_fingerprint,
  result_digest: $result_digest, rollback_image_id: $rollback_image_id,
  rollback_image_digest: $image_digest, evidence_digest: $evidence_digest,
  payload_digest: $payload_digest, outcome: $outcome,
  request_digest: $request_digest, repaired_state_digest: $repaired_state_digest,
  checkpoint_id: $checkpoint_id, outbox_event_id: $outbox_event_id,
  created_at: datetime()
})
CREATE (checkpoint:CrmDealRepairCheckpoint {
  run_id: $run_id, unit_id: $unit_id, checkpoint_id: $checkpoint_id,
  generation: $generation, sequence: $sequence, attempt: $attempt,
  owner_id: $owner_id, fence_token: $fence_token,
  boundary_digest: $boundary_digest, checkpoint_digest: $checkpoint_digest,
  evidence_digest: $evidence_digest, state: 'written', created_at: datetime()
})
CREATE (outbox:CrmDealRepairOutbox {
  run_id: $run_id, unit_id: $unit_id, event_id: $outbox_event_id,
  generation: $generation, sequence: $sequence, attempt: $attempt,
  owner_id: $owner_id, delivery_token: $fence_token,
  boundary_digest: $boundary_digest, payload_digest: $outbox_payload_digest,
  evidence_digest: $evidence_digest, state: 'pending',
  mutation_id: $mutation_id, created_at: datetime()
})
SET unit.state = $unit_state, unit.applied_mutation_id = $mutation_id,
    unit.updated_at = datetime()
CREATE (run)-[:HAS_REPAIR_ROLLBACK_IMAGE]->(image)
CREATE (run)-[:HAS_REPAIR_MUTATION]->(result)
CREATE (unit)-[:HAS_REPAIR_CHECKPOINT]->(checkpoint)
CREATE (unit)-[:HAS_REPAIR_OUTBOX]->(outbox)
RETURN result.mutation_id AS mutation_id
"""

VERIFY_REPAIRED_MUTATION_POSTCONDITIONS = """
MATCH (new:SourceRecord {source_record_pk: $new_source_record_pk})
CALL {
  WITH new
  OPTIONAL MATCH (new)-[link:LINKED_TO]->(:Person)
  RETURN count(CASE WHEN coalesce(link.is_active, true) THEN 1 END) AS active_links,
    count(CASE WHEN link.is_active = false AND link.provisional = true THEN 1 END)
      AS provisional_links,
    count(CASE WHEN coalesce(link.is_active, true)
      AND coalesce(link.authoritative, true) THEN 1 END) AS authoritative_links
}
CALL {
  WITH new
  OPTIONAL MATCH (:Person)-[projection:IDENTIFIED_BY|LIVES_AT|HAS_FACT]->()
  WHERE projection.source_record_pk = new.source_record_pk
    AND coalesce(projection.is_active, true) = true
  RETURN count(projection) AS active_person_evidence
}
CALL {
  WITH new
  OPTIONAL MATCH (new)-[projection:DESCRIBES_ADDRESS]->()
  WHERE coalesce(projection.is_active, true) = true
  RETURN count(projection) AS active_source_evidence
}
RETURN new.lifecycle_status AS lifecycle_status, active_links, provisional_links,
       authoritative_links, active_person_evidence + active_source_evidence AS active_evidence
"""
