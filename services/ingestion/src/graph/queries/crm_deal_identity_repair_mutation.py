"""Parameterized queries for one atomic CRM-deal repair mutation."""

from __future__ import annotations

LOCK_REPAIR_MUTATION_UNIT = """
MATCH (unit:CrmDealRepairUnit {
  run_id: $run_id, unit_id: $unit_id, generation: $generation, sequence: $sequence,
  attempt: $attempt, boundary_digest: $boundary_digest,
  inventory_fingerprint: $unit_fingerprint, inventory_key: $inventory_key,
  source_record_pk: $source_record_pk,
  inventory_graph_fingerprint: $inventory_graph_fingerprint,
  inventory_stored_payload_fingerprint: $inventory_stored_payload_fingerprint,
  inventory_binding_digest: $inventory_binding_digest
})
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
WITH result, collect(image) AS images
OPTIONAL MATCH (checkpoint:CrmDealRepairCheckpoint {
  run_id: result.run_id, checkpoint_id: result.checkpoint_id
})
WITH result, images, collect(checkpoint) AS checkpoints
OPTIONAL MATCH (outbox:CrmDealRepairOutbox {
  run_id: result.run_id, event_id: result.outbox_event_id
})
WITH result, images, checkpoints, collect(outbox) AS outboxes
OPTIONAL MATCH (source:SourceRecord {repair_mutation_id: result.mutation_id})
WITH result, images, checkpoints, outboxes, collect(source) AS sources
RETURN properties(result) AS result,
       CASE WHEN size(images) = 1 THEN properties(images[0]) ELSE null END AS image,
       CASE WHEN size(checkpoints) = 1 THEN properties(checkpoints[0]) ELSE null END AS checkpoint,
       CASE WHEN size(outboxes) = 1 THEN properties(outboxes[0]) ELSE null END AS outbox,
       CASE WHEN size(sources) = 1 THEN sources[0].source_record_pk ELSE null END
         AS committed_source_record_pk,
       size(images) AS image_count, size(checkpoints) AS checkpoint_count,
       size(outboxes) AS outbox_count, size(sources) AS source_count
"""

LOCK_AND_ASSERT_REPAIR_MUTATION_GUARD = """
MATCH (run:CrmDealRepairRun {
  run_id: $run_id, boundary_digest: $boundary_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  status: 'qualified', execution_allowed: false
})
WHERE run.source_record_pks_json CONTAINS $quoted_source_record_pk
MATCH (unit:CrmDealRepairUnit {
  run_id: $run_id, unit_id: $unit_id, generation: $generation, sequence: $sequence,
  attempt: $attempt, boundary_digest: $boundary_digest,
  inventory_fingerprint: $unit_fingerprint, mutation_lock_id: $mutation_id,
  inventory_key: $inventory_key,
  source_record_pk: $source_record_pk,
  inventory_graph_fingerprint: $inventory_graph_fingerprint,
  inventory_stored_payload_fingerprint: $inventory_stored_payload_fingerprint,
  inventory_binding_digest: $inventory_binding_digest
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

LOCK_AND_ASSERT_REPAIR_MUTATION_FINAL_GUARD = """
MATCH (run:CrmDealRepairRun {
  run_id: $run_id, boundary_digest: $boundary_digest,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  status: 'qualified', execution_allowed: false
})
WHERE run.source_record_pks_json CONTAINS $quoted_source_record_pk
MATCH (unit:CrmDealRepairUnit {
  run_id: $run_id, unit_id: $unit_id, generation: $generation, sequence: $sequence,
  attempt: $attempt, boundary_digest: $boundary_digest,
  inventory_fingerprint: $unit_fingerprint, mutation_lock_id: $mutation_id,
  inventory_key: $inventory_key, source_record_pk: $source_record_pk,
  inventory_graph_fingerprint: $inventory_graph_fingerprint,
  inventory_stored_payload_fingerprint: $inventory_stored_payload_fingerprint,
  inventory_binding_digest: $inventory_binding_digest
})
WHERE unit.state IN ['allocated', 'quiesced']
MATCH (:CrmDealRepairFence {
  run_id: $run_id, unit_id: $unit_id, fence_id: $fence_id,
  generation: $generation, sequence: $sequence, attempt: $attempt,
  owner_id: $owner_id, token: $fence_token, boundary_digest: $boundary_digest, state: 'claimed'
})
MATCH (:BitrixDispatchControl {
  source_key: 'bitrix_chat', control_instance_id: $control_instance_id, blocked: true
})
MATCH (source_instance:BitrixSourceInstance {
  source_key: 'bitrix_chat', source_instance_id: $source_instance_id, status: 'active'
})-[:OWNS_BITRIX_CONTROL]->(:BitrixExecutionSourceBinding {
  source_key: 'bitrix_chat', source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id
})
MATCH (:BitrixSourceInstance {
  source_key: 'bitrix_chat', source_instance_id: $control_instance_id, status: 'active'
})
MATCH (old:SourceRecord {source_record_pk: $source_record_pk, source_record_id: $source_record_id,
  source_instance_id: $source_instance_id, lifecycle_status: 'superseded', is_latest: false})
