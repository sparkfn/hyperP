"""Read-side Cypher constants for Person lookup, search, connections, audit, matches."""

from __future__ import annotations

FIND_PERSON_BY_IDENTIFIER = """
MATCH (id:Identifier {identifier_type: $identifier_type, normalized_value: $value})
  <-[lookup_identifier:IDENTIFIED_BY]-(p:Person)
WHERE coalesce(lookup_identifier.is_active, true) = true
  AND p.status <> 'merged'
OPTIONAL MATCH (addr:Address {address_id: p.preferred_address_id})
OPTIONAL MATCH (sr:SourceRecord)-[link:LINKED_TO]->(p)
WHERE coalesce(link.is_active, true) = true
  AND (sr.history_family IS NULL OR sr.history_family = 'activity')
  AND (sr.lifecycle_status = 'active'
    OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))
WITH p, addr, count(sr) AS source_record_count
CALL {
  WITH p
  OPTIONAL MATCH (p)-[p_identifier:IDENTIFIED_BY]->(:Identifier)
    <-[ci_identifier:IDENTIFIED_BY]-(ci:Person)
    WHERE coalesce(p_identifier.is_active, true) = true
      AND coalesce(ci_identifier.is_active, true) = true
      AND ci.person_id <> p.person_id AND ci.status <> 'merged'
  WITH p, collect(DISTINCT ci) AS identifier_conn
  OPTIONAL MATCH (p)-[p_address:LIVES_AT]->(:Address)
    <-[ca_address:LIVES_AT]-(ca:Person)
    WHERE coalesce(p_address.is_active, true) = true
      AND coalesce(ca_address.is_active, true) = true
      AND ca.person_id <> p.person_id AND ca.status <> 'merged'
  WITH p, identifier_conn, collect(DISTINCT ca) AS address_conn
  OPTIONAL MATCH (p)-[p_knows:KNOWS]-(ck:Person)
    WHERE coalesce(p_knows.is_active, true) = true
      AND ck.person_id <> p.person_id AND ck.status <> 'merged'
  WITH identifier_conn, address_conn, collect(DISTINCT ck) AS knows_conn
  WITH identifier_conn + address_conn + knows_conn AS all_conn
  UNWIND all_conn AS c
  RETURN count(DISTINCT c) AS connection_count
}
RETURN p {
  .person_id, .status, .is_high_value, .is_high_risk,
  .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob, .preferred_nric,
  .preferred_race_ethnicity,
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
WITH person, addr, count {
  (sr:SourceRecord)-[link:LINKED_TO]->(person)
  WHERE coalesce(link.is_active, true) = true
    AND (sr.history_family IS NULL OR sr.history_family = 'activity')
    AND (sr.lifecycle_status = 'active'
      OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))
} AS source_record_count
CALL {
  WITH person
  OPTIONAL MATCH (person)-[person_identifier:IDENTIFIED_BY]->(:Identifier)
    <-[ci_identifier:IDENTIFIED_BY]-(ci:Person)
    WHERE coalesce(person_identifier.is_active, true) = true
      AND coalesce(ci_identifier.is_active, true) = true
      AND ci.person_id <> person.person_id AND ci.status <> 'merged'
  WITH person, collect(DISTINCT ci) AS identifier_conn
  OPTIONAL MATCH (person)-[person_address:LIVES_AT]->(:Address)
    <-[ca_address:LIVES_AT]-(ca:Person)
    WHERE coalesce(person_address.is_active, true) = true
      AND coalesce(ca_address.is_active, true) = true
      AND ca.person_id <> person.person_id AND ca.status <> 'merged'
  WITH person, identifier_conn, collect(DISTINCT ca) AS address_conn
  OPTIONAL MATCH (person)-[person_knows:KNOWS]-(ck:Person)
    WHERE coalesce(person_knows.is_active, true) = true
      AND ck.person_id <> person.person_id AND ck.status <> 'merged'
  WITH identifier_conn, address_conn, collect(DISTINCT ck) AS knows_conn
  WITH identifier_conn + address_conn + knows_conn AS all_conn
  UNWIND all_conn AS c
  RETURN count(DISTINCT c) AS connection_count
}
CALL {
  WITH person
  OPTIONAL MATCH (person)-[:PURCHASED]->(o:Order)
  RETURN sum(o.total_amount) AS lifetime_value
}
RETURN person {
  .person_id, .status, .is_high_value, .is_high_risk,
  .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob, .preferred_nric,
  .preferred_race_ethnicity,
  .profile_completeness_score, .golden_profile_computed_at, .golden_profile_version,
  .created_at, .updated_at
} AS person,
addr {
  .address_id, .unit_number, .street_number, .street_name,
  .city, .postal_code, .country_code, .normalized_full
} AS preferred_address,
source_record_count,
connection_count,
lifetime_value
"""

