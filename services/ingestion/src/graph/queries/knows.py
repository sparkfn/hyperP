"""Cypher constants for the declared social Person↔Person ``KNOWS`` relationship.

``KNOWS`` mirrors the Fundbox ``contacts`` table (emergency contact /
next-of-kin / referrer). It is sourced from systems of record, never inferred
by the matching engine, and survives merges via the rewire queries below.
"""

from __future__ import annotations

#: Idempotent create of a KNOWS edge between two persons. Deduplicated by
#: (source_system_key, source_record_pk) so re-ingestion of the same source
#: row never produces duplicate edges.
LINK_PERSON_KNOWS = """
MATCH (current:SourceRecord {source_record_pk: $source_record_pk})
OPTIONAL MATCH (old:SourceRecord)-[:PREVIOUS_VERSION_OF]->(current)
OPTIONAL MATCH ()-[old_rel:KNOWS]->()
WHERE old_rel.source_record_pk = old.source_record_pk
WITH collect(old_rel) AS old_relationships
MATCH (declarer:Person {person_id: $declarer_person_id})
MATCH (contact:Person  {person_id: $contact_person_id})
FOREACH (old_rel IN old_relationships |
  SET old_rel.is_active = false,
      old_rel.retired_at = datetime(),
      old_rel.updated_at = datetime()
)
MERGE (declarer)-[rel:KNOWS {
    source_system_key: $source_system_key,
    source_record_pk:  $source_record_pk
}]->(contact)
ON CREATE SET
    rel.knows_id              = randomUUID(),
    rel.relationship_label    = $relationship_label,
    rel.relationship_category = $relationship_category,
    rel.declared_by_person_id = $declarer_person_id,
    rel.status                = $status,
    rel.approved_at           = $approved_at,
    rel.first_seen_at         = datetime(),
    rel.last_seen_at          = datetime(),
    rel.last_confirmed_at     = datetime(),
    rel.created_at            = datetime(),
    rel.updated_at            = datetime(),
    rel.is_active             = true,
    rel.activated_at          = datetime()
ON MATCH SET
    rel.relationship_label = $relationship_label,
    rel.status             = $status,
    rel.approved_at        = $approved_at,
    rel.last_seen_at       = datetime(),
    rel.last_confirmed_at  = datetime(),
    rel.updated_at         = datetime(),
    rel.is_active          = true,
    rel.activated_at       = coalesce(rel.activated_at, datetime()),
    rel.retired_at         = null
RETURN rel.knows_id AS knows_id
"""

#: Resolve a (declarer source_record_pk, contact source_record_pk) pair to
#: their currently linked Person ids. Used by the post-resolution KNOWS
#: materializer to walk pending links once both sides have a Person.
RESOLVE_KNOWS_ENDPOINTS = """
MATCH (declarer_sr:SourceRecord {source_record_pk: $declarer_source_record_pk})
      -[:LINKED_TO]->(declarer:Person {status: 'active'})
MATCH (contact_sr:SourceRecord  {source_record_pk: $contact_source_record_pk})
      -[:LINKED_TO]->(contact:Person  {status: 'active'})
RETURN declarer.person_id AS declarer_person_id,
       contact.person_id  AS contact_person_id
"""

#: Rewire KNOWS edges in both directions when ``absorbed`` is merged into
#: ``survivor``. Mirrors the rewire pattern for IDENTIFIED_BY / LIVES_AT.
REWIRE_KNOWS_OUT = """
MATCH (absorbed:Person {person_id: $absorbed_id})-[old:KNOWS]->(other:Person)
WHERE other.person_id <> $survivor_id
WITH old, other, properties(old) AS props
DELETE old
WITH other, props
MATCH (survivor:Person {person_id: $survivor_id})
MERGE (survivor)-[rel:KNOWS {
    source_system_key: props.source_system_key,
    source_record_pk:  props.source_record_pk
}]->(other)
ON CREATE SET rel += props, rel.declared_by_person_id = $survivor_id
RETURN count(other) AS rewired_count
"""

#: Find contact SourceRecords from the Fundbox contacts feed, paginated by
#: source_record_pk cursor. Backed by the source_record_pk_unique constraint
#: so the range scan is indexed.
SCAN_CONTACT_SOURCE_RECORDS = """
MATCH (sr:SourceRecord)
      -[:FROM_SOURCE]->(ss:SourceSystem {source_key: 'fundbox:contacts'})
WHERE sr.source_record_pk > $cursor
  AND sr.lifecycle_status = 'active'
RETURN sr.source_record_pk AS source_record_pk,
       ss.source_key       AS source_system_key,
       sr.raw_payload       AS raw_payload
ORDER BY sr.source_record_pk
LIMIT $batch_size
"""

#: Resolve the Person attached to a SourceRecord looked up by its human-readable
#: source_record_id (e.g. "fundbox-user-12345"). Used by the
#: KNOWS materializer to resolve the declarer side of a contact link.
RESOLVE_PERSON_FROM_SOURCE_RECORD_ID = """
MATCH (sr:SourceRecord {source_record_id: $source_record_id})
MATCH (sr)-[:FROM_SOURCE]->(:SourceSystem {source_key: $source_system_key})
MATCH (sr)-[:LINKED_TO]->(p:Person {status: 'active'})
WHERE sr.lifecycle_status = 'active'
   OR (sr.lifecycle_status IS NULL AND sr.is_latest = true)
RETURN p.person_id AS person_id
LIMIT 1
"""

#: Resolve the Person attached to a SourceRecord by its graph-local pk.
RESOLVE_PERSON_FROM_SOURCE_RECORD_PK = """
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
      -[:LINKED_TO]->(p:Person {status: 'active'})
WHERE sr.lifecycle_status = 'active'
RETURN p.person_id AS person_id
LIMIT 1
"""

SCAN_CHAT_RELATIONSHIP_SOURCE_RECORDS = """
MATCH (sr:SourceRecord)-[:FROM_SOURCE]->(ss:SourceSystem)
WHERE sr.source_record_pk > $cursor
  AND sr.record_type = 'conversation'
  AND sr.lifecycle_status = 'active'
  AND sr.raw_payload CONTAINS 'primary_source_record_id'
  AND sr.raw_payload CONTAINS 'relationship'
RETURN sr.source_record_pk AS source_record_pk,
       ss.source_key       AS source_system_key,
       sr.raw_payload      AS raw_payload
ORDER BY sr.source_record_pk
LIMIT $batch_size
"""

RETIRE_KNOWS_PROJECTION = """
MATCH ()-[rel:KNOWS {source_record_pk: $source_record_pk}]->()
WHERE coalesce(rel.is_active, true)
SET rel.is_active = false,
    rel.retired_at = datetime(),
    rel.updated_at = datetime()
"""


REWIRE_KNOWS_IN = """
MATCH (other:Person)-[old:KNOWS]->(absorbed:Person {person_id: $absorbed_id})
WHERE other.person_id <> $survivor_id
WITH old, other, properties(old) AS props
DELETE old
WITH other, props
MATCH (survivor:Person {person_id: $survivor_id})
MERGE (other)-[rel:KNOWS {
    source_system_key: props.source_system_key,
    source_record_pk:  props.source_record_pk
}]->(survivor)
ON CREATE SET rel += props
RETURN count(other) AS rewired_count
"""
