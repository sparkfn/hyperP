"""Cypher query constants for the profile unifier Neo4j graph.

Rules followed:
    - HAS_FACT goes Person -> SourceRecord
    - IDENTIFIED_BY carries source_system_key and source_record_pk on the rel
    - Golden profile fields live directly on the Person node
    - preferred_address_id is resolved to a full Address at read time
    - Reads use parameterised queries; writes belong to route modules and
      MUST use session.execute_write with explicit transactions.
"""

from __future__ import annotations

# --- Person lookup ---

FIND_PERSON_BY_IDENTIFIER = """
MATCH (id:Identifier {identifier_type: $identifier_type, normalized_value: $value})
  <-[:IDENTIFIED_BY]-(p:Person)
WHERE p.status <> 'merged'
OPTIONAL MATCH (addr:Address {address_id: p.preferred_address_id})
RETURN p {
  .person_id, .status, .is_high_value, .is_high_risk,
  .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob,
  .profile_completeness_score, .golden_profile_computed_at, .golden_profile_version,
  .created_at, .updated_at
} AS person,
addr {
  .address_id, .unit_number, .street_number, .street_name,
  .city, .postal_code, .country_code, .normalized_full
} AS preferred_address
ORDER BY p.updated_at DESC
"""

GET_PERSON_BY_ID = """
MATCH (p:Person {person_id: $person_id})
OPTIONAL MATCH (p)-[:MERGED_INTO]->(canonical:Person)
WITH coalesce(canonical, p) AS person
OPTIONAL MATCH (addr:Address {address_id: person.preferred_address_id})
OPTIONAL MATCH (sr:SourceRecord)-[:LINKED_TO]->(person)
WITH person, addr, count(sr) AS source_record_count
RETURN person {
  .person_id, .status, .is_high_value, .is_high_risk,
  .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob,
  .profile_completeness_score, .golden_profile_computed_at, .golden_profile_version,
  .created_at, .updated_at
} AS person,
addr {
  .address_id, .unit_number, .street_number, .street_name,
  .city, .postal_code, .country_code, .normalized_full
} AS preferred_address,
source_record_count
"""

GET_PERSON_SOURCE_RECORDS = """
MATCH (sr:SourceRecord)-[:LINKED_TO]->(p:Person {person_id: $person_id})
MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
RETURN sr {
  .source_record_pk, .source_record_id, .source_record_version,
  .record_type, .extraction_confidence,
  .link_status, .observed_at, .ingested_at
} AS source_record,
ss.source_key AS source_system,
p.person_id AS linked_person_id
ORDER BY sr.observed_at DESC
SKIP $skip LIMIT $limit
"""

GET_PERSON_CONNECTIONS_IDENTIFIER = """
MATCH (p:Person {person_id: $person_id})-[:IDENTIFIED_BY]->(id:Identifier)
  <-[:IDENTIFIED_BY]-(other:Person)
WHERE other.person_id <> p.person_id
  AND other.status <> 'merged'
  AND ($identifier_type IS NULL OR id.identifier_type = $identifier_type)
WITH other, collect(DISTINCT {identifier_type: id.identifier_type, normalized_value: id.normalized_value}) AS shared_identifiers
RETURN other.person_id AS person_id,
       other.status AS status,
       other.preferred_full_name AS preferred_full_name,
       1 AS hops,
       shared_identifiers,
       [] AS shared_addresses
ORDER BY other.preferred_full_name
SKIP $skip LIMIT $limit
"""

GET_PERSON_CONNECTIONS_ADDRESS = """
MATCH (p:Person {person_id: $person_id})-[:LIVES_AT]->(addr:Address)
  <-[:LIVES_AT]-(other:Person)
WHERE other.person_id <> p.person_id
  AND other.status <> 'merged'
WITH other, collect(DISTINCT {address_id: addr.address_id, normalized_full: addr.normalized_full}) AS shared_addresses
RETURN other.person_id AS person_id,
       other.status AS status,
       other.preferred_full_name AS preferred_full_name,
       1 AS hops,
       [] AS shared_identifiers,
       shared_addresses
ORDER BY other.preferred_full_name
SKIP $skip LIMIT $limit
"""

