"""Read-side Cypher constants for Person lookup, search, connections, audit, matches."""

from __future__ import annotations

FIND_PERSON_BY_IDENTIFIER = """
MATCH (id:Identifier {identifier_type: $identifier_type, normalized_value: $value})
  <-[:IDENTIFIED_BY]-(p:Person)
WHERE p.status <> 'merged'
OPTIONAL MATCH (addr:Address {address_id: p.preferred_address_id})
OPTIONAL MATCH (sr:SourceRecord)-[:LINKED_TO]->(p)
WITH p, addr, count(sr) AS source_record_count
CALL {
  WITH p
  OPTIONAL MATCH (p)-[:IDENTIFIED_BY]->(:Identifier)<-[:IDENTIFIED_BY]-(ci:Person)
    WHERE ci.person_id <> p.person_id AND ci.status <> 'merged'
  OPTIONAL MATCH (p)-[:LIVES_AT]->(:Address)<-[:LIVES_AT]-(ca:Person)
    WHERE ca.person_id <> p.person_id AND ca.status <> 'merged'
  OPTIONAL MATCH (p)-[:KNOWS]-(ck:Person)
    WHERE ck.person_id <> p.person_id AND ck.status <> 'merged'
  WITH collect(DISTINCT ci) + collect(DISTINCT ca) + collect(DISTINCT ck) AS all_conn
  UNWIND all_conn AS c
  RETURN count(DISTINCT c) AS connection_count
}
RETURN p {
  .person_id, .status, .is_high_value, .is_high_risk,
  .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob, .preferred_nric,
  .profile_completeness_score, .golden_profile_computed_at, .golden_profile_version,
  .created_at, .updated_at
} AS person,
addr {
  .address_id, .unit_number, .street_number, .street_name,
  .city, .postal_code, .country_code, .normalized_full
} AS preferred_address,
source_record_count,
connection_count
ORDER BY p.updated_at DESC
"""

GET_PERSON_BY_ID = """
MATCH (p:Person {person_id: $person_id})
OPTIONAL MATCH (p)-[:MERGED_INTO]->(canonical:Person)
WITH coalesce(canonical, p) AS person
OPTIONAL MATCH (addr:Address {address_id: person.preferred_address_id})
OPTIONAL MATCH (sr:SourceRecord)-[:LINKED_TO]->(person)
WITH person, addr, count(sr) AS source_record_count
CALL {
  WITH person
  OPTIONAL MATCH (person)-[:IDENTIFIED_BY]->(:Identifier)<-[:IDENTIFIED_BY]-(ci:Person)
    WHERE ci.person_id <> person.person_id AND ci.status <> 'merged'
  OPTIONAL MATCH (person)-[:LIVES_AT]->(:Address)<-[:LIVES_AT]-(ca:Person)
    WHERE ca.person_id <> person.person_id AND ca.status <> 'merged'
  OPTIONAL MATCH (person)-[:KNOWS]-(ck:Person)
    WHERE ck.person_id <> person.person_id AND ck.status <> 'merged'
  WITH collect(DISTINCT ci) + collect(DISTINCT ca) + collect(DISTINCT ck) AS all_conn
  UNWIND all_conn AS c
  RETURN count(DISTINCT c) AS connection_count
}
RETURN person {
  .person_id, .status, .is_high_value, .is_high_risk,
  .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob, .preferred_nric,
  .profile_completeness_score, .golden_profile_computed_at, .golden_profile_version,
  .created_at, .updated_at
} AS person,
addr {
  .address_id, .unit_number, .street_number, .street_name,
  .city, .postal_code, .country_code, .normalized_full
} AS preferred_address,
source_record_count,
connection_count
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
       [] AS shared_addresses,
       [] AS knows_relationships
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
       shared_addresses,
       [] AS knows_relationships
ORDER BY other.preferred_full_name
SKIP $skip LIMIT $limit
"""