MATCH (new:SourceRecord {source_record_pk: $new_source_record_pk, repair_mutation_id: $mutation_id,
  lifecycle_status: $new_lifecycle_status, is_latest: true})
RETURN old.source_record_pk AS old_source_record_pk, new.source_record_pk AS new_source_record_pk
"""


READ_REPAIR_IDENTIFIER_PREEXISTENCE = """
UNWIND $identifiers AS item
OPTIONAL MATCH (identifier:Identifier {
  identifier_type: item.identifier_type, identifier_scope: item.identifier_scope,
  normalized_value: item.normalized_value
})
RETURN item.identifier_type AS identifier_type, item.identifier_scope AS identifier_scope,
       item.normalized_value AS normalized_value, identifier IS NOT NULL AS preexisting
ORDER BY identifier_type, identifier_scope, normalized_value
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
  // Property maps cannot be stringified by Neo4j.  Return every relationship
  // and let the typed rollback codec canonical-sort the complete maps in Python.
  // Element ids only make the stream deterministic inside this read; the codec
  // preserves duplicate rows with an ordinal based on canonical content.
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


READ_REPAIRED_OWNER_IDS = """
MATCH (:SourceRecord {source_record_pk: $source_record_pk})-[link:LINKED_TO]->(person:Person)
WHERE coalesce(link.is_active, true) = true AND coalesce(link.authoritative, true) = true
RETURN collect(person.person_id) AS owner_ids
"""