GET_PERSON_CONNECTIONS_ALL = """
MATCH (p:Person {person_id: $person_id})
OPTIONAL MATCH (p)-[:IDENTIFIED_BY]->(id:Identifier)<-[:IDENTIFIED_BY]-(oi:Person)
  WHERE oi.person_id <> p.person_id AND oi.status <> 'merged'
    AND ($identifier_type IS NULL OR id.identifier_type = $identifier_type)
OPTIONAL MATCH (p)-[:LIVES_AT]->(addr:Address)<-[:LIVES_AT]-(oa:Person)
  WHERE oa.person_id <> p.person_id AND oa.status <> 'merged'
WITH p,
  collect(DISTINCT CASE WHEN oi IS NOT NULL THEN {person_id: oi.person_id, status: oi.status, preferred_full_name: oi.preferred_full_name, identifier_type: id.identifier_type, normalized_value: id.normalized_value} END) AS id_links,
  collect(DISTINCT CASE WHEN oa IS NOT NULL THEN {person_id: oa.person_id, status: oa.status, preferred_full_name: oa.preferred_full_name, address_id: addr.address_id, normalized_full: addr.normalized_full} END) AS addr_links
UNWIND (id_links + addr_links) AS link
WITH link WHERE link IS NOT NULL
WITH link.person_id AS person_id,
     link.status AS status,
     link.preferred_full_name AS preferred_full_name,
     collect(DISTINCT CASE WHEN link.identifier_type IS NOT NULL THEN {identifier_type: link.identifier_type, normalized_value: link.normalized_value} END) AS shared_identifiers_raw,
     collect(DISTINCT CASE WHEN link.address_id IS NOT NULL THEN {address_id: link.address_id, normalized_full: link.normalized_full} END) AS shared_addresses_raw
RETURN person_id, status, preferred_full_name, 1 AS hops,
       [x IN shared_identifiers_raw WHERE x IS NOT NULL] AS shared_identifiers,
       [x IN shared_addresses_raw WHERE x IS NOT NULL] AS shared_addresses
ORDER BY preferred_full_name
SKIP $skip LIMIT $limit
"""

SEARCH_PERSONS = """
CALL db.index.fulltext.queryNodes('person_name_search', $query) YIELD node AS p, score
WHERE p.status <> 'merged'
  AND ($status IS NULL OR p.status = $status)
OPTIONAL MATCH (addr:Address {address_id: p.preferred_address_id})
RETURN p {
  .person_id, .status, .is_high_value, .is_high_risk,
  .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob,
  .profile_completeness_score, .golden_profile_computed_at, .golden_profile_version,
  .created_at, .updated_at
} AS person,
addr {
  .address_id, .unit_number, .street_number, .street_name,
  .city, .postal_code, .country_code, .normalized_full
} AS preferred_address,
score
ORDER BY score DESC
SKIP $skip LIMIT $limit
"""

GET_PERSON_AUDIT = """
MATCH (me:MergeEvent)
WHERE (me)-[:ABSORBED]->(:Person {person_id: $person_id})
   OR (me)-[:SURVIVOR]->(:Person {person_id: $person_id})
OPTIONAL MATCH (me)-[:ABSORBED]->(absorbed:Person)
OPTIONAL MATCH (me)-[:SURVIVOR]->(survivor:Person)
OPTIONAL MATCH (me)-[:TRIGGERED_BY]->(md:MatchDecision)
RETURN me {
  .merge_event_id, .event_type, .actor_type, .actor_id,
  .reason, .metadata, .created_at
} AS merge_event,
absorbed.person_id AS absorbed_person_id,
survivor.person_id AS survivor_person_id,
md.match_decision_id AS triggered_by_decision_id
ORDER BY me.created_at DESC
SKIP $skip LIMIT $limit
"""

GET_PERSON_MATCHES = """
MATCH (md:MatchDecision)
WHERE (md)-[:ABOUT_LEFT]->(:Person {person_id: $person_id})
   OR (md)-[:ABOUT_RIGHT]->(:Person {person_id: $person_id})
OPTIONAL MATCH (md)-[:ABOUT_LEFT]->(left)
OPTIONAL MATCH (md)-[:ABOUT_RIGHT]->(right)
RETURN md {
  .match_decision_id, .engine_type, .engine_version, .policy_version,
  .decision, .confidence, .reasons, .blocking_conflicts, .created_at
} AS match_decision,
left.person_id AS left_person_id,
right.person_id AS right_person_id
ORDER BY md.created_at DESC
SKIP $skip LIMIT $limit
"""

