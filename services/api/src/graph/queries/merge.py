"""Cypher constants for manual merge / unmerge / pair-lock operations."""

from __future__ import annotations

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
