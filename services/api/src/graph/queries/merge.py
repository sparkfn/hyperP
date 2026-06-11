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
  actor_id: $actor_id,
  reason: $reason,
  metadata: '{}',
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

WITH DISTINCT absorbed, survivor, me
OPTIONAL MATCH (absorbed)-[old_id:IDENTIFIED_BY]->(id:Identifier)
OPTIONAL MATCH (id_sr:SourceRecord {source_record_pk: old_id.source_record_pk})
WITH absorbed, survivor, me, id, id_sr, old_id, properties(old_id) AS old_id_props
FOREACH (_ IN CASE WHEN old_id IS NOT NULL THEN [1] ELSE [] END |
  CREATE (survivor)-[new_id:IDENTIFIED_BY]->(id)
  SET new_id = old_id_props
  DELETE old_id
)
FOREACH (_ IN CASE WHEN id_sr IS NOT NULL THEN [1] ELSE [] END |
  MERGE (me)-[:AFFECTED_RECORD]->(id_sr)
)

WITH DISTINCT absorbed, survivor, me
OPTIONAL MATCH (absorbed)-[old_addr:LIVES_AT]->(addr:Address)
OPTIONAL MATCH (addr_sr:SourceRecord {source_record_pk: old_addr.source_record_pk})
WITH absorbed, survivor, me, addr, addr_sr, old_addr, properties(old_addr) AS old_addr_props
FOREACH (_ IN CASE WHEN old_addr IS NOT NULL THEN [1] ELSE [] END |
  CREATE (survivor)-[new_addr:LIVES_AT]->(addr)
  SET new_addr = old_addr_props
  DELETE old_addr
)
FOREACH (_ IN CASE WHEN addr_sr IS NOT NULL THEN [1] ELSE [] END |
  MERGE (me)-[:AFFECTED_RECORD]->(addr_sr)
)

WITH DISTINCT absorbed, survivor, me
OPTIONAL MATCH (absorbed)-[old_k_out:KNOWS]->(k_other:Person)
WHERE k_other.person_id <> survivor.person_id
WITH absorbed, survivor, me, k_other, old_k_out, properties(old_k_out) AS old_k_out_props
FOREACH (_ IN CASE WHEN old_k_out IS NOT NULL THEN [1] ELSE [] END |
  CREATE (survivor)-[new_k_out:KNOWS]->(k_other)
  SET new_k_out = old_k_out_props
  SET new_k_out.declared_by_person_id = survivor.person_id,
      new_k_out.updated_at = datetime()
  DELETE old_k_out
)

WITH DISTINCT absorbed, survivor, me
OPTIONAL MATCH (k_other2:Person)-[old_k_in:KNOWS]->(absorbed)
WHERE k_other2.person_id <> survivor.person_id
WITH absorbed, survivor, me, k_other2, old_k_in, properties(old_k_in) AS old_k_in_props
FOREACH (_ IN CASE WHEN old_k_in IS NOT NULL THEN [1] ELSE [] END |
  CREATE (k_other2)-[new_k_in:KNOWS]->(survivor)
  SET new_k_in = old_k_in_props
  SET new_k_in.updated_at = datetime()
  DELETE old_k_in
)

WITH DISTINCT absorbed, survivor, me
OPTIONAL MATCH (absorbed)-[old_fact:HAS_FACT]->(sr_fact:SourceRecord)
WITH absorbed, survivor, me, sr_fact, old_fact, properties(old_fact) AS old_fact_props
FOREACH (_ IN CASE WHEN old_fact IS NOT NULL THEN [1] ELSE [] END |
  CREATE (survivor)-[new_fact:HAS_FACT]->(sr_fact)
  SET new_fact = old_fact_props
  DELETE old_fact
)
FOREACH (_ IN CASE WHEN sr_fact IS NOT NULL THEN [1] ELSE [] END |
  MERGE (me)-[:AFFECTED_RECORD]->(sr_fact)
)