# --- Review cases ---

LIST_REVIEW_CASES = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
WHERE ($queue_state IS NULL OR rc.queue_state = $queue_state)
  AND ($assigned_to IS NULL OR rc.assigned_to = $assigned_to)
  AND ($priority_lte IS NULL OR rc.priority <= $priority_lte)
RETURN rc {
  .review_case_id, .queue_state, .priority, .assigned_to,
  .follow_up_at, .sla_due_at, .resolution, .resolved_at,
  .actions, .created_at, .updated_at
} AS review_case,
md {
  .match_decision_id, .engine_type, .engine_version, .policy_version,
  .decision, .confidence, .reasons, .blocking_conflicts, .created_at
} AS match_decision
ORDER BY rc.priority, rc.sla_due_at, rc.created_at
SKIP $skip LIMIT $limit
"""

GET_REVIEW_CASE = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
OPTIONAL MATCH (md)-[:ABOUT_LEFT]->(left)
OPTIONAL MATCH (md)-[:ABOUT_RIGHT]->(right)
OPTIONAL MATCH (left_addr:Address) WHERE left:Person AND left_addr.address_id = left.preferred_address_id
OPTIONAL MATCH (right_addr:Address) WHERE right:Person AND right_addr.address_id = right.preferred_address_id
RETURN rc {
  .review_case_id, .queue_state, .priority, .assigned_to,
  .follow_up_at, .sla_due_at, .resolution, .resolved_at,
  .actions, .created_at, .updated_at
} AS review_case,
md {
  .match_decision_id, .engine_type, .engine_version, .policy_version,
  .decision, .confidence, .reasons, .blocking_conflicts, .created_at
} AS match_decision,
left { .person_id, .status, .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob } AS left_entity,
left_addr { .address_id, .unit_number, .street_number, .street_name, .city, .postal_code, .country_code, .normalized_full } AS left_address,
right { .person_id, .status, .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob } AS right_entity,
right_addr { .address_id, .unit_number, .street_number, .street_name, .city, .postal_code, .country_code, .normalized_full } AS right_address
"""

ASSIGN_REVIEW_CASE = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})
WHERE rc.queue_state IN ['open', 'assigned']
SET rc.assigned_to = $assigned_to,
    rc.queue_state = 'assigned',
    rc.updated_at = datetime(),
    rc.actions = rc.actions + [{
      action_type: 'assign',
      actor_type: 'system',
      actor_id: $assigned_to,
      notes: null,
      created_at: toString(datetime())
    }]
RETURN rc {
  .review_case_id, .queue_state, .assigned_to, .priority,
  .follow_up_at, .sla_due_at, .updated_at
} AS review_case
"""

CREATE_NO_MATCH_LOCK_FROM_REVIEW = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT]->(left:Person)
MATCH (md)-[:ABOUT_RIGHT]->(right:Person)
WITH left, right,
     CASE WHEN left.person_id < right.person_id THEN left ELSE right END AS a,
     CASE WHEN left.person_id < right.person_id THEN right ELSE left END AS b
CREATE (a)-[:NO_MATCH_LOCK {
  lock_id: randomUUID(),
  lock_type: 'manual_no_match',
  reason: $notes,
  actor_type: 'reviewer',
  actor_id: 'current_user',
  expires_at: null,
  created_at: datetime()
}]->(b)
"""

# --- Merge / unmerge / lock ---

CHECK_NO_MATCH_LOCK = """
MATCH (a:Person {person_id: $left})-[lock:NO_MATCH_LOCK]-(b:Person {person_id: $right})
WHERE lock.lock_type = 'manual_no_match'
  AND (lock.expires_at IS NULL OR lock.expires_at > datetime())
RETURN count(lock) > 0 AS is_locked
"""

CHECK_BOTH_PERSONS_ACTIVE = """
MATCH (absorbed:Person {person_id: $from_id, status: 'active'})
MATCH (survivor:Person {person_id: $to_id, status: 'active'})
RETURN absorbed, survivor
"""

