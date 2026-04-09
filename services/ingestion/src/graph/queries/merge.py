"""Cypher constants for MergeEvent creation, relationship rewires, and path compression."""

from __future__ import annotations

CREATE_MERGE_EVENT_PERSON_CREATED = """
MATCH (p:Person {person_id: $person_id})
CREATE (me:MergeEvent {
    merge_event_id:      randomUUID(),
    event_type:          'person_created',
    actor_type:          'system',
    actor_id:            'ingestion_pipeline',
    reason:              'New person created — no matching candidates found',
    metadata:            '{}',
    created_at:          datetime(),
    retention_expires_at: null
})-[:SURVIVOR]->(p)
RETURN me.merge_event_id AS merge_event_id
"""

CREATE_MERGE_EVENT_AUTO_MERGE = """
MATCH (from_p:Person {person_id: $from_person_id})
MATCH (to_p:Person {person_id: $to_person_id})
CREATE (me:MergeEvent {
    merge_event_id: randomUUID(),
    event_type: 'auto_merge',
    actor_type: 'system',
    actor_id: 'match_engine',
    reason: $reason,
    metadata: '{}',
    created_at: datetime(),
    retention_expires_at: null
})
CREATE (me)-[:ABSORBED]->(from_p)
CREATE (me)-[:SURVIVOR]->(to_p)
RETURN me.merge_event_id AS merge_event_id
"""

# --- Relationship rewires (absorbed → survivor) ---------------------------

REWIRE_LINKED_TO = """
MATCH (sr:SourceRecord)-[old:LINKED_TO]->(absorbed:Person {person_id: $absorbed_id})
DELETE old
WITH sr
MATCH (survivor:Person {person_id: $survivor_id})
CREATE (sr)-[:LINKED_TO {linked_at: datetime()}]->(survivor)
RETURN count(sr) AS rewired_count
"""

REWIRE_IDENTIFIED_BY = """
MATCH (absorbed:Person {person_id: $absorbed_id})-[old:IDENTIFIED_BY]->(id:Identifier)
WITH absorbed, old, id, properties(old) AS props
DELETE old
WITH id, props
MATCH (survivor:Person {person_id: $survivor_id})
MERGE (survivor)-[rel:IDENTIFIED_BY]->(id)
ON CREATE SET
    rel.is_verified = props.is_verified,
    rel.verification_method = props.verification_method,
    rel.is_active = props.is_active,
    rel.quality_flag = props.quality_flag,
    rel.first_seen_at = props.first_seen_at,
    rel.last_seen_at = props.last_seen_at,
    rel.last_confirmed_at = props.last_confirmed_at,
    rel.source_system_key = props.source_system_key,
    rel.source_record_pk = props.source_record_pk
ON MATCH SET
    rel.last_seen_at = datetime(),
    rel.last_confirmed_at = datetime()
RETURN count(id) AS rewired_count
"""

REWIRE_LIVES_AT = """
MATCH (absorbed:Person {person_id: $absorbed_id})-[old:LIVES_AT]->(addr:Address)
WITH absorbed, old, addr, properties(old) AS props
DELETE old
WITH addr, props
MATCH (survivor:Person {person_id: $survivor_id})
MERGE (survivor)-[rel:LIVES_AT]->(addr)
ON CREATE SET
    rel.is_active = props.is_active,
    rel.is_verified = props.is_verified,
    rel.quality_flag = props.quality_flag,
    rel.source_system_key = props.source_system_key,
    rel.source_record_pk = props.source_record_pk,
    rel.first_seen_at = props.first_seen_at,
    rel.last_seen_at = props.last_seen_at,
    rel.last_confirmed_at = props.last_confirmed_at
ON MATCH SET
    rel.last_seen_at = datetime(),
    rel.last_confirmed_at = datetime()
RETURN count(addr) AS rewired_count
"""

REWIRE_HAS_FACT = """
MATCH (absorbed:Person {person_id: $absorbed_id})-[old:HAS_FACT]->(sr:SourceRecord)
WITH absorbed, old, sr, properties(old) AS props
DELETE old
WITH sr, props
MATCH (survivor:Person {person_id: $survivor_id})
CREATE (survivor)-[:HAS_FACT {
    attribute_name: props.attribute_name,
    attribute_value: props.attribute_value,
    source_trust_tier: props.source_trust_tier,
    confidence: props.confidence,
    quality_flag: props.quality_flag,
    is_current_hint: props.is_current_hint,
    observed_at: props.observed_at,
    created_at: props.created_at
}]->(sr)
RETURN count(sr) AS rewired_count
"""

# --- Mark absorbed + lineage compression ----------------------------------

MARK_PERSON_MERGED = """
MATCH (absorbed:Person {person_id: $absorbed_id})
SET absorbed.status = 'merged',
    absorbed.updated_at = datetime()
"""

CREATE_MERGED_INTO = """
MATCH (absorbed:Person {person_id: $absorbed_id})
MATCH (survivor:Person {person_id: $survivor_id})
CREATE (absorbed)-[:MERGED_INTO {
    merge_event_id: $merge_event_id,
    actor: 'match_engine',
    timestamp: datetime()
}]->(survivor)
"""

PATH_COMPRESS_MERGED_INTO = """
MATCH (prev:Person)-[old:MERGED_INTO]->(absorbed:Person {person_id: $absorbed_id})
WITH prev, old, properties(old) AS props
DELETE old
WITH prev, props
MATCH (survivor:Person {person_id: $survivor_id})
CREATE (prev)-[:MERGED_INTO {
    merge_event_id: props.merge_event_id,
    actor: props.actor,
    timestamp: props.timestamp
}]->(survivor)
RETURN count(prev) AS compressed_count
"""

# --- Audit links ----------------------------------------------------------

GET_AFFECTED_SOURCE_RECORDS = """
MATCH (sr:SourceRecord)-[:LINKED_TO]->(p:Person {person_id: $person_id})
RETURN sr.source_record_pk AS source_record_pk
"""

LINK_MERGE_EVENT_TRIGGERED_BY = """
MATCH (me:MergeEvent {merge_event_id: $merge_event_id})
MATCH (md:MatchDecision {match_decision_id: $match_decision_id})
CREATE (me)-[:TRIGGERED_BY]->(md)
"""

LINK_MERGE_EVENT_AFFECTED_RECORD = """
MATCH (me:MergeEvent {merge_event_id: $merge_event_id})
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
CREATE (me)-[:AFFECTED_RECORD]->(sr)
"""
