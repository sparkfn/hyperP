"""Cypher constants for MergeEvent creation and relationship rewires."""

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
MATCH (survivor:Person {person_id: $survivor_id})
MATCH (me:MergeEvent {merge_event_id: $merge_event_id})
WITH sr, absorbed, survivor, me, old, properties(old) AS props
CREATE (me)-[moved:MOVED_RELATIONSHIP]->(sr)
SET moved = props,
    moved.relationship_type = 'LINKED_TO',
    moved.direction = 'incoming',
    moved.origin_person_id = coalesce(props.merge_origin_person_id, $absorbed_id),
    moved.created_on_survivor = true
DELETE old
CREATE (sr)-[rel:LINKED_TO]->(survivor)
SET rel = props,
    rel.linked_at = datetime(),
    rel.merge_origin_person_id = moved.origin_person_id
RETURN count(sr) AS rewired_count
"""

REWIRE_IDENTIFIED_BY = """
MATCH (absorbed:Person {person_id: $absorbed_id})-[old:IDENTIFIED_BY]->(id:Identifier)
MATCH (survivor:Person {person_id: $survivor_id})
MATCH (me:MergeEvent {merge_event_id: $merge_event_id})
OPTIONAL MATCH (survivor)-[existing:IDENTIFIED_BY {
    source_system_key: old.source_system_key,
    source_record_pk: old.source_record_pk
}]->(id)
WITH absorbed, old, id, survivor, me, existing, properties(old) AS props
CREATE (me)-[moved:MOVED_RELATIONSHIP]->(id)
SET moved = props,
    moved.relationship_type = 'IDENTIFIED_BY',
    moved.direction = 'outgoing',
    moved.origin_person_id = coalesce(props.merge_origin_person_id, $absorbed_id),
    moved.created_on_survivor = existing IS NULL
MERGE (survivor)-[rel:IDENTIFIED_BY {
    source_system_key: props.source_system_key,
    source_record_pk: props.source_record_pk
}]->(id)
ON CREATE SET rel = props,
              rel.merge_origin_person_id = moved.origin_person_id
ON MATCH SET
    rel.last_seen_at = datetime(),
    rel.last_confirmed_at = datetime()
DELETE old
RETURN count(id) AS rewired_count
"""

REWIRE_LIVES_AT = """
MATCH (absorbed:Person {person_id: $absorbed_id})-[old:LIVES_AT]->(addr:Address)
MATCH (survivor:Person {person_id: $survivor_id})
MATCH (me:MergeEvent {merge_event_id: $merge_event_id})
OPTIONAL MATCH (survivor)-[existing:LIVES_AT {
    source_system_key: old.source_system_key,
    source_record_pk: old.source_record_pk
}]->(addr)
WITH absorbed, old, addr, survivor, me, existing, properties(old) AS props
CREATE (me)-[moved:MOVED_RELATIONSHIP]->(addr)
SET moved = props,
    moved.relationship_type = 'LIVES_AT',
    moved.direction = 'outgoing',
    moved.origin_person_id = coalesce(props.merge_origin_person_id, $absorbed_id),
    moved.created_on_survivor = existing IS NULL
MERGE (survivor)-[rel:LIVES_AT {
    source_system_key: props.source_system_key,
    source_record_pk: props.source_record_pk
}]->(addr)
ON CREATE SET rel = props,
              rel.merge_origin_person_id = moved.origin_person_id
ON MATCH SET
    rel.last_seen_at = datetime(),
    rel.last_confirmed_at = datetime()
DELETE old
RETURN count(addr) AS rewired_count
"""

REWIRE_HAS_FACT = """
MATCH (absorbed:Person {person_id: $absorbed_id})-[old:HAS_FACT]->(sr:SourceRecord)
MATCH (survivor:Person {person_id: $survivor_id})
MATCH (me:MergeEvent {merge_event_id: $merge_event_id})
WITH absorbed, old, sr, survivor, me, properties(old) AS props
CREATE (me)-[moved:MOVED_RELATIONSHIP]->(sr)
SET moved = props,
    moved.relationship_type = 'HAS_FACT',
    moved.direction = 'outgoing',
    moved.origin_person_id = coalesce(props.merge_origin_person_id, $absorbed_id),
    moved.created_on_survivor = true
DELETE old
CREATE (survivor)-[rel:HAS_FACT]->(sr)
SET rel = props,
    rel.merge_origin_person_id = moved.origin_person_id
RETURN count(sr) AS rewired_count
"""

# --- Mark absorbed + preserve event lineage -------------------------------

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
MATCH (survivor:Person {person_id: $survivor_id})
MATCH (me:MergeEvent {merge_event_id: $merge_event_id})
WITH prev, old, absorbed, survivor, me, properties(old) AS props
CREATE (me)-[moved_lineage:MOVED_MERGE_LINEAGE]->(prev)
SET moved_lineage = props,
    moved_lineage.prior_survivor_person_id = absorbed.person_id,
    moved_lineage.compressed_survivor_person_id = survivor.person_id
DELETE old
CREATE (prev)-[compressed:MERGED_INTO]->(survivor)
SET compressed = props
RETURN count(prev) AS compressed_count
"""

# --- Audit links ----------------------------------------------------------

GET_AFFECTED_SOURCE_RECORDS = """
MATCH (person:Person {person_id: $person_id})
CALL (person) {
  MATCH (sr:SourceRecord)-[:LINKED_TO]->(person)
  RETURN sr.source_record_pk AS source_record_pk
  UNION
  MATCH (person)-[projection]->()
  WHERE projection.source_record_pk IS NOT NULL
  RETURN projection.source_record_pk AS source_record_pk
  UNION
  MATCH ()-[projection]->(person)
  WHERE projection.source_record_pk IS NOT NULL
  RETURN projection.source_record_pk AS source_record_pk
}
RETURN DISTINCT source_record_pk
ORDER BY source_record_pk
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
