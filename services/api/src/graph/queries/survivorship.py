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