GET_PERSON_CONNECTIONS_KNOWS = """
MATCH (p:Person {person_id: $person_id})-[k:KNOWS]-(other:Person)
WHERE other.person_id <> p.person_id
  AND other.status <> 'merged'
WITH other,
  collect(DISTINCT {
    relationship_label: k.relationship_label,
    relationship_category: k.relationship_category
  }) AS knows_rels
RETURN other.person_id AS person_id,
       other.status AS status,
       other.preferred_full_name AS preferred_full_name,
       1 AS hops,
       [] AS shared_identifiers,
       [] AS shared_addresses,
       knows_rels AS knows_relationships
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
OPTIONAL MATCH (p)-[k:KNOWS]-(ok:Person)
  WHERE ok.person_id <> p.person_id AND ok.status <> 'merged'
WITH p,
  collect(DISTINCT CASE WHEN oi IS NOT NULL THEN {person_id: oi.person_id, status: oi.status, preferred_full_name: oi.preferred_full_name, identifier_type: id.identifier_type, normalized_value: id.normalized_value} END) AS id_links,
  collect(DISTINCT CASE WHEN oa IS NOT NULL THEN {person_id: oa.person_id, status: oa.status, preferred_full_name: oa.preferred_full_name, address_id: addr.address_id, normalized_full: addr.normalized_full} END) AS addr_links,
  collect(DISTINCT CASE WHEN ok IS NOT NULL THEN {person_id: ok.person_id, status: ok.status, preferred_full_name: ok.preferred_full_name, relationship_label: k.relationship_label, relationship_category: k.relationship_category} END) AS knows_links
UNWIND (id_links + addr_links + knows_links) AS link
WITH link WHERE link IS NOT NULL
WITH link.person_id AS person_id,
     link.status AS status,
     link.preferred_full_name AS preferred_full_name,
     collect(DISTINCT CASE WHEN link.identifier_type IS NOT NULL THEN {identifier_type: link.identifier_type, normalized_value: link.normalized_value} END) AS shared_identifiers_raw,
     collect(DISTINCT CASE WHEN link.address_id IS NOT NULL THEN {address_id: link.address_id, normalized_full: link.normalized_full} END) AS shared_addresses_raw,
     collect(DISTINCT CASE WHEN link.relationship_category IS NOT NULL THEN {relationship_label: link.relationship_label, relationship_category: link.relationship_category} END) AS knows_raw
RETURN person_id, status, preferred_full_name, 1 AS hops,
       [x IN shared_identifiers_raw WHERE x IS NOT NULL] AS shared_identifiers,
       [x IN shared_addresses_raw WHERE x IS NOT NULL] AS shared_addresses,
       [x IN knows_raw WHERE x IS NOT NULL] AS knows_relationships
ORDER BY preferred_full_name
SKIP $skip LIMIT $limit
"""

SEARCH_PERSONS = """
CALL db.index.fulltext.queryNodes('person_name_search', $query) YIELD node AS p, score
WHERE p.status <> 'merged'
  AND ($status IS NULL OR p.status = $status)
OPTIONAL MATCH (addr:Address {address_id: p.preferred_address_id})
OPTIONAL MATCH (sr:SourceRecord)-[:LINKED_TO]->(p)
WITH p, addr, score, count(sr) AS source_record_count
CALL {
  WITH p
  OPTIONAL MATCH (p)-[:IDENTIFIED_BY]->(:Identifier)<-[:IDENTIFIED_BY]-(ci:Person)
    WHERE ci.person_id <> p.person_id AND ci.status <> 'merged'
  OPTIONAL MATCH (p)-[:LIVES_AT]->(:Address)<-[:LIVES_AT]-(ca:Person)
    WHERE ca.person_id <> p.person_id AND ca.status <> 'merged'
  OPTIONAL MATCH (p)-[:KNOWS]-(ck:Person)
    WHERE ck.person_id <> p.person_id AND ck.status <> 'merged'
  WITH collect(DISTINCT ci) + collect(DISTINCT ca) + collect(DISTINCT ck) AS all_conn
  UNWIND all_conn AS c
  RETURN count(DISTINCT c) AS connection_count
}
RETURN p {
  .person_id, .status, .is_high_value, .is_high_risk,
  .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob, .preferred_nric,
  .profile_completeness_score, .golden_profile_computed_at, .golden_profile_version,
  .created_at, .updated_at
} AS person,
addr {
  .address_id, .unit_number, .street_number, .street_name,
  .city, .postal_code, .country_code, .normalized_full
} AS preferred_address,
source_record_count,
connection_count,
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

GET_PERSON_ENTITIES = """
MATCH (sr:SourceRecord)-[:LINKED_TO]->(p:Person {person_id: $person_id})
MATCH (sr)-[:FROM_SOURCE]->(:SourceSystem)-[:OPERATED_BY]->(e:Entity)
WITH e, count(DISTINCT sr) AS source_record_count
RETURN e {
  .entity_key, .display_name, .entity_type, .country_code, .is_active
} AS entity,
source_record_count
ORDER BY e.display_name
"""

GET_PERSON_IDENTIFIERS = """
MATCH (p:Person {person_id: $person_id})-[rel:IDENTIFIED_BY]->(id:Identifier)
RETURN id.identifier_type AS identifier_type,
       id.normalized_value AS normalized_value,
       rel.is_active AS is_active,
       rel.is_verified AS is_verified,
       rel.last_confirmed_at AS last_confirmed_at,
       rel.source_system_key AS source_system_key