GET_PERSON_LOYALTY = """
MATCH (p:Person {person_id: $person_id})
OPTIONAL MATCH (p)-[:MERGED_INTO]->(canonical:Person)
WITH coalesce(canonical, p) AS person
OPTIONAL MATCH (sr:SourceRecord {record_type: 'identity'})-[link:LINKED_TO]->(person)
WHERE coalesce(link.is_active, true) = true
  AND (sr.lifecycle_status = 'active'
  OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))
OPTIONAL MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
RETURN person.person_id AS person_id,
       collect(CASE WHEN sr IS NULL OR ss IS NULL THEN null ELSE {
         source_system: ss.source_key,
         observed_at: sr.observed_at,
         source_record_pk: sr.source_record_pk,
         raw_payload: sr.raw_payload
       } END) AS loyalty_rows
"""

GET_PERSON_VEHICLES = """
MATCH (p:Person {person_id: $person_id})
OPTIONAL MATCH (p)-[:MERGED_INTO]->(canonical:Person)
WITH coalesce(canonical, p) AS person
OPTIONAL MATCH (person)-[rel:OWNS_VEHICLE|BOUGHT_VEHICLE]->(v:Vehicle)
RETURN person.person_id AS person_id,
       collect(CASE WHEN v IS NULL THEN NULL ELSE {
         vehicle_id: v.vehicle_id,
         product: v.product,
         product_sku: v.product_sku,
         manufacturer: v.manufacturer,
         model: v.model,
         lta_tag: v.lta_tag,
         serial_number: v.serial_number,
         rel_type: type(rel),
         is_active: rel.is_active,
         conflict_flag: coalesce(v.conflict_flag, false),
         observed_at: rel.observed_at
       } END) AS vehicles
"""

GET_PERSON_SOURCE_RECORDS = """
MATCH (sr:SourceRecord)-[link:LINKED_TO]->(p:Person {person_id: $person_id})
MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
WHERE coalesce(link.is_active, true) = true
  AND (sr.history_family IS NULL OR sr.history_family = 'activity')
OPTIONAL MATCH (sr)-[:OWNED_BY]->(record_entity:Entity)
OPTIONAL MATCH (ss)-[:OPERATED_BY]->(source_entity:Entity)
WITH sr, p, ss, coalesce(record_entity, source_entity) AS entity
WHERE ($entity_key IS NULL OR entity.entity_key = $entity_key)
  AND ($record_type IS NULL OR sr.record_type = $record_type)
  AND (sr.lifecycle_status = 'active'
    OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))
RETURN sr {
  .source_record_pk, .source_record_id, .source_record_version,
  .record_type, .extraction_confidence, .extraction_method,
  .link_status, .lifecycle_status, .observed_at, .ingested_at,
  .parent_source_system, .parent_source_record_id, .parent_record_type,
  .history_family, .history_kind, .history_source,
  .event_category_id, .event_stage_id, .event_stage_semantic_id, .event_at,
  .history_projection_version, .history_projection_source, .history_projected_at,
  .conversation_ref, .raw_payload, .normalized_payload
} AS source_record,
ss.source_key AS source_system,
p.person_id AS linked_person_id,
entity.entity_key AS entity_key,
entity.display_name AS entity_display_name
ORDER BY sr.observed_at DESC
SKIP $skip LIMIT $limit
"""

GET_PERSON_SOURCE_RECORD_ENTITY_FACETS = """
MATCH (sr:SourceRecord)-[link:LINKED_TO]->(:Person {person_id: $person_id})
MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
WHERE coalesce(link.is_active, true) = true
  AND (sr.history_family IS NULL OR sr.history_family = 'activity')
OPTIONAL MATCH (sr)-[:OWNED_BY]->(record_entity:Entity)
OPTIONAL MATCH (ss)-[:OPERATED_BY]->(source_entity:Entity)
WITH sr, ss, coalesce(record_entity, source_entity) AS entity
WHERE (sr.lifecycle_status = 'active'
  OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))
RETURN ss.source_key AS source_system,
       entity.entity_key AS entity_key,
       entity.display_name AS entity_display_name,
       count(sr) AS count
ORDER BY source_system, entity_display_name
"""

GET_PERSON_BANKRUPTCY_CASES = """
MATCH (:Person {person_id: $person_id})-[bankruptcy_rel:HAS_BANKRUPTCY_CASE]->(bc:BankruptcyCase)
WHERE coalesce(bankruptcy_rel.is_active, true) = true
RETURN bc {
  .bankruptcy_case_id, .source_system_key, .source_case_id,
  .case_number, .document_type, .document_date,
  .event_type, .event_date, .trustee_name, .trustee_firm,
  .source_url, .first_seen_at, .last_seen_at, .created_at, .updated_at
} AS bankruptcy_case
ORDER BY coalesce(bc.event_date, bc.document_date, toString(bc.last_seen_at), toString(bc.updated_at), bc.source_case_id) DESC
SKIP $skip LIMIT $limit
"""

