"""Cypher constants for survivorship overrides and golden-profile recompute."""

from __future__ import annotations

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
       ss.field_trust AS field_trust
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
       ss.field_trust AS field_trust
ORDER BY la.last_seen_at DESC
LIMIT 1
"""

GET_BEST_IDENTIFIER = """
MATCH (p:Person {person_id: $person_id})-[rel:IDENTIFIED_BY]->(id:Identifier {identifier_type: $identifier_type})
WHERE rel.is_active = true
RETURN id.normalized_value AS normalized_value,
       rel.is_verified AS is_verified,
       rel.last_confirmed_at AS last_confirmed_at
ORDER BY rel.is_verified DESC, rel.last_confirmed_at DESC
LIMIT 1
"""

UPDATE_GOLDEN_PROFILE = """
MATCH (p:Person {person_id: $person_id})
SET p.preferred_full_name = $full_name,
    p.preferred_phone = $phone,
    p.preferred_email = $email,
    p.preferred_dob = $dob,
    p.preferred_address_id = $address_id,
    p.preferred_nric = $nric,
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
  metadata: '{}',
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

GET_IDENTIFIER_VALUE_FOR_SR = """
MATCH (p:Person {person_id: $person_id})-[rel:IDENTIFIED_BY {source_record_pk: $source_record_pk}]->(id:Identifier {identifier_type: $identifier_type})
RETURN id.normalized_value AS value
LIMIT 1
"""

GET_ADDRESS_BY_NORMALIZED = """
MATCH (a:Address {normalized_full: $normalized_full})
RETURN a.address_id AS address_id
LIMIT 1
"""

GET_ADDRESS_FOR_SR = """
MATCH (p:Person {person_id: $person_id})-[la:LIVES_AT {source_record_pk: $source_record_pk}]->(a:Address)
RETURN a.address_id AS address_id, a.normalized_full AS normalized_full
LIMIT 1
"""

GET_FIELD_OPTIONS = """
MATCH (p:Person {person_id: $person_id, status: 'active'})
CALL {
  WITH p
  MATCH (p)-[f:HAS_FACT]->(sr:SourceRecord)
  WHERE f.attribute_name IN ['full_name', 'dob']
    AND coalesce(f.quality_flag, 'valid') <> 'invalid_format'
    AND coalesce(f.quality_flag, 'valid') <> 'placeholder_value'
  MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
  OPTIONAL MATCH (ss)-[:OPERATED_BY]->(e:Entity)
  RETURN collect({
    field_name: 'preferred_' + f.attribute_name,
    source_kind: 'source_record_fact',
    identifier_type: null,
    value: toString(f.attribute_value),
    address_id: null,
    source_record_pk: sr.source_record_pk,
    source_system: ss.source_key,
    entity_display_name: e.display_name,
    observed_at: toString(f.observed_at)
  }) AS fact_opts
}
CALL {
  WITH p
  MATCH (p)-[rel:IDENTIFIED_BY]->(id:Identifier)
  WHERE id.identifier_type IN ['phone', 'email', 'nric'] AND rel.is_active = true
  MATCH (sr:SourceRecord {source_record_pk: rel.source_record_pk})-[:FROM_SOURCE]->(ss:SourceSystem)
  OPTIONAL MATCH (ss)-[:OPERATED_BY]->(e:Entity)
  RETURN collect({
    field_name: 'preferred_' + id.identifier_type,
    source_kind: 'identifier',
    identifier_type: id.identifier_type,
    value: id.normalized_value,
    address_id: null,
    source_record_pk: rel.source_record_pk,
    source_system: ss.source_key,
    entity_display_name: e.display_name,
    observed_at: toString(rel.last_seen_at)
  }) AS id_opts
}
CALL {
  WITH p
  MATCH (p)-[la:LIVES_AT]->(a:Address)
  WHERE la.is_active = true AND la.quality_flag IN ['valid', 'partial_parse']
  MATCH (sr:SourceRecord {source_record_pk: la.source_record_pk})-[:FROM_SOURCE]->(ss:SourceSystem)
  OPTIONAL MATCH (ss)-[:OPERATED_BY]->(e:Entity)
  RETURN collect({
    field_name: 'preferred_address',
    source_kind: 'address',
    identifier_type: null,
    value: a.normalized_full,
    address_id: a.address_id,
    source_record_pk: la.source_record_pk,
    source_system: ss.source_key,
    entity_display_name: e.display_name,
    observed_at: toString(la.last_seen_at)
  }) AS addr_opts
}
RETURN p.preferred_full_name AS preferred_full_name,
       p.preferred_dob AS preferred_dob,
       p.preferred_phone AS preferred_phone,
       p.preferred_email AS preferred_email,
       p.preferred_nric AS preferred_nric,
       p.preferred_address_id AS preferred_address_id,
       p.survivorship_overrides AS overrides,
       fact_opts + id_opts + addr_opts AS options
"""

UPDATE_OVERRIDES = """
MATCH (p:Person {person_id: $person_id})
SET p.survivorship_overrides = $overrides, p.updated_at = datetime()
"""

UPDATE_GOLDEN_FIELD = """
MATCH (p:Person {person_id: $person_id})
SET p[$field_name] = $value, p.updated_at = datetime()
"""

UPSERT_CUSTOM_ADDRESS = """
MERGE (a:Address {normalized_full: $normalized_full})
ON CREATE SET
  a.address_id = randomUUID(),
  a.created_at = datetime()
SET a.updated_at = datetime()
RETURN a.address_id AS address_id
"""

CREATE_OVERRIDE_AUDIT = """
MATCH (p:Person {person_id: $person_id})
CREATE (me:MergeEvent {
  merge_event_id: randomUUID(),
  event_type: 'survivorship_override',
  actor_type: 'admin',
  actor_id: $actor_id,
  reason: $reason,
  metadata: '{}',
  created_at: datetime()
})
CREATE (me)-[:SURVIVOR]->(p)
"""