READ_LOCKED_REPAIR_AUTHORITY = """
MATCH (deal:SourceRecord {source_record_pk: $source_record_pk})
MATCH (source_instance:BitrixSourceInstance {
  source_key: 'bitrix_chat', source_instance_id: $source_instance_id, status: 'active'
})-[:OWNS_BITRIX_CONTROL]->(binding:BitrixExecutionSourceBinding {
  source_key: 'bitrix_chat', source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id
})
MATCH (:BitrixSourceInstance {
  source_key: 'bitrix_chat', source_instance_id: $control_instance_id, status: 'active'
})
MATCH (person:Person)
WHERE person.person_id IN $owner_ids
SET deal.source_record_pk = deal.source_record_pk, person.person_id = person.person_id
WITH deal, person, binding
CALL {
  WITH deal, person, binding
  OPTIONAL MATCH (support:SourceRecord)-[support_link:LINKED_TO]->(person)
  WHERE coalesce(support_link.is_active, true) = true
    AND support.source_record_pk <> deal.source_record_pk
    AND support.source_instance_id = $source_instance_id
    AND support.source_entity_type IN ['contact', 'lead']
    AND support.record_type <> 'crm_deal'
    AND (support.lifecycle_status = 'active'
      OR (support.lifecycle_status IS NULL AND support.is_latest = true))
    AND support.identity_policy_version IN ['crm_contact_identity_v1', 'crm_lead_identity_v1']
    AND support.standalone_crm_available_at IS NOT NULL
    AND support.standalone_crm_census_id IS NOT NULL
    AND support.standalone_crm_stream_kind IS NOT NULL
    AND support.standalone_crm_generation IS NOT NULL
    AND support.standalone_crm_fence_token IS NOT NULL
    AND support.standalone_crm_fence_owner_id IS NOT NULL
    AND support.standalone_crm_task_name IS NOT NULL
    AND support.standalone_crm_task_id IS NOT NULL
    AND support.standalone_crm_payload_digest IS NOT NULL
    AND support.standalone_crm_call_intent_id IS NOT NULL
    AND support.standalone_crm_authorization_id IS NOT NULL
    AND support.standalone_crm_authorization_digest IS NOT NULL
    AND support.standalone_crm_availability_contract_version IS NOT NULL
    AND support.standalone_crm_frozen_upper_id IS NOT NULL
    AND support.standalone_crm_control_instance_id = $control_instance_id
  MATCH (:StandaloneCrmCensus {
    census_id: support.standalone_crm_census_id,
    generation: support.standalone_crm_generation,
    source_key: 'bitrix_chat', source_instance_id: support.source_instance_id
  })
  MATCH (:StandaloneCrmCensusFence {
    census_id: support.standalone_crm_census_id,
    generation: support.standalone_crm_generation,
    stream_kind: support.standalone_crm_stream_kind,
    token: support.standalone_crm_fence_token,
    owner_id: support.standalone_crm_fence_owner_id
  })
  MATCH (:StandaloneCrmChildPublication {
    census_id: support.standalone_crm_census_id,
    generation: support.standalone_crm_generation,
    stream_kind: support.standalone_crm_stream_kind,
    task_name: support.standalone_crm_task_name,
    task_id: support.standalone_crm_task_id,
    payload_digest: support.standalone_crm_payload_digest, status: 'published'
  })
  MATCH (:StandaloneCrmHttpCallReservation {
    intent_id: support.standalone_crm_call_intent_id,
    census_id: support.standalone_crm_census_id,
    generation: support.standalone_crm_generation,
    stream_kind: support.standalone_crm_stream_kind,
    fence_token: support.standalone_crm_fence_token,
    task_id: support.standalone_crm_task_id, status: 'succeeded'
  })
  MATCH (:StandaloneCrmSourceFactPageReceipt {
    status: 'committed', census_id: support.standalone_crm_census_id,
    generation: support.standalone_crm_generation,
    stream_kind: support.standalone_crm_stream_kind,
    fence_token: support.standalone_crm_fence_token,
    fence_owner_id: support.standalone_crm_fence_owner_id,
    source_key: 'bitrix_chat', source_instance_id: support.source_instance_id,
    control_instance_id: $control_instance_id,
    task_name: support.standalone_crm_task_name, task_id: support.standalone_crm_task_id,
    payload_digest: support.standalone_crm_payload_digest,
    call_intent_id: support.standalone_crm_call_intent_id,
    authorization_id: support.standalone_crm_authorization_id,
    authorization_digest: support.standalone_crm_authorization_digest,
    available_at: support.standalone_crm_available_at,
    availability_contract_version: support.standalone_crm_availability_contract_version,
    frozen_upper_id: support.standalone_crm_frozen_upper_id
  })
  OPTIONAL MATCH (person)-[identifier_link:IDENTIFIED_BY]->(identifier:Identifier)
  WHERE identifier_link.source_record_pk = support.source_record_pk
    AND coalesce(identifier_link.is_active, true) = true
    AND identifier.identifier_type IN ['crm_contact_id', 'crm_lead_id']
    AND identifier.source_instance_id = $source_instance_id
    AND identifier.identifier_scope = support.source_instance_id
    AND identifier.normalized_value = support.source_entity_id
    AND ((support.source_entity_type = 'contact'
          AND support.identity_policy_version = 'crm_contact_identity_v1'
          AND identifier.identifier_type = 'crm_contact_id')
      OR (support.source_entity_type = 'lead'
          AND support.identity_policy_version = 'crm_lead_identity_v1'
          AND identifier.identifier_type = 'crm_lead_id'))
  RETURN collect(CASE WHEN support IS NULL OR identifier IS NULL THEN NULL ELSE {
    source_record_pk: support.source_record_pk, source_record_id: support.source_record_id,
    source_instance_id: support.source_instance_id, record_type: support.record_type,
    source_entity_type: support.source_entity_type,
    source_entity_id: support.source_entity_id,
    identity_policy_version: support.identity_policy_version,
    lifecycle_status: support.lifecycle_status,
    identifier_type: identifier.identifier_type,
    identifier_scope: identifier.identifier_scope,
    identifier_source_instance_id: identifier.source_instance_id,
    identifier_link_source_record_pk: identifier_link.source_record_pk,
    standalone_crm_available_at: toString(support.standalone_crm_available_at),
    standalone_crm_census_id: support.standalone_crm_census_id,
    standalone_crm_stream_kind: support.standalone_crm_stream_kind,
    standalone_crm_generation: support.standalone_crm_generation,
    standalone_crm_fence_token: support.standalone_crm_fence_token,
    standalone_crm_fence_owner_id: support.standalone_crm_fence_owner_id,
    standalone_crm_task_name: support.standalone_crm_task_name,
    standalone_crm_task_id: support.standalone_crm_task_id,
    standalone_crm_payload_digest: support.standalone_crm_payload_digest,
    standalone_crm_call_intent_id: support.standalone_crm_call_intent_id,
    standalone_crm_authorization_id: support.standalone_crm_authorization_id,
    standalone_crm_authorization_digest: support.standalone_crm_authorization_digest,
    standalone_crm_availability_contract_version:
      support.standalone_crm_availability_contract_version,
    standalone_crm_frozen_upper_id: support.standalone_crm_frozen_upper_id
    ,control_instance_id: binding.control_instance_id
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
    source_repair_mutation_id: reviewed.repair_mutation_id,
    match_decision_id: decision.match_decision_id,
    decision_repair_mutation_id: decision.repair_mutation_id,
    review_case_id: review.review_case_id,
    review_repair_mutation_id: review.repair_mutation_id,
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
  OPTIONAL MATCH (self_support:SourceRecord)-[:CHILD_OF*1..2]->(deal)
  OPTIONAL MATCH (self_support)-[self_link:LINKED_TO]->(person)
  WHERE coalesce(self_link.is_active, true) = true
  // The descendant can remain bound when its link is inactive.  Only an
  // actually matched active link is self-supporting authority.
  RETURN collect(CASE WHEN self_link IS NULL THEN NULL ELSE {
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
SET lock.locked_at = datetime(), lock.repair_mutation_id = $mutation_id,
    support.source_record_pk = support.source_record_pk
WITH support_row, support
CALL {
  WITH support_row, support
  WITH support_row, support WHERE support_row.provenance_class = 'independent_trusted'
  MATCH (support)-[support_link:LINKED_TO]->(person:Person {
    person_id: support_row.person_id
  })
  MATCH (person)-[identifier_link:IDENTIFIED_BY]->(identifier:Identifier {
    identifier_type: support_row.identifier_type,
    identifier_scope: support_row.identifier_scope,
    source_instance_id: support_row.identifier_source_instance_id,
    normalized_value: support_row.source_entity_id
  })
  WHERE coalesce(support_link.is_active, true) = true
    AND coalesce(identifier_link.is_active, true) = true
    AND identifier_link.source_record_pk = support.source_record_pk
  MATCH (census:StandaloneCrmCensus {
    census_id: support.standalone_crm_census_id,
    generation: support.standalone_crm_generation,
    source_key: 'bitrix_chat', source_instance_id: support.source_instance_id
  })
  MATCH (fence:StandaloneCrmCensusFence {
    census_id: support.standalone_crm_census_id,
    generation: support.standalone_crm_generation,
    stream_kind: support.standalone_crm_stream_kind,
    token: support.standalone_crm_fence_token,
    owner_id: support.standalone_crm_fence_owner_id
  })
  MATCH (publication:StandaloneCrmChildPublication {
    census_id: support.standalone_crm_census_id,
    generation: support.standalone_crm_generation,
    stream_kind: support.standalone_crm_stream_kind,
    task_name: support.standalone_crm_task_name,
    task_id: support.standalone_crm_task_id,
    payload_digest: support.standalone_crm_payload_digest, status: 'published'
  })
  MATCH (reservation:StandaloneCrmHttpCallReservation {
    intent_id: support.standalone_crm_call_intent_id,
    census_id: support.standalone_crm_census_id,
    generation: support.standalone_crm_generation,
    stream_kind: support.standalone_crm_stream_kind,
    fence_token: support.standalone_crm_fence_token,
    task_id: support.standalone_crm_task_id, status: 'succeeded'
  })
  MATCH (receipt:StandaloneCrmSourceFactPageReceipt {
    status: 'committed', census_id: support.standalone_crm_census_id,
    generation: support.standalone_crm_generation,
    stream_kind: support.standalone_crm_stream_kind,
    fence_token: support.standalone_crm_fence_token,
    fence_owner_id: support.standalone_crm_fence_owner_id,
    source_key: 'bitrix_chat', source_instance_id: support.source_instance_id,
    control_instance_id: support.standalone_crm_control_instance_id,
    task_name: support.standalone_crm_task_name, task_id: support.standalone_crm_task_id,
    payload_digest: support.standalone_crm_payload_digest,
    call_intent_id: support.standalone_crm_call_intent_id,
    authorization_id: support.standalone_crm_authorization_id,
    authorization_digest: support.standalone_crm_authorization_digest,
    available_at: support.standalone_crm_available_at,
    availability_contract_version: support.standalone_crm_availability_contract_version,
    frozen_upper_id: support.standalone_crm_frozen_upper_id
  })
  SET person.person_id = person.person_id,
      identifier.identifier_type = identifier.identifier_type,
      census.census_id = census.census_id,
      fence.token = fence.token,
      publication.task_id = publication.task_id,
      reservation.intent_id = reservation.intent_id,
      receipt.status = receipt.status
  RETURN count(*) AS independent_chain_count, 0 AS reviewed_chain_count
  UNION
  WITH support_row, support
  WITH support_row, support WHERE support_row.provenance_class = 'reviewed_v2'
  MATCH (decision:MatchDecision {match_decision_id: support_row.match_decision_id})
        -[:ABOUT_LEFT {entity_type: 'source_record'}]->(support)
  MATCH (decision)-[:ABOUT_RIGHT {entity_type: 'person'}]->(person:Person {
    person_id: support_row.person_id
  })
  MATCH (review:ReviewCase {review_case_id: support_row.review_case_id})
        -[:FOR_DECISION]->(decision)
  WHERE decision.policy_version = 'crm_deal_identity_v2'
    AND decision.decision = 'merge'
    AND review.resolution = support_row.resolution
  SET decision.match_decision_id = decision.match_decision_id,
      person.person_id = person.person_id,
      review.review_case_id = review.review_case_id
  RETURN 0 AS independent_chain_count, count(*) AS reviewed_chain_count
}
RETURN count(DISTINCT support) AS locked_count,
       sum(independent_chain_count) AS independent_chain_count,
       sum(reviewed_chain_count) AS reviewed_chain_count
"""