COUNT_PERSON_BANKRUPTCY_CASES = """
MATCH (:Person {person_id: $person_id})-[bankruptcy_rel:HAS_BANKRUPTCY_CASE]->(bc:BankruptcyCase)
WHERE coalesce(bankruptcy_rel.is_active, true) = true
RETURN count(bc) AS total
"""

GET_PERSON_TIMELINE = """
MATCH (sr:SourceRecord)-[link:LINKED_TO]->(p:Person {person_id: $person_id})
MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
WHERE coalesce(link.is_active, true) = true
  AND (sr.history_family IS NULL OR sr.history_family = 'activity')
OPTIONAL MATCH (sr)-[:DESCRIBES_CASE]->(bc:BankruptcyCase)
RETURN sr {
  .source_record_pk, .source_record_id, .source_record_version,
  .record_type, .extraction_confidence,
  .link_status, .observed_at, .ingested_at, .normalized_payload
} AS source_record,
ss.source_key AS source_system,
p.person_id AS linked_person_id,
bc {
  .bankruptcy_case_id, .source_system_key, .source_case_id,
  .case_number, .document_type, .document_date,
  .event_type, .event_date, .trustee_name, .trustee_firm,
  .source_url, .first_seen_at, .last_seen_at, .created_at, .updated_at
} AS bankruptcy_case
ORDER BY coalesce(sr.observed_at, sr.ingested_at) DESC, sr.source_record_pk DESC
SKIP $skip LIMIT $limit
"""

COUNT_PERSON_TIMELINE = """
MATCH (sr:SourceRecord)-[link:LINKED_TO]->(:Person {person_id: $person_id})
WHERE coalesce(link.is_active, true) = true
  AND (sr.history_family IS NULL OR sr.history_family = 'activity')
RETURN count(sr) AS total
"""

GET_PERSON_TIMELINE_TARGET = """
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})-[link:LINKED_TO]->(p:Person {person_id: $person_id})
MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
WHERE coalesce(link.is_active, true) = true
  AND (sr.history_family IS NULL OR sr.history_family = 'activity')
OPTIONAL MATCH (sr)-[:DESCRIBES_CASE]->(bc:BankruptcyCase)
RETURN sr {
  .source_record_pk, .source_record_id, .source_record_version,
  .record_type, .extraction_confidence,
  .link_status, .observed_at, .ingested_at, .normalized_payload
} AS source_record,
ss.source_key AS source_system,
p.person_id AS linked_person_id,
bc {
  .bankruptcy_case_id, .source_system_key, .source_case_id,
  .case_number, .document_type, .document_date,
  .event_type, .event_date, .trustee_name, .trustee_firm,
  .source_url, .first_seen_at, .last_seen_at, .created_at, .updated_at
} AS bankruptcy_case
"""

GET_PERSON_CONNECTIONS_IDENTIFIER = """
MATCH (p:Person {person_id: $person_id})-[p_identifier:IDENTIFIED_BY]->(id:Identifier)
  <-[other_identifier:IDENTIFIED_BY]-(other:Person)
WHERE coalesce(p_identifier.is_active, true) = true
  AND coalesce(other_identifier.is_active, true) = true
  AND other.person_id <> p.person_id
  AND other.status <> 'merged'
  AND ($identifier_type IS NULL OR id.identifier_type = $identifier_type)
WITH other, collect(DISTINCT {identifier_type: id.identifier_type, normalized_value: id.normalized_value}) AS shared_identifiers
RETURN other.person_id AS person_id,
       other.status AS status,
       other.preferred_full_name AS preferred_full_name,
       1 AS hops,
       shared_identifiers,
       [] AS shared_addresses,
       [] AS knows_relationships,
       [] AS connection_sources
ORDER BY other.preferred_full_name
SKIP $skip LIMIT $limit
"""