WITH DISTINCT absorbed, survivor, me
SET absorbed.status = 'merged', absorbed.updated_at = datetime()
CREATE (absorbed)-[:MERGED_INTO {
  merge_event_id: me.merge_event_id,
  actor: $actor_id,
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
MATCH (absorbed:Person {person_id: $absorbed_id})-[mi:MERGED_INTO]->(merge_survivor:Person)
MATCH (merge_survivor)-[:MERGED_INTO*0..]->(current_survivor:Person)
WHERE NOT (current_survivor)-[:MERGED_INTO]->(:Person)
WITH absorbed, mi, current_survivor, current_survivor.person_id AS current_survivor_id
OPTIONAL MATCH (merge_event:MergeEvent {merge_event_id: mi.merge_event_id})-[:AFFECTED_RECORD]->(affected_sr:SourceRecord)
WITH absorbed, mi, current_survivor, current_survivor_id, collect(affected_sr.source_record_pk) AS affected_pks

OPTIONAL MATCH (sr:SourceRecord)-[survivor_link:LINKED_TO]->(current_survivor)
WHERE sr.source_record_pk IN affected_pks
FOREACH (_ IN CASE WHEN survivor_link IS NOT NULL THEN [1] ELSE [] END |
  DELETE survivor_link
  CREATE (sr)-[:LINKED_TO {linked_at: datetime()}]->(absorbed)
)

WITH DISTINCT absorbed, mi, current_survivor, current_survivor_id, affected_pks
OPTIONAL MATCH (current_survivor)-[survivor_id:IDENTIFIED_BY]->(id:Identifier)
WHERE survivor_id.source_record_pk IN affected_pks
FOREACH (_ IN CASE WHEN survivor_id IS NOT NULL THEN [1] ELSE [] END |
  CREATE (absorbed)-[:IDENTIFIED_BY {
    is_verified: survivor_id.is_verified,
    verification_method: survivor_id.verification_method,
    is_active: survivor_id.is_active,
    quality_flag: survivor_id.quality_flag,
    first_seen_at: survivor_id.first_seen_at,
    last_seen_at: survivor_id.last_seen_at,
    last_confirmed_at: survivor_id.last_confirmed_at,
    source_system_key: survivor_id.source_system_key,
    source_record_pk: survivor_id.source_record_pk
  }]->(id)
  DELETE survivor_id
)

WITH DISTINCT absorbed, mi, current_survivor, current_survivor_id, affected_pks
OPTIONAL MATCH (current_survivor)-[survivor_addr:LIVES_AT]->(addr:Address)
WHERE survivor_addr.source_record_pk IN affected_pks
FOREACH (_ IN CASE WHEN survivor_addr IS NOT NULL THEN [1] ELSE [] END |
  CREATE (absorbed)-[:LIVES_AT {
    is_active: survivor_addr.is_active,
    is_verified: survivor_addr.is_verified,
    source_system_key: survivor_addr.source_system_key,
    source_record_pk: survivor_addr.source_record_pk,
    first_seen_at: survivor_addr.first_seen_at,
    last_seen_at: survivor_addr.last_seen_at,
    last_confirmed_at: survivor_addr.last_confirmed_at,
    quality_flag: survivor_addr.quality_flag
  }]->(addr)
  DELETE survivor_addr
)

WITH DISTINCT absorbed, mi, current_survivor, current_survivor_id, affected_pks
OPTIONAL MATCH (current_survivor)-[survivor_fact:HAS_FACT]->(sr_fact:SourceRecord)
WHERE sr_fact.source_record_pk IN affected_pks
FOREACH (_ IN CASE WHEN survivor_fact IS NOT NULL THEN [1] ELSE [] END |
  CREATE (absorbed)-[:HAS_FACT {
    attribute_name: survivor_fact.attribute_name,
    attribute_value: survivor_fact.attribute_value,
    source_trust_tier: survivor_fact.source_trust_tier,
    confidence: survivor_fact.confidence,
    quality_flag: survivor_fact.quality_flag,
    is_current_hint: survivor_fact.is_current_hint,
    observed_at: survivor_fact.observed_at,
    created_at: survivor_fact.created_at
  }]->(sr_fact)
  DELETE survivor_fact
)

WITH DISTINCT absorbed, mi, current_survivor, current_survivor_id, affected_pks
OPTIONAL MATCH (current_survivor)-[survivor_k_out:KNOWS]->(k_other_out:Person)
WHERE survivor_k_out.source_record_pk IN affected_pks
  AND k_other_out.person_id <> absorbed.person_id
FOREACH (_ IN CASE WHEN survivor_k_out IS NOT NULL THEN [1] ELSE [] END |
  CREATE (absorbed)-[:KNOWS {
    knows_id: survivor_k_out.knows_id,
    relationship_label: survivor_k_out.relationship_label,
    relationship_category: survivor_k_out.relationship_category,
    source_system_key: survivor_k_out.source_system_key,
    source_record_pk: survivor_k_out.source_record_pk,
    declared_by_person_id: absorbed.person_id,
    status: survivor_k_out.status,
    approved_at: survivor_k_out.approved_at,
    first_seen_at: survivor_k_out.first_seen_at,
    last_seen_at: survivor_k_out.last_seen_at,
    last_confirmed_at: survivor_k_out.last_confirmed_at,
    created_at: survivor_k_out.created_at,
    updated_at: datetime()
  }]->(k_other_out)
  DELETE survivor_k_out
)

WITH DISTINCT absorbed, mi, current_survivor, current_survivor_id, affected_pks
OPTIONAL MATCH (k_other_in:Person)-[survivor_k_in:KNOWS]->(current_survivor)
WHERE survivor_k_in.source_record_pk IN affected_pks
  AND k_other_in.person_id <> absorbed.person_id
FOREACH (_ IN CASE WHEN survivor_k_in IS NOT NULL THEN [1] ELSE [] END |
  CREATE (k_other_in)-[:KNOWS {
    knows_id: survivor_k_in.knows_id,
    relationship_label: survivor_k_in.relationship_label,
    relationship_category: survivor_k_in.relationship_category,
    source_system_key: survivor_k_in.source_system_key,
    source_record_pk: survivor_k_in.source_record_pk,
    declared_by_person_id: survivor_k_in.declared_by_person_id,
    status: survivor_k_in.status,
    approved_at: survivor_k_in.approved_at,
    first_seen_at: survivor_k_in.first_seen_at,
    last_seen_at: survivor_k_in.last_seen_at,
    last_confirmed_at: survivor_k_in.last_confirmed_at,
    created_at: survivor_k_in.created_at,
    updated_at: datetime()
  }]->(absorbed)
  DELETE survivor_k_in
)

WITH DISTINCT absorbed, mi, current_survivor_id
DELETE mi
SET absorbed.status = 'active', absorbed.updated_at = datetime()
RETURN count(mi) AS removed_count, current_survivor_id
"""

CREATE_UNMERGE_AUDIT = """
MATCH (absorbed:Person {person_id: $absorbed_id})
MATCH (survivor:Person {person_id: $survivor_id})
CREATE (ume:MergeEvent {
  merge_event_id: randomUUID(),
  event_type: 'unmerge',
  actor_type: 'admin',
  actor_id: $actor_id,
  reason: $reason,
  metadata: $metadata,
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
  actor_id: $actor_id,
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

CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT {entity_type: 'person'}]->(a:Person)
MATCH (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(b:Person)
WHERE rc.queue_state IN ['open', 'assigned', 'deferred']
  AND (a.person_id = $absorbed_id OR b.person_id = $absorbed_id)
SET rc.queue_state = 'cancelled',
    rc.resolution = 'cancelled_superseded',
    rc.resolved_at = datetime(),
    rc.closed_by_merge_event_id = $merge_event_id,
    rc.updated_at = datetime()
"""

REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
WHERE rc.queue_state IN ['open', 'assigned', 'deferred']
MATCH (md)-[old_right:ABOUT_RIGHT {entity_type: 'person'}]->(absorbed:Person {person_id: $absorbed_id})
MATCH (md)-[:ABOUT_LEFT {entity_type: 'source_record'}]->(:SourceRecord)
MATCH (survivor:Person {person_id: $survivor_id})
CREATE (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(survivor)
DELETE old_right
SET rc.redirected_by_merge_event_id = $merge_event_id,
    rc.redirected_from_person_id = $absorbed_id,
    rc.updated_at = datetime()
"""

REVERT_RECORD_PERSON_CASE_REDIRECTS = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
WHERE rc.redirected_by_merge_event_id = $merge_event_id
  AND rc.queue_state IN ['open', 'assigned', 'deferred']
MATCH (md)-[cur_right:ABOUT_RIGHT {entity_type: 'person'}]->(:Person)
MATCH (absorbed:Person {person_id: rc.redirected_from_person_id})
CREATE (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(absorbed)
DELETE cur_right
SET rc.redirected_by_merge_event_id = null,
    rc.redirected_from_person_id = null,
    rc.updated_at = datetime()
"""

REVERT_PERSON_PAIR_CASE_CLOSURES = """
MATCH (rc:ReviewCase)
WHERE rc.closed_by_merge_event_id = $merge_event_id
  AND rc.queue_state = 'cancelled'
  AND rc.resolution = 'cancelled_superseded'
SET rc.queue_state = 'open',
    rc.resolution = null,
    rc.resolved_at = null,
    rc.closed_by_merge_event_id = null,
    rc.updated_at = datetime()
"""
