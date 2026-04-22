"""Cypher constants for Person, Identifier, Address, fact, and golden-profile writes/reads."""

from __future__ import annotations

UPSERT_IDENTIFIER = """
MERGE (id:Identifier {
    identifier_type: $identifier_type,
    normalized_value: $normalized_value
})
ON CREATE SET
    id.identifier_id = randomUUID(),
    id.created_at = datetime()
RETURN id.identifier_id AS identifier_id
"""

UPSERT_ADDRESS = """
MERGE (addr:Address {
    country_code: $country_code,
    postal_code:  $postal_code,
    street_name:  $street_name,
    street_number: $street_number,
    unit_number:  $unit_number
})
ON CREATE SET
    addr.address_id      = randomUUID(),
    addr.building_name   = $building_name,
    addr.city            = $city,
    addr.state_province  = $state_province,
    addr.normalized_full = $normalized_full,
    addr.created_at      = datetime()
RETURN addr.address_id AS address_id
"""

CREATE_PERSON = """
CREATE (p:Person {
    person_id:  randomUUID(),
    status:     'active',
    is_high_value: false,
    is_high_risk:  false,
    profile_completeness_score: 0.0,
    created_at: datetime(),
    updated_at: datetime()
})
RETURN p.person_id AS person_id
"""

LINK_PERSON_TO_IDENTIFIER = """
MATCH (p:Person {person_id: $person_id})
MATCH (id:Identifier {identifier_type: $identifier_type, normalized_value: $normalized_value})
MERGE (p)-[rel:IDENTIFIED_BY]->(id)
ON CREATE SET
    rel.is_verified = $is_verified,
    rel.verification_method = $verification_method,
    rel.is_active = true,
    rel.quality_flag = $quality_flag,
    rel.first_seen_at = datetime(),
    rel.last_seen_at = datetime(),
    rel.last_confirmed_at = datetime(),
    rel.source_system_key = $source_system_key,
    rel.source_record_pk = $source_record_pk
ON MATCH SET
    rel.last_seen_at = datetime(),
    rel.last_confirmed_at = datetime(),
    rel.is_active = true,
    rel.source_record_pk = $source_record_pk
"""

LINK_PERSON_TO_ADDRESS = """
MATCH (p:Person {person_id: $person_id})
MATCH (addr:Address {
    country_code:  $country_code,
    postal_code:   $postal_code,
    street_name:   $street_name,
    street_number: $street_number,
    unit_number:   $unit_number
})
MERGE (p)-[rel:LIVES_AT]->(addr)
ON CREATE SET
    rel.is_active = true,
    rel.is_verified = $is_verified,
    rel.quality_flag = $quality_flag,
    rel.source_system_key = $source_system_key,
    rel.source_record_pk = $source_record_pk,
    rel.first_seen_at = datetime(),
    rel.last_seen_at = datetime(),
    rel.last_confirmed_at = datetime()
ON MATCH SET
    rel.last_seen_at = datetime(),
    rel.last_confirmed_at = datetime(),
    rel.is_active = true,
    rel.source_record_pk = $source_record_pk
"""

# Person -[:HAS_FACT]-> SourceRecord — never a self-loop.
CREATE_ATTRIBUTE_FACT = """
MATCH (p:Person {person_id: $person_id})
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
CREATE (p)-[:HAS_FACT {
    attribute_name:    $attribute_name,
    attribute_value:   $attribute_value,
    source_trust_tier: $source_trust_tier,
    confidence:        $confidence,
    quality_flag:      $quality_flag,
    is_current_hint:   false,
    observed_at:       datetime($observed_at),
    created_at:        datetime()
}]->(sr)
"""

FETCH_PERSON_FACTS = """
MATCH (p:Person {person_id: $person_id})-[f:HAS_FACT]->(sr:SourceRecord)
RETURN f.attribute_name AS attribute_name,
       f.attribute_value AS attribute_value,
       f.source_trust_tier AS source_trust_tier,
       f.observed_at AS observed_at,
       f.quality_flag AS quality_flag
"""

FETCH_PERSON_IDENTIFIERS = """
MATCH (p:Person {person_id: $person_id})-[rel:IDENTIFIED_BY]->(id:Identifier)
WHERE rel.is_active = true
RETURN id.identifier_type AS identifier_type,
       id.normalized_value AS normalized_value,
       rel.is_verified AS is_verified,
       rel.last_confirmed_at AS last_confirmed_at
"""

FETCH_PERSON_ADDRESSES = """
MATCH (p:Person {person_id: $person_id})-[rel:LIVES_AT]->(addr:Address)
WHERE rel.is_active = true
RETURN addr.address_id AS address_id,
       addr.normalized_full AS normalized_full,
       rel.is_verified AS is_verified,
       rel.last_confirmed_at AS last_confirmed_at
"""

FIND_BIRTHDAY_PERSONS = """
MATCH (p:Person)
WHERE p.status = 'active'
  AND p.preferred_dob IS NOT NULL
  AND p.preferred_phone IS NOT NULL
  AND substring(p.preferred_dob, 5, 5) = $mmdd
RETURN p.person_id          AS person_id,
       p.preferred_phone    AS phone,
       p.preferred_full_name AS full_name
"""

UPDATE_GOLDEN_PROFILE = """
MATCH (p:Person {person_id: $person_id})
SET p.preferred_full_name = $preferred_full_name,
    p.preferred_phone = $preferred_phone,
    p.preferred_email = $preferred_email,
    p.preferred_dob = $preferred_dob,
    p.preferred_address_id = $preferred_address_id,
    p.preferred_nric = $preferred_nric,
    p.profile_completeness_score = $profile_completeness_score,
    p.golden_profile_computed_at = datetime(),
    p.golden_profile_version = $golden_profile_version,
    p.updated_at = datetime()
RETURN p.person_id AS person_id
"""