GET_PERSON_CONNECTIONS_ADDRESS = """
MATCH (p:Person {person_id: $person_id})-[p_address:LIVES_AT]->(addr:Address)
  <-[la:LIVES_AT]-(other:Person)
WHERE coalesce(p_address.is_active, true) = true
  AND coalesce(la.is_active, true) = true
  AND other.person_id <> p.person_id
  AND other.status <> 'merged'
WITH other,
     collect(DISTINCT {address_id: addr.address_id, normalized_full: addr.normalized_full, source_system_key: la.source_system_key}) AS shared_addresses,
     [ref IN collect(DISTINCT {
       source_system_key: la.source_system_key,
       source_record_pk: la.source_record_pk
     }) WHERE ref.source_system_key IS NOT NULL] AS source_refs
CALL {
  WITH source_refs
  UNWIND source_refs AS ref
  OPTIONAL MATCH (ss:SourceSystem {source_key: ref.source_system_key})
  OPTIONAL MATCH (sr:SourceRecord {source_record_pk: ref.source_record_pk})
    -[:FROM_SOURCE]->(ss)
  OPTIONAL MATCH (sr)-[:OWNED_BY]->(record_entity:Entity)
  OPTIONAL MATCH (ss)-[:OPERATED_BY]->(source_entity:Entity)
  WITH ref.source_system_key AS source_system_key,
       coalesce(record_entity, source_entity) AS entity
  RETURN collect(DISTINCT {
    source_system_key: source_system_key,
    entity_display_name: entity.display_name
  }) AS connection_sources
}
RETURN other.person_id AS person_id,
       other.status AS status,
       other.preferred_full_name AS preferred_full_name,
       1 AS hops,
       [] AS shared_identifiers,
       shared_addresses,
       [] AS knows_relationships,
       connection_sources
ORDER BY other.preferred_full_name
SKIP $skip LIMIT $limit
"""

GET_PERSON_CONNECTIONS_KNOWS = """
MATCH (p:Person {person_id: $person_id})-[k:KNOWS]-(other:Person)
WHERE coalesce(k.is_active, true) = true
  AND other.person_id <> p.person_id
  AND other.status <> 'merged'
WITH other,
  collect(DISTINCT {
    relationship_label: k.relationship_label,
    relationship_category: k.relationship_category,
    source_system_key: k.source_system_key
  }) AS knows_rels,
  [ref IN collect(DISTINCT {
    source_system_key: k.source_system_key,
    source_record_pk: k.source_record_pk
  }) WHERE ref.source_system_key IS NOT NULL] AS source_refs
CALL {
  WITH source_refs
  UNWIND source_refs AS ref
  OPTIONAL MATCH (ss:SourceSystem {source_key: ref.source_system_key})
  OPTIONAL MATCH (sr:SourceRecord {source_record_pk: ref.source_record_pk})
    -[:FROM_SOURCE]->(ss)
  OPTIONAL MATCH (sr)-[:OWNED_BY]->(record_entity:Entity)
  OPTIONAL MATCH (ss)-[:OPERATED_BY]->(source_entity:Entity)
  WITH ref.source_system_key AS source_system_key,
       coalesce(record_entity, source_entity) AS entity
  RETURN collect(DISTINCT {
    source_system_key: source_system_key,
    entity_display_name: entity.display_name
  }) AS connection_sources
}
RETURN other.person_id AS person_id,
       other.status AS status,
       other.preferred_full_name AS preferred_full_name,
       1 AS hops,
       [] AS shared_identifiers,
       [] AS shared_addresses,
       knows_rels AS knows_relationships,
       connection_sources
ORDER BY other.preferred_full_name
SKIP $skip LIMIT $limit
"""

GET_PERSON_CONNECTIONS_ALL = """
MATCH (p:Person {person_id: $person_id})
OPTIONAL MATCH (p)-[p_address:LIVES_AT]->(addr:Address)
  <-[la:LIVES_AT]-(oa:Person)
  WHERE coalesce(p_address.is_active, true) = true
    AND coalesce(la.is_active, true) = true
    AND oa.person_id <> p.person_id AND oa.status <> 'merged'
OPTIONAL MATCH (p)-[k:KNOWS]-(ok:Person)
  WHERE coalesce(k.is_active, true) = true
    AND ok.person_id <> p.person_id AND ok.status <> 'merged'
WITH p,
  collect(DISTINCT CASE WHEN oa IS NOT NULL THEN {person_id: oa.person_id, status: oa.status, preferred_full_name: oa.preferred_full_name, address_id: addr.address_id, normalized_full: addr.normalized_full, source_system_key: la.source_system_key, source_record_pk: la.source_record_pk} END) AS addr_links,
  collect(DISTINCT CASE WHEN ok IS NOT NULL THEN {person_id: ok.person_id, status: ok.status, preferred_full_name: ok.preferred_full_name, relationship_label: k.relationship_label, relationship_category: k.relationship_category, source_system_key: k.source_system_key, source_record_pk: k.source_record_pk} END) AS knows_links
UNWIND (addr_links + knows_links) AS link
WITH link WHERE link IS NOT NULL
WITH link.person_id AS person_id,
     link.status AS status,
     link.preferred_full_name AS preferred_full_name,
     collect(DISTINCT CASE WHEN link.address_id IS NOT NULL THEN {address_id: link.address_id, normalized_full: link.normalized_full, source_system_key: link.source_system_key} END) AS shared_addresses_raw,
     collect(DISTINCT CASE WHEN link.relationship_category IS NOT NULL THEN {relationship_label: link.relationship_label, relationship_category: link.relationship_category, source_system_key: link.source_system_key} END) AS knows_raw,
     [ref IN collect(DISTINCT {
       source_system_key: link.source_system_key,
       source_record_pk: link.source_record_pk
     }) WHERE ref.source_system_key IS NOT NULL] AS source_refs
WITH person_id, status, preferred_full_name,
     [x IN shared_addresses_raw WHERE x IS NOT NULL] AS shared_addresses,
     [x IN knows_raw WHERE x IS NOT NULL] AS knows_relationships,
     source_refs
CALL {
  WITH source_refs
  UNWIND source_refs AS ref
  OPTIONAL MATCH (ss:SourceSystem {source_key: ref.source_system_key})
  OPTIONAL MATCH (sr:SourceRecord {source_record_pk: ref.source_record_pk})
    -[:FROM_SOURCE]->(ss)
  OPTIONAL MATCH (sr)-[:OWNED_BY]->(record_entity:Entity)
  OPTIONAL MATCH (ss)-[:OPERATED_BY]->(source_entity:Entity)
  WITH ref.source_system_key AS source_system_key,
       coalesce(record_entity, source_entity) AS entity
  RETURN collect(DISTINCT {
    source_system_key: source_system_key,
    entity_display_name: entity.display_name
  }) AS connection_sources
}
RETURN person_id, status, preferred_full_name, 1 AS hops,
       [] AS shared_identifiers,
       shared_addresses,
       knows_relationships,
       connection_sources
ORDER BY preferred_full_name
SKIP $skip LIMIT $limit
"""