EXECUTE_MANUAL_MERGE = """
MATCH (absorbed:Person {person_id: $from_id})
MATCH (survivor:Person {person_id: $to_id})

CREATE (me:MergeEvent {
  merge_event_id: randomUUID(),
  event_type: 'manual_merge',
  actor_type: 'admin',
  actor_id: 'current_user',
  reason: $reason,
  metadata: {},
  created_at: datetime()
})
CREATE (me)-[:ABSORBED]->(absorbed)
CREATE (me)-[:SURVIVOR]->(survivor)

WITH absorbed, survivor, me
OPTIONAL MATCH (sr:SourceRecord)-[old_link:LINKED_TO]->(absorbed)
FOREACH (_ IN CASE WHEN old_link IS NOT NULL THEN [1] ELSE [] END |
  DELETE old_link
  CREATE (sr)-[:LINKED_TO {linked_at: datetime()}]->(survivor)
  CREATE (me)-[:AFFECTED_RECORD]->(sr)
)

WITH absorbed, survivor, me
OPTIONAL MATCH (absorbed)-[old_id:IDENTIFIED_BY]->(id:Identifier)
FOREACH (_ IN CASE WHEN old_id IS NOT NULL THEN [1] ELSE [] END |
  CREATE (survivor)-[:IDENTIFIED_BY {
    is_verified: old_id.is_verified,
    verification_method: old_id.verification_method,
    is_active: old_id.is_active,
    quality_flag: old_id.quality_flag,
    first_seen_at: old_id.first_seen_at,
    last_seen_at: old_id.last_seen_at,
    last_confirmed_at: old_id.last_confirmed_at,
    source_system_key: old_id.source_system_key,
    source_record_pk: old_id.source_record_pk
  }]->(id)
  DELETE old_id
)

WITH absorbed, survivor, me
OPTIONAL MATCH (absorbed)-[old_addr:LIVES_AT]->(addr:Address)
FOREACH (_ IN CASE WHEN old_addr IS NOT NULL THEN [1] ELSE [] END |
  CREATE (survivor)-[:LIVES_AT {
    is_active: old_addr.is_active,
    is_verified: old_addr.is_verified,
    source_system_key: old_addr.source_system_key,
    source_record_pk: old_addr.source_record_pk,
    first_seen_at: old_addr.first_seen_at,
    last_seen_at: old_addr.last_seen_at,
    last_confirmed_at: old_addr.last_confirmed_at,
    quality_flag: old_addr.quality_flag
  }]->(addr)
  DELETE old_addr
)

WITH absorbed, survivor, me
OPTIONAL MATCH (absorbed)-[old_k_out:KNOWS]->(k_other:Person)
WHERE k_other.person_id <> survivor.person_id
FOREACH (_ IN CASE WHEN old_k_out IS NOT NULL THEN [1] ELSE [] END |
  CREATE (survivor)-[:KNOWS {
    knows_id: old_k_out.knows_id,
    relationship_label: old_k_out.relationship_label,
    relationship_category: old_k_out.relationship_category,
    source_system_key: old_k_out.source_system_key,
    source_record_pk: old_k_out.source_record_pk,
    declared_by_person_id: survivor.person_id,
    status: old_k_out.status,
    approved_at: old_k_out.approved_at,
    first_seen_at: old_k_out.first_seen_at,
    last_seen_at: old_k_out.last_seen_at,
    last_confirmed_at: old_k_out.last_confirmed_at,
    created_at: old_k_out.created_at,
    updated_at: datetime()
  }]->(k_other)
  DELETE old_k_out
)

WITH absorbed, survivor, me
OPTIONAL MATCH (k_other2:Person)-[old_k_in:KNOWS]->(absorbed)
WHERE k_other2.person_id <> survivor.person_id
FOREACH (_ IN CASE WHEN old_k_in IS NOT NULL THEN [1] ELSE [] END |
  CREATE (k_other2)-[:KNOWS {
    knows_id: old_k_in.knows_id,
    relationship_label: old_k_in.relationship_label,
    relationship_category: old_k_in.relationship_category,
    source_system_key: old_k_in.source_system_key,
    source_record_pk: old_k_in.source_record_pk,
    declared_by_person_id: old_k_in.declared_by_person_id,
    status: old_k_in.status,
    approved_at: old_k_in.approved_at,
    first_seen_at: old_k_in.first_seen_at,
    last_seen_at: old_k_in.last_seen_at,
    last_confirmed_at: old_k_in.last_confirmed_at,
    created_at: old_k_in.created_at,
    updated_at: datetime()
  }]->(survivor)
  DELETE old_k_in
)

WITH absorbed, survivor, me
OPTIONAL MATCH (absorbed)-[old_fact:HAS_FACT]->(sr_fact:SourceRecord)
FOREACH (_ IN CASE WHEN old_fact IS NOT NULL THEN [1] ELSE [] END |
  CREATE (survivor)-[:HAS_FACT {
    attribute_name: old_fact.attribute_name,
    attribute_value: old_fact.attribute_value,
    source_trust_tier: old_fact.source_trust_tier,
    confidence: old_fact.confidence,
    quality_flag: old_fact.quality_flag,
    is_current_hint: old_fact.is_current_hint,
    observed_at: old_fact.observed_at,
    created_at: old_fact.created_at
  }]->(sr_fact)
  DELETE old_fact
)

WITH absorbed, survivor, me
SET absorbed.status = 'merged', absorbed.updated_at = datetime()
CREATE (absorbed)-[:MERGED_INTO {
  merge_event_id: me.merge_event_id,
  actor: 'current_user',
  timestamp: datetime()
}]->(survivor)

WITH absorbed, survivor, me
OPTIONAL MATCH (prev:Person)-[old_merge:MERGED_INTO]->(absorbed)
FOREACH (_ IN CASE WHEN old_merge IS NOT NULL THEN [1] ELSE [] END |
  CREATE (prev)-[:MERGED_INTO {
    merge_event_id: old_merge.merge_event_id,
    actor: old_merge.actor,
    timestamp: old_merge.timestamp
  }]->(survivor)
  DELETE old_merge
)

WITH survivor, me
SET survivor.updated_at = datetime()
RETURN me.merge_event_id AS merge_event_id
"""