CREATE_REPAIRED_SOURCE_RECORD = """
MATCH (source:SourceSystem {source_key: 'bitrix_chat'})
MATCH (old:SourceRecord {source_record_pk: $old_source_record_pk})
WHERE (old.lifecycle_status = 'active'
  OR (old.lifecycle_status IS NULL AND old.is_latest = true))
  AND old.entity_key = $entity_key
OPTIONAL MATCH (old)-[:OWNED_BY]->(entity:Entity {entity_key: $entity_key})
OPTIONAL MATCH (conflict:SourceRecord {source_version_key: $source_version_key})
WITH source, old, entity, conflict
WHERE conflict IS NULL OR conflict.source_record_pk = $new_source_record_pk
MERGE (new:SourceRecord {source_record_pk: $new_source_record_pk})
ON CREATE SET new = old {
  .*,
  source_record_pk: $new_source_record_pk,
  source_record_id: $source_record_id,
  source_instance_id: $source_instance_id,
  source_record_version: $source_record_version,
  source_version_key: $source_version_key,
  entity_key: $entity_key,
  expected_active_source_record_pk: $old_source_record_pk,
  lifecycle_status: 'pending_review',
  is_latest: false,
  link_status: $link_status,
  record_type: 'crm_deal',
  observed_at: datetime($observed_at),
  ingested_at: datetime(),
  record_hash: $record_hash,
  raw_payload: $raw_payload,
  normalized_payload: $normalized_payload,
  source_entity_type: 'deal',
  source_entity_id: $deal_id,
  identity_policy_version: 'crm_deal_identity_v2',
  identity_link_key: $identity_link_key,
  repair_mutation_id: $mutation_id,
  retention_expires_at: old.retention_expires_at,
  crm_deal_stage_id: coalesce(old.crm_deal_stage_id, old.stage_id),
  extraction_confidence: old.extraction_confidence,
  extraction_method: old.extraction_method,
  conversation_ref: old.conversation_ref,
  parent_source_system: old.parent_source_system,
  parent_source_instance_id: old.parent_source_instance_id,
  parent_source_record_id: old.parent_source_record_id,
  parent_record_type: old.parent_record_type
}
MERGE (new)-[from_source:FROM_SOURCE]->(source)
ON CREATE SET from_source.repair_mutation_id = $mutation_id
MERGE (old)-[previous:PREVIOUS_VERSION_OF]->(new)
ON CREATE SET previous.repair_mutation_id = $mutation_id
FOREACH (_ IN CASE WHEN entity IS NULL THEN [] ELSE [1] END |
  MERGE (new)-[owned:OWNED_BY]->(entity)
  ON CREATE SET owned.repair_mutation_id = $mutation_id
)
RETURN new.source_record_pk AS source_record_pk
"""