SEARCH_PERSONS = """
CALL db.index.fulltext.queryNodes('person_name_search', $query) YIELD node AS p, score
WHERE p.status <> 'merged'
  AND ($status IS NULL OR p.status = $status)
OPTIONAL MATCH (addr:Address {address_id: p.preferred_address_id})
OPTIONAL MATCH (sr:SourceRecord)-[link:LINKED_TO]->(p)
WHERE coalesce(link.is_active, true) = true
  AND (sr.history_family IS NULL OR sr.history_family = 'activity')
  AND (sr.lifecycle_status = 'active'
    OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))
WITH p, addr, score, count(sr) AS source_record_count
CALL {
  WITH p
  OPTIONAL MATCH (p)-[p_identifier:IDENTIFIED_BY]->(:Identifier)
    <-[ci_identifier:IDENTIFIED_BY]-(ci:Person)
    WHERE coalesce(p_identifier.is_active, true) = true
      AND coalesce(ci_identifier.is_active, true) = true
      AND ci.person_id <> p.person_id AND ci.status <> 'merged'
  OPTIONAL MATCH (p)-[p_address:LIVES_AT]->(:Address)
    <-[ca_address:LIVES_AT]-(ca:Person)
    WHERE coalesce(p_address.is_active, true) = true
      AND coalesce(ca_address.is_active, true) = true
      AND ca.person_id <> p.person_id AND ca.status <> 'merged'
  OPTIONAL MATCH (p)-[p_knows:KNOWS]-(ck:Person)
    WHERE coalesce(p_knows.is_active, true) = true
      AND ck.person_id <> p.person_id AND ck.status <> 'merged'
  WITH collect(DISTINCT ci) + collect(DISTINCT ca) + collect(DISTINCT ck) AS all_conn
  UNWIND all_conn AS c
  RETURN count(DISTINCT c) AS connection_count
}
RETURN p {
  .person_id, .status, .is_high_value, .is_high_risk,
  .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob, .preferred_nric,
  .preferred_race_ethnicity,
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
MATCH (p:Person {person_id: $person_id})
MATCH (event_person:Person)-[:MERGED_INTO*0..]->(p)
MATCH (me:MergeEvent)-[:ABSORBED|SURVIVOR]->(event_person)
WITH DISTINCT me
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
ORDER BY me.created_at DESC, me.merge_event_id DESC
SKIP $skip LIMIT $limit
"""

GET_PERSON_ENTITIES = """
MATCH (sr:SourceRecord)-[link:LINKED_TO]->(p:Person {person_id: $person_id})
WHERE coalesce(link.is_active, true) = true
  AND (sr.lifecycle_status = 'active'
   OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))
  AND (sr.history_family IS NULL OR sr.history_family = 'activity')
MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
OPTIONAL MATCH (sr)-[:OWNED_BY]->(record_entity:Entity)
OPTIONAL MATCH (ss)-[:OPERATED_BY]->(source_entity:Entity)
WITH coalesce(record_entity, source_entity) AS e, sr
WHERE e IS NOT NULL
WITH e, count(DISTINCT sr) AS source_record_count
RETURN e {
  .entity_key, .display_name, .entity_type, .country_code, .is_active
} AS entity,
source_record_count
ORDER BY e.display_name
"""