GET_UNMERGE_TARGET = """
MATCH (me:MergeEvent {merge_event_id: $merge_event_id})
MATCH (me)-[:ABSORBED]->(absorbed:Person)
MATCH (me)-[:SURVIVOR]->(survivor:Person)
WHERE absorbed.status = 'merged'
RETURN absorbed.person_id AS absorbed_id, survivor.person_id AS survivor_id
"""

REVERT_MERGE = """
MATCH (absorbed:Person {person_id: $absorbed_id})-[mi:MERGED_INTO]->(survivor:Person {person_id: $survivor_id})
DELETE mi
SET absorbed.status = 'active', absorbed.updated_at = datetime()
"""

CREATE_UNMERGE_AUDIT = """
MATCH (absorbed:Person {person_id: $absorbed_id})
MATCH (survivor:Person {person_id: $survivor_id})
CREATE (ume:MergeEvent {
  merge_event_id: randomUUID(),
  event_type: 'unmerge',
  actor_type: 'admin',
  actor_id: 'current_user',
  reason: $reason,
  metadata: {original_merge_event_id: $original_merge_event_id},
  created_at: datetime()
})
CREATE (ume)-[:ABSORBED]->(absorbed)
CREATE (ume)-[:SURVIVOR]->(survivor)
"""

FLAG_AFFECTED_RECORDS_FOR_REVIEW = """
MATCH (me:MergeEvent {merge_event_id: $merge_event_id})-[:AFFECTED_RECORD]->(sr:SourceRecord)
SET sr.link_status = 'pending_review'
"""

CHECK_EXISTING_LOCK = """
MATCH (a:Person {person_id: $left})-[lock:NO_MATCH_LOCK]-(b:Person {person_id: $right})
WHERE lock.expires_at IS NULL OR lock.expires_at > datetime()
RETURN lock.lock_id AS lock_id
"""

CREATE_PERSON_PAIR_LOCK = """
MATCH (a:Person {person_id: $left})
MATCH (b:Person {person_id: $right})
CREATE (a)-[lock:NO_MATCH_LOCK {
  lock_id: randomUUID(),
  lock_type: $lock_type,
  reason: $reason,
  actor_type: 'admin',
  actor_id: 'current_user',
  expires_at: CASE WHEN $expires_at IS NOT NULL THEN datetime($expires_at) ELSE null END,
  created_at: datetime()
}]->(b)
RETURN lock.lock_id AS lock_id
"""

DELETE_LOCK = """
MATCH ()-[lock:NO_MATCH_LOCK {lock_id: $lock_id}]->()
DELETE lock
RETURN $lock_id AS deleted_lock_id
"""

# --- Survivorship ---