CREATE_UNRECONSTRUCTABLE_REVIEW_SOURCE_RECORD = """
MATCH (source:SourceSystem {source_key: 'bitrix_chat'})
MATCH (old:SourceRecord {source_record_pk: $old_source_record_pk})-[:FROM_SOURCE]->(source)
WHERE old.lifecycle_status = 'active' OR (old.lifecycle_status IS NULL AND old.is_latest = true)
OPTIONAL MATCH (old)-[:OWNED_BY]->(entity:Entity)
MERGE (new:SourceRecord {source_record_pk: $new_source_record_pk})
ON CREATE SET new = old {
  .*,
  source_record_pk: $new_source_record_pk,
  source_record_id: old.source_record_id,
  source_instance_id: old.source_instance_id,
  source_record_version: $source_record_version,
  source_version_key: $source_version_key,
  entity_key: old.entity_key,
  expected_active_source_record_pk: old.source_record_pk,
  lifecycle_status: 'pending_review',
  is_latest: false,
  link_status: 'pending_review',
  record_type: old.record_type,
  observed_at: old.observed_at,
  ingested_at: datetime(),
  record_hash: old.record_hash,
  raw_payload: old.raw_payload,
  normalized_payload: old.normalized_payload,
  repair_mutation_id: $mutation_id,
  repair_reconstruction_status: 'unreconstructable_review_only'
}
MERGE (old)-[previous:PREVIOUS_VERSION_OF]->(new)
ON CREATE SET previous.repair_mutation_id = $mutation_id
MERGE (new)-[from_source:FROM_SOURCE]->(source)
ON CREATE SET from_source.repair_mutation_id = $mutation_id
FOREACH (_ IN CASE WHEN entity IS NULL THEN [] ELSE [1] END |
  MERGE (new)-[owned:OWNED_BY]->(entity)
  ON CREATE SET owned.repair_mutation_id = $mutation_id
)
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
CREATE (decision)-[:ABOUT_LEFT {entity_type: 'source_record', repair_mutation_id: $mutation_id}]->(source)
RETURN decision.match_decision_id AS match_decision_id
"""