GET_PERSON_IDENTIFIERS = """
MATCH (p:Person {person_id: $person_id})-[rel:IDENTIFIED_BY]->(id:Identifier)
WITH id, rel
ORDER BY coalesce(rel.source_record_pk, '')
WITH id,
     rel.is_active AS is_active,
     rel.is_verified AS is_verified,
     rel.last_confirmed_at AS last_confirmed_at,
     rel.source_system_key AS source_system_key,
     collect(DISTINCT rel.source_record_pk) AS source_record_pks
ORDER BY is_active DESC, id.identifier_type, id.normalized_value,
         coalesce(source_system_key, ''), coalesce(last_confirmed_at, datetime('1970-01-01T00:00:00Z')),
         source_record_pks
SKIP $skip LIMIT $limit
WITH collect({
  identifier: id,
  is_active: is_active,
  is_verified: is_verified,
  last_confirmed_at: last_confirmed_at,
  source_system_key: source_system_key,
  source_record_pks: source_record_pks
}) AS page
CALL {
  WITH page
  UNWIND page AS item
  // Legacy projections can carry IDENTIFIED_BY edges without a source-record
  // provenance key. Preserve those identifiers by unwinding a one-null
  // sentinel instead of dropping the item entirely.
  UNWIND CASE WHEN size(item.source_record_pks) = 0
    THEN [null]
    ELSE item.source_record_pks
  END AS source_record_pk
  OPTIONAL MATCH (sr:SourceRecord {source_record_pk: source_record_pk})
  OPTIONAL MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
  OPTIONAL MATCH (sr)-[:OWNED_BY]->(record_entity:Entity)
  OPTIONAL MATCH (ss)-[:OPERATED_BY]->(source_entity:Entity)
  WITH item,
       sr,
       ss,
       coalesce(record_entity, source_entity) AS e
  WITH item,
       [record IN collect(DISTINCT CASE WHEN sr IS NULL THEN null ELSE {
         source_record: sr {
           .source_record_pk, .source_record_id, .source_record_version,
           .record_type, .extraction_confidence, .extraction_method,
           .link_status, .lifecycle_status, .observed_at, .ingested_at,
           .parent_source_system, .parent_source_record_id, .parent_record_type,
           .conversation_ref,
           .raw_payload, .normalized_payload
         },
         source_system: ss.source_key,
         linked_person_id: $person_id,
         entity_key: e.entity_key,
         entity_display_name: e.display_name
       } END) WHERE record IS NOT NULL] AS source_records,
       [source_record_id IN collect(DISTINCT CASE WHEN sr IS NULL THEN null ELSE sr.source_record_id END) WHERE source_record_id IS NOT NULL] AS source_record_ids,
       collect(DISTINCT CASE WHEN sr IS NOT NULL AND e IS NOT NULL THEN {
         e: e,
         sr: sr
       } END) AS sr_entity_pairs
  CALL {
    WITH sr_entity_pairs
    UNWIND CASE WHEN size(sr_entity_pairs) = 0
      THEN [{e: null, sr: null}]
      ELSE sr_entity_pairs
    END AS pair
    WITH pair.e AS e, pair.sr AS sr
    WITH e, count(DISTINCT sr) AS source_record_count
    RETURN [entity IN collect(CASE WHEN e IS NULL THEN null ELSE e {
           .entity_key, .display_name, .entity_type, .country_code, .is_active,
           source_record_count: source_record_count
         } END) WHERE entity IS NOT NULL] AS entities
  }
  RETURN item, source_records, source_record_ids, entities
}
RETURN item.identifier.identifier_type AS identifier_type,
       item.identifier.normalized_value AS normalized_value,
       item.is_active AS is_active,
       item.is_verified AS is_verified,
       item.last_confirmed_at AS last_confirmed_at,
       item.source_system_key AS source_system_key,
       item.source_record_pks AS source_record_pks,
       source_record_ids AS source_record_ids,
       entities AS entities,
       source_records AS source_records
ORDER BY item.is_active DESC,
         item.identifier.identifier_type,
         item.identifier.normalized_value,
         coalesce(item.source_system_key, ''),
         coalesce(item.last_confirmed_at, datetime('1970-01-01T00:00:00Z')),
         item.source_record_pks
"""
GET_PERSON_SHARED_IDENTIFIERS = """
MATCH (p:Person {person_id: $person_id})-[p_identifier:IDENTIFIED_BY]->(id:Identifier)
  <-[other_identifier:IDENTIFIED_BY]-(other:Person)
WHERE coalesce(p_identifier.is_active, true) = true
  AND coalesce(other_identifier.is_active, true) = true
  AND other.person_id <> p.person_id
  AND other.status <> 'merged'
WITH other, id
ORDER BY id.identifier_type, id.normalized_value
RETURN other.person_id AS person_id,
       other.status AS status,
       other.preferred_full_name AS preferred_full_name,
       other.preferred_phone AS preferred_phone,
       other.preferred_email AS preferred_email,
       other.preferred_dob AS preferred_dob,
       other.profile_completeness_score AS profile_completeness_score,
       collect(DISTINCT {
         identifier_type: id.identifier_type,
         normalized_value: id.normalized_value
       }) AS identifiers
ORDER BY preferred_full_name, person_id
SKIP $skip LIMIT $limit
"""