ORDER BY rel.is_active DESC, id.identifier_type, id.normalized_value
SKIP $skip LIMIT $limit
"""

COUNT_PERSON_SOURCE_RECORDS = """
MATCH (sr:SourceRecord)-[:LINKED_TO]->(p:Person {person_id: $person_id})
RETURN count(sr) AS total
"""

COUNT_PERSON_IDENTIFIERS = """
MATCH (p:Person {person_id: $person_id})-[:IDENTIFIED_BY]->(id:Identifier)
RETURN count(id) AS total
"""

COUNT_PERSON_AUDIT = """
MATCH (me:MergeEvent)
WHERE (me)-[:ABSORBED]->(:Person {person_id: $person_id})
   OR (me)-[:SURVIVOR]->(:Person {person_id: $person_id})
RETURN count(me) AS total
"""

COUNT_PERSON_CONNECTIONS_IDENTIFIER = """
MATCH (p:Person {person_id: $person_id})-[:IDENTIFIED_BY]->(id:Identifier)
  <-[:IDENTIFIED_BY]-(other:Person)
WHERE other.person_id <> p.person_id
  AND other.status <> 'merged'
  AND ($identifier_type IS NULL OR id.identifier_type = $identifier_type)
RETURN count(DISTINCT other) AS total
"""

COUNT_PERSON_CONNECTIONS_ADDRESS = """
MATCH (p:Person {person_id: $person_id})-[:LIVES_AT]->(:Address)
  <-[:LIVES_AT]-(other:Person)
WHERE other.person_id <> p.person_id
  AND other.status <> 'merged'
RETURN count(DISTINCT other) AS total
"""

COUNT_PERSON_CONNECTIONS_KNOWS = """
MATCH (p:Person {person_id: $person_id})-[:KNOWS]-(other:Person)
WHERE other.person_id <> p.person_id
  AND other.status <> 'merged'
RETURN count(DISTINCT other) AS total
"""

COUNT_PERSON_CONNECTIONS_ALL = """
MATCH (p:Person {person_id: $person_id})
CALL {
  WITH p
  OPTIONAL MATCH (p)-[:IDENTIFIED_BY]->(:Identifier)<-[:IDENTIFIED_BY]-(ci:Person)
    WHERE ci.person_id <> p.person_id AND ci.status <> 'merged'
  OPTIONAL MATCH (p)-[:LIVES_AT]->(:Address)<-[:LIVES_AT]-(ca:Person)
    WHERE ca.person_id <> p.person_id AND ca.status <> 'merged'
  OPTIONAL MATCH (p)-[:KNOWS]-(ck:Person)
    WHERE ck.person_id <> p.person_id AND ck.status <> 'merged'
  WITH collect(DISTINCT ci) + collect(DISTINCT ca) + collect(DISTINCT ck) AS all_conn
  UNWIND all_conn AS c
  RETURN count(DISTINCT c) AS total
}
RETURN total
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