STAGE_ACTIVE_REPAIR_LINK = """
MATCH (source:SourceRecord {source_record_pk: $new_source_record_pk})
MATCH (decision:MatchDecision {match_decision_id: $match_decision_id})
MATCH (person:Person {person_id: $person_id})
CREATE (decision)-[:ABOUT_RIGHT {entity_type: 'person', repair_mutation_id: $mutation_id}]->(person)
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
CREATE (decision)-[:ABOUT_RIGHT {entity_type: 'person', repair_mutation_id: $mutation_id}]->(person)
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
  assigned_to: null, follow_up_at: null,
  sla_due_at: CASE WHEN $sla_due_at IS NULL THEN null ELSE datetime($sla_due_at) END,
  resolution: null, resolved_at: null, actions: [], created_at: datetime(),
  updated_at: datetime(), repair_mutation_id: $mutation_id
})-[:FOR_DECISION {repair_mutation_id: $mutation_id}]->(decision)
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
  identifier.created_at = datetime(), identifier.repair_mutation_id = $mutation_id
MERGE (person)-[link:IDENTIFIED_BY {
  source_system_key: 'bitrix_chat', source_record_pk: $source_record_pk
}]->(identifier)
ON CREATE SET link.is_verified = identifier_row.is_verified,
  link.verification_method = null, link.is_active = true,
  link.quality_flag = identifier_row.quality_flag,
  link.first_seen_at = datetime(), link.last_seen_at = datetime(),
  link.last_confirmed_at = datetime(), link.repair_mutation_id = $mutation_id
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
  is_current_hint: false, observed_at: datetime($observed_at), created_at: datetime(),
  repair_mutation_id: $mutation_id
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
  new_source_record_pk: $new_source_record_pk,
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
WITH new, new.repair_mutation_id AS mutation_id
CALL {
  WITH mutation_id
  MATCH (node)
  WHERE node.repair_mutation_id = mutation_id
    AND (node:SourceRecord OR node:MatchDecision OR node:ReviewCase OR node:Identifier)
  RETURN collect({
    object_kind: CASE
      WHEN node:SourceRecord THEN 'SourceRecord'
      WHEN node:MatchDecision THEN 'MatchDecision'
      WHEN node:ReviewCase THEN 'ReviewCase'
      ELSE 'Identifier'
    END,
    identity: CASE
      WHEN node:SourceRecord THEN {source_record_pk: node.source_record_pk}
      WHEN node:MatchDecision THEN {match_decision_id: node.match_decision_id}
      WHEN node:ReviewCase THEN {review_case_id: node.review_case_id}
      ELSE {
        identifier_type: node.identifier_type,
        identifier_scope: node.identifier_scope,
        normalized_value: node.normalized_value
      }
    END,
    properties: properties(node)
  }) AS nodes
}
CALL {
  WITH mutation_id
  MATCH (left)-[relationship]->(right)
  WHERE relationship.repair_mutation_id = mutation_id
  RETURN collect({
    object_kind: type(relationship),
    direction: 'outgoing',
    left_endpoint: CASE
      WHEN left:SourceRecord THEN {source_record_pk: left.source_record_pk}
      WHEN left:MatchDecision THEN {match_decision_id: left.match_decision_id}
      WHEN left:ReviewCase THEN {review_case_id: left.review_case_id}
      WHEN left:Person THEN {person_id: left.person_id}
      WHEN left:Identifier THEN {
        identifier_type: left.identifier_type,
        identifier_scope: left.identifier_scope,
        normalized_value: left.normalized_value
      }
      WHEN left:SourceSystem THEN {source_key: left.source_key}
      ELSE {entity_key: left.entity_key}
    END,
    right_endpoint: CASE
      WHEN right:SourceRecord THEN {source_record_pk: right.source_record_pk}
      WHEN right:MatchDecision THEN {match_decision_id: right.match_decision_id}
      WHEN right:ReviewCase THEN {review_case_id: right.review_case_id}
      WHEN right:Person THEN {person_id: right.person_id}
      WHEN right:Identifier THEN {
        identifier_type: right.identifier_type,
        identifier_scope: right.identifier_scope,
        normalized_value: right.normalized_value
      }
      WHEN right:SourceSystem THEN {source_key: right.source_key}
      ELSE {entity_key: right.entity_key}
    END,
    properties: properties(relationship)
  }) AS relationships
}
RETURN nodes, relationships
"""