COUNT_PERSON_SOURCE_RECORDS = """
MATCH (sr:SourceRecord)-[link:LINKED_TO]->(p:Person {person_id: $person_id})
MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
WHERE coalesce(link.is_active, true) = true
  AND (sr.history_family IS NULL OR sr.history_family = 'activity')
OPTIONAL MATCH (sr)-[:OWNED_BY]->(record_entity:Entity)
OPTIONAL MATCH (ss)-[:OPERATED_BY]->(source_entity:Entity)
WITH sr, coalesce(record_entity, source_entity) AS entity
WHERE ($entity_key IS NULL OR entity.entity_key = $entity_key)
  AND ($record_type IS NULL OR sr.record_type = $record_type)
  AND (sr.lifecycle_status = 'active'
    OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))
RETURN count(sr) AS total
"""

COUNT_PERSON_IDENTIFIERS = """
MATCH (p:Person {person_id: $person_id})-[rel:IDENTIFIED_BY]->(id:Identifier)
WITH DISTINCT id,
     rel.is_active AS is_active,
     rel.is_verified AS is_verified,
     rel.last_confirmed_at AS last_confirmed_at,
     rel.source_system_key AS source_system_key
RETURN count(*) AS total
"""

COUNT_PERSON_SHARED_IDENTIFIERS = """
MATCH (p:Person {person_id: $person_id})-[p_identifier:IDENTIFIED_BY]->(:Identifier)
  <-[other_identifier:IDENTIFIED_BY]-(other:Person)
WHERE coalesce(p_identifier.is_active, true) = true
  AND coalesce(other_identifier.is_active, true) = true
  AND other.person_id <> p.person_id
  AND other.status <> 'merged'
RETURN count(DISTINCT other) AS total
"""

COUNT_PERSON_AUDIT = """
MATCH (p:Person {person_id: $person_id})
MATCH (event_person:Person)-[:MERGED_INTO*0..]->(p)
MATCH (me:MergeEvent)-[:ABSORBED|SURVIVOR]->(event_person)
RETURN count(DISTINCT me) AS total
"""

COUNT_PERSON_CONNECTIONS_IDENTIFIER = """
MATCH (p:Person {person_id: $person_id})-[p_identifier:IDENTIFIED_BY]->(id:Identifier)
  <-[other_identifier:IDENTIFIED_BY]-(other:Person)
WHERE coalesce(p_identifier.is_active, true) = true
  AND coalesce(other_identifier.is_active, true) = true
  AND other.person_id <> p.person_id
  AND other.status <> 'merged'
  AND ($identifier_type IS NULL OR id.identifier_type = $identifier_type)
RETURN count(DISTINCT other) AS total
"""

COUNT_PERSON_CONNECTIONS_ADDRESS = """
MATCH (p:Person {person_id: $person_id})-[p_address:LIVES_AT]->(:Address)
  <-[other_address:LIVES_AT]-(other:Person)
WHERE coalesce(p_address.is_active, true) = true
  AND coalesce(other_address.is_active, true) = true
  AND other.person_id <> p.person_id
  AND other.status <> 'merged'
RETURN count(DISTINCT other) AS total
"""

COUNT_PERSON_CONNECTIONS_KNOWS = """
MATCH (p:Person {person_id: $person_id})-[knows:KNOWS]-(other:Person)
WHERE coalesce(knows.is_active, true) = true
  AND other.person_id <> p.person_id
  AND other.status <> 'merged'
RETURN count(DISTINCT other) AS total
"""

COUNT_PERSON_CONNECTIONS_ALL = """
MATCH (p:Person {person_id: $person_id})
CALL {
  WITH p
  OPTIONAL MATCH (p)-[p_address:LIVES_AT]->(:Address)
    <-[ca_address:LIVES_AT]-(ca:Person)
    WHERE coalesce(p_address.is_active, true) = true
      AND coalesce(ca_address.is_active, true) = true
      AND ca.person_id <> p.person_id AND ca.status <> 'merged'
  OPTIONAL MATCH (p)-[p_knows:KNOWS]-(ck:Person)
    WHERE coalesce(p_knows.is_active, true) = true
      AND ck.person_id <> p.person_id AND ck.status <> 'merged'
  WITH collect(DISTINCT ca) + collect(DISTINCT ck) AS all_conn
  UNWIND all_conn AS c
  RETURN count(DISTINCT c) AS total
}
RETURN total
"""