CHECK_PERSON_ACTIVE = """
MATCH (p:Person {person_id: $person_id, status: 'active'})
RETURN p.person_id AS person_id
"""

GET_PERSON_FACTS = """
MATCH (p:Person {person_id: $person_id})-[f:HAS_FACT]->(sr:SourceRecord)
MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
RETURN f.attribute_name AS attribute_name,
       f.attribute_value AS attribute_value,
       f.quality_flag AS quality_flag,
       f.confidence AS confidence,
       f.observed_at AS observed_at,
       sr.source_record_pk AS source_record_pk,
       ss.field_trust[f.attribute_name] AS trust_tier
ORDER BY attribute_name
"""

GET_PERSON_OVERRIDES = """
MATCH (p:Person {person_id: $person_id})
RETURN p.survivorship_overrides AS overrides
"""

GET_BEST_ADDRESS = """
MATCH (p:Person {person_id: $person_id})-[la:LIVES_AT]->(addr:Address)
WHERE la.is_active = true AND la.quality_flag IN ['valid', 'partial_parse']
MATCH (sr:SourceRecord {source_record_pk: la.source_record_pk})-[:FROM_SOURCE]->(ss:SourceSystem)
RETURN addr.address_id AS address_id,
       la.last_seen_at AS last_seen_at,
       ss.field_trust['address'] AS trust_tier
ORDER BY ss.field_trust['address'], la.last_seen_at DESC
LIMIT 1
"""

UPDATE_GOLDEN_PROFILE = """
MATCH (p:Person {person_id: $person_id})
SET p.preferred_full_name = $full_name,
    p.preferred_phone = $phone,
    p.preferred_email = $email,
    p.preferred_dob = $dob,
    p.preferred_address_id = $address_id,
    p.profile_completeness_score = $completeness,
    p.golden_profile_computed_at = datetime(),
    p.golden_profile_version = $version,
    p.updated_at = datetime()
"""

CREATE_RECOMPUTE_AUDIT = """
MATCH (p:Person {person_id: $person_id})
CREATE (me:MergeEvent {
  merge_event_id: randomUUID(),
  event_type: 'survivorship_override',
  actor_type: 'system',
  actor_id: 'golden_profile_recompute',
  reason: 'Golden profile recomputed',
  metadata: {},
  created_at: datetime()
})
CREATE (me)-[:SURVIVOR]->(p)
"""

GET_PERSON_OVERRIDES_FULL = """
MATCH (p:Person {person_id: $person_id, status: 'active'})
RETURN p.person_id AS person_id, p.survivorship_overrides AS overrides
"""

CHECK_SOURCE_RECORD_LINKED = """
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})-[:LINKED_TO]->(p:Person {person_id: $person_id})
RETURN sr.source_record_pk AS pk
"""

GET_FACT_VALUE = """
MATCH (p:Person {person_id: $person_id})-[f:HAS_FACT {attribute_name: $attribute_name}]->(sr:SourceRecord {source_record_pk: $source_record_pk})
RETURN f.attribute_value AS value
"""

UPDATE_OVERRIDES = """
MATCH (p:Person {person_id: $person_id})
SET p.survivorship_overrides = $overrides, p.updated_at = datetime()
"""

UPDATE_GOLDEN_FIELD = """
MATCH (p:Person {person_id: $person_id})
SET p[$field_name] = $value, p.updated_at = datetime()
"""

# --- Ingestion ---

CHECK_SOURCE_SYSTEM = """
MATCH (ss:SourceSystem {source_key: $source_key, is_active: true})
RETURN ss.source_system_id AS id
"""

CREATE_INGEST_RUN_INLINE = """
MATCH (ss:SourceSystem {source_key: $source_key})
CREATE (ir:IngestRun {
  ingest_run_id: randomUUID(),
  run_type: $ingest_type,
  status: 'started',
  started_at: datetime(),
  record_count: 0,
  rejected_count: 0,
  metadata: {}
})
CREATE (ir)-[:FROM_SOURCE]->(ss)
RETURN ir.ingest_run_id AS ingest_run_id
"""