GET_PERSON_POSSIBLE_MATCH_DETAIL = """
MATCH (p:Person {person_id: $person_id})-[p_identifier:IDENTIFIED_BY]->(id:Identifier)
  <-[candidate_identifier:IDENTIFIED_BY]-(candidate:Person {person_id: $candidate_person_id})
WHERE coalesce(p_identifier.is_active, true) = true
  AND coalesce(candidate_identifier.is_active, true) = true
  AND candidate.status <> 'merged'
WITH DISTINCT p, candidate, id
CALL {
  WITH p, id
  MATCH (p)-[rel:IDENTIFIED_BY]->(id)
  MATCH (sr:SourceRecord {source_record_pk: rel.source_record_pk})
  WHERE coalesce(rel.is_active, true) = true
    AND (sr.lifecycle_status = 'active'
     OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))
  OPTIONAL MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
  OPTIONAL MATCH (sr)-[:OWNED_BY]->(record_entity:Entity)
  OPTIONAL MATCH (ss)-[:OPERATED_BY]->(source_entity:Entity)
  WITH sr, ss, coalesce(record_entity, source_entity) AS entity
  RETURN collect(DISTINCT sr {
    .source_record_pk, .source_record_id, .source_record_version,
    entity_key: entity.entity_key,
    source_system: ss.source_key,
    entity_display_name: entity.display_name,
    .record_type, .extraction_confidence, .extraction_method,
    .link_status, .lifecycle_status, .linked_person_id, .observed_at, .ingested_at,
    .parent_source_system, .parent_source_record_id, .parent_record_type,
    .conversation_ref, .raw_payload, .normalized_payload
  }) AS current_person_source_records
}
CALL {
  WITH candidate, id
  MATCH (candidate)-[rel:IDENTIFIED_BY]->(id)
  MATCH (sr:SourceRecord {source_record_pk: rel.source_record_pk})
  WHERE coalesce(rel.is_active, true) = true
    AND (sr.lifecycle_status = 'active'
     OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))
  OPTIONAL MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
  OPTIONAL MATCH (sr)-[:OWNED_BY]->(record_entity:Entity)
  OPTIONAL MATCH (ss)-[:OPERATED_BY]->(source_entity:Entity)
  WITH sr, ss, coalesce(record_entity, source_entity) AS entity
  RETURN collect(DISTINCT sr {
    .source_record_pk, .source_record_id, .source_record_version,
    entity_key: entity.entity_key,
    source_system: ss.source_key,
    entity_display_name: entity.display_name,
    .record_type, .extraction_confidence, .extraction_method,
    .link_status, .lifecycle_status, .linked_person_id, .observed_at, .ingested_at,
    .parent_source_system, .parent_source_record_id, .parent_record_type,
    .conversation_ref, .raw_payload, .normalized_payload
  }) AS candidate_source_records
}
RETURN candidate.person_id AS candidate_person_id,
       candidate.preferred_full_name AS candidate_name,
       id.identifier_type AS identifier_type,
       id.normalized_value AS normalized_value,
       candidate_source_records AS candidate_source_records,
       current_person_source_records AS current_person_source_records
ORDER BY id.identifier_type, id.normalized_value
"""


COUNT_PERSON_MATCHES = """
MATCH (p:Person {person_id: $person_id})
MATCH (md:MatchDecision)-[:ABOUT_LEFT|ABOUT_RIGHT]->(p)
WITH DISTINCT md
RETURN count(md) AS total
"""


GET_PERSON_MATCHES = """
MATCH (p:Person {person_id: $person_id})
MATCH (md:MatchDecision)-[:ABOUT_LEFT|ABOUT_RIGHT]->(p)
WITH DISTINCT md
OPTIONAL MATCH (md)-[:ABOUT_LEFT]->(left)
OPTIONAL MATCH (md)-[:ABOUT_RIGHT]->(right)
OPTIONAL MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md)
RETURN md {
  .match_decision_id, .engine_type, .engine_version, .policy_version,
  .decision, .confidence, .reasons, .blocking_conflicts, .created_at
} AS match_decision,
left.person_id AS left_person_id,
right.person_id AS right_person_id,
rc.review_case_id AS review_case_id,
rc.queue_state AS review_case_queue_state,
rc.assigned_to AS review_case_assigned_to
ORDER BY md.created_at DESC
SKIP $skip LIMIT $limit
"""