CREATE_SOURCE_RECORD = """
MATCH (ss:SourceSystem {source_key: $source_key})
MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id})
CREATE (sr:SourceRecord {
  source_record_pk: randomUUID(),
  source_record_id: $source_record_id,
  source_record_version: $source_record_version,
  record_type: $record_type,
  extraction_confidence: $extraction_confidence,
  extraction_method: $extraction_method,
  conversation_ref: $conversation_ref,
  link_status: 'pending_review',
  observed_at: datetime($observed_at),
  ingested_at: datetime(),
  record_hash: $record_hash,
  raw_payload: $raw_payload,
  normalized_payload: $attributes,
  metadata: {},
  retention_expires_at: null
})
CREATE (sr)-[:FROM_SOURCE]->(ss)
CREATE (sr)-[:PART_OF_RUN]->(ir)
"""

UPDATE_INGEST_RUN_COUNTERS = """
MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id})
SET ir.record_count = ir.record_count + $accepted,
    ir.rejected_count = ir.rejected_count + $rejected
"""

CREATE_INGEST_RUN = """
MATCH (ss:SourceSystem {source_key: $source_key, is_active: true})
CREATE (ir:IngestRun {
  ingest_run_id: randomUUID(),
  run_type: $run_type,
  status: 'started',
  started_at: datetime(),
  finished_at: null,
  record_count: 0,
  rejected_count: 0,
  metadata: $metadata
})
CREATE (ir)-[:FROM_SOURCE]->(ss)
RETURN ir.ingest_run_id AS ingest_run_id,
       ir.status AS status,
       toString(ir.started_at) AS started_at
"""

UPDATE_INGEST_RUN = """
MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id})-[:FROM_SOURCE]->(ss:SourceSystem {source_key: $source_key})
SET ir.status = $status,
    ir.finished_at = CASE WHEN $finished_at IS NOT NULL THEN datetime($finished_at) ELSE ir.finished_at END,
    ir.metadata = CASE WHEN $metadata IS NOT NULL THEN $metadata ELSE ir.metadata END
RETURN ir.ingest_run_id AS ingest_run_id,
       ir.status AS status,
       toString(ir.finished_at) AS finished_at
"""

GET_INGEST_RUN = """
MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id})
OPTIONAL MATCH (ir)-[:FROM_SOURCE]->(ss:SourceSystem)
RETURN ir {
  .ingest_run_id, .run_type, .status,
  .record_count, .rejected_count, .metadata
} AS run,
toString(ir.started_at) AS started_at,
toString(ir.finished_at) AS finished_at,
ss.source_key AS source_key
"""

# --- Admin ---

LIST_SOURCE_SYSTEMS = """
MATCH (ss:SourceSystem)
RETURN ss {
  .source_system_id, .source_key, .display_name,
  .system_type, .is_active, .field_trust,
  .created_at, .updated_at
} AS source_system
ORDER BY ss.source_key
"""

GET_FIELD_TRUST = """
MATCH (ss:SourceSystem {source_key: $source_key})
RETURN ss.field_trust AS field_trust,
       ss.source_key AS source_key,
       ss.display_name AS display_name
"""

UPDATE_FIELD_TRUST = """
MATCH (ss:SourceSystem {source_key: $source_key})
SET ss.field_trust = $field_trust,
    ss.updated_at = datetime()
"""

# --- Events ---

LIST_EVENTS = """
MATCH (me:MergeEvent)
WHERE me.created_at >= datetime($since)
  AND ($event_type IS NULL OR me.event_type = $event_type)
OPTIONAL MATCH (me)-[:ABSORBED]->(absorbed:Person)
OPTIONAL MATCH (me)-[:SURVIVOR]->(survivor:Person)
WITH me, collect(DISTINCT absorbed.person_id) + collect(DISTINCT survivor.person_id) AS pids
WITH me, [x IN pids WHERE x IS NOT NULL] AS affected_person_ids
RETURN me.merge_event_id AS event_id,
       CASE me.event_type
         WHEN 'auto_merge' THEN 'person_merged'
         WHEN 'manual_merge' THEN 'person_merged'
         WHEN 'unmerge' THEN 'person_unmerged'
         WHEN 'person_created' THEN 'person_created'
         WHEN 'survivorship_override' THEN 'golden_profile_updated'
         WHEN 'review_reject' THEN 'review_case_resolved'
         WHEN 'manual_no_match' THEN 'review_case_resolved'
         ELSE me.event_type
       END AS event_type,
       affected_person_ids,
       me.metadata AS metadata,
       toString(me.created_at) AS created_at
ORDER BY me.created_at ASC
SKIP $skip LIMIT $limit
"""
