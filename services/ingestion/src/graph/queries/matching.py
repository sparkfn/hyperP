"""Cypher constants for candidate generation, MatchDecision persistence, and ReviewCase creation."""

from __future__ import annotations

# --- Candidate generation -------------------------------------------------

FIND_CANDIDATES_BY_IDENTIFIER = """
MATCH (id:Identifier {
    identifier_type: $identifier_type,
    identifier_scope: $identifier_scope,
    normalized_value: $normalized_value
})
<-[rel:IDENTIFIED_BY]-(candidate:Person {status: 'active'})
WHERE coalesce(rel.is_active, true) = true
  AND rel.quality_flag IN ['valid', 'partial_parse']
RETURN DISTINCT candidate.person_id AS person_id
"""

FIND_CANDIDATES_BY_IDENTIFIERS_BATCH = """
UNWIND $identifiers AS input
OPTIONAL MATCH (id:Identifier {
    identifier_type: input.identifier_type,
    identifier_scope: input.identifier_scope,
    normalized_value: input.normalized_value
})
CALL (id) {
  OPTIONAL MATCH (id)<-[fanout_rel:IDENTIFIED_BY]-(fanout_person:Person {status: 'active'})
  WHERE coalesce(fanout_rel.is_active, true) = true
  RETURN count(DISTINCT fanout_person) AS fanout
}
CALL (id) {
  OPTIONAL MATCH (id)<-[rel:IDENTIFIED_BY]-(candidate:Person {status: 'active'})
  WHERE coalesce(rel.is_active, true) = true
    AND rel.quality_flag IN ['valid', 'partial_parse']
  RETURN collect(DISTINCT candidate.person_id) AS person_ids
}
RETURN input.input_index AS input_index,
       input.identifier_type AS identifier_type,
       input.normalized_value AS normalized_value,
       fanout,
       person_ids
ORDER BY input_index
"""

FIND_CANDIDATES_BY_ADDRESS = """
MATCH (addr:Address {
    country_code:  $country_code,
    postal_code:   $postal_code,
    street_name:   $street_name,
    street_number: $street_number,
    unit_number:   $unit_number
})
<-[rel:LIVES_AT]-(candidate:Person {status: 'active'})
WHERE coalesce(rel.is_active, true) = true
  AND rel.quality_flag IN ['valid', 'partial_parse']
RETURN DISTINCT candidate.person_id AS person_id
"""

FIND_CANDIDATES_BY_ADDRESSES_BATCH = """
UNWIND $addresses AS input
OPTIONAL MATCH (addr:Address {
    country_code: input.country_code,
    postal_code: input.postal_code,
    street_name: input.street_name,
    street_number: input.street_number,
    unit_number: input.unit_number
})
OPTIONAL MATCH (addr)<-[rel:LIVES_AT]-(candidate:Person {status: 'active'})
WHERE coalesce(rel.is_active, true) = true
  AND rel.quality_flag IN ['valid', 'partial_parse']
RETURN input.input_index AS input_index,
       collect(DISTINCT candidate.person_id) AS person_ids
ORDER BY input_index
"""

CHECK_IDENTIFIER_FANOUT = """
MATCH (id:Identifier {
    identifier_type: $identifier_type,
    identifier_scope: $identifier_scope,
    normalized_value: $normalized_value
})
      <-[rel:IDENTIFIED_BY]-(p:Person {status: 'active'})
WHERE coalesce(rel.is_active, true) = true
RETURN count(DISTINCT p) AS fanout
"""

CHECK_NO_MATCH_LOCK = """
MATCH (a:Person {person_id: $left_person_id})
      -[lock:NO_MATCH_LOCK]-
      (b:Person {person_id: $right_person_id})
WHERE lock.expires_at IS NULL OR lock.expires_at > datetime()
RETURN count(lock) > 0 AS is_locked
"""

# --- MatchDecision persistence --------------------------------------------

CREATE_MATCH_DECISION = """
CREATE (md:MatchDecision {
    match_decision_id: randomUUID(),
    engine_type: $engine_type,
    engine_version: $engine_version,
    decision: $decision,
    confidence: $confidence,
    reasons: $reasons,
    blocking_conflicts: $blocking_conflicts,
    review_candidate_person_ids: $review_candidate_person_ids,
    feature_snapshot: $feature_snapshot,
    policy_version: $policy_version,
    created_at: datetime(),
    retention_expires_at: null
})
RETURN md.match_decision_id AS match_decision_id
"""

LINK_MATCH_DECISION_LEFT_PERSON = """
MATCH (md:MatchDecision {match_decision_id: $match_decision_id})
MATCH (p:Person {person_id: $person_id})
CREATE (md)-[:ABOUT_LEFT {entity_type: 'person'}]->(p)
"""

LINK_MATCH_DECISION_RIGHT_PERSON = """
MATCH (md:MatchDecision {match_decision_id: $match_decision_id})
MATCH (p:Person {person_id: $person_id})
CREATE (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(p)
"""

LINK_MATCH_DECISION_LEFT_SOURCE_RECORD = """
MATCH (md:MatchDecision {match_decision_id: $match_decision_id})
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
CREATE (md)-[:ABOUT_LEFT {entity_type: 'source_record'}]->(sr)
"""

LINK_MATCH_DECISION_RIGHT_SOURCE_RECORD = """
MATCH (md:MatchDecision {match_decision_id: $match_decision_id})
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
CREATE (md)-[:ABOUT_RIGHT {entity_type: 'source_record'}]->(sr)
"""

# --- ReviewCase creation --------------------------------------------------

CREATE_REVIEW_CASE = """
MATCH (md:MatchDecision {match_decision_id: $match_decision_id})
CREATE (rc:ReviewCase {
    review_case_id: randomUUID(),
    priority: $priority,
    queue_state: 'open',
    assigned_to: null,
    follow_up_at: null,
    sla_due_at: datetime($sla_due_at),
    resolution: null,
    resolved_at: null,
    actions: [],
    created_at: datetime(),
    updated_at: datetime()
})-[:FOR_DECISION]->(md)
RETURN rc.review_case_id AS review_case_id
"""

SUPPRESS_MATCH_DECISION_REVIEW = """
MATCH (md:MatchDecision {match_decision_id: $match_decision_id})
SET md.review_suppressed = true,
    md.suppressed_reason = $suppressed_reason
RETURN md.match_decision_id AS match_decision_id
"""


# --- v2 participant candidate generation ----------------------------------

FIND_PARTICIPANT_CANDIDATES = """
UNWIND $identifiers AS input
MATCH (identifier:Identifier {
    identifier_type: input.identifier_type,
    identifier_scope: input.identifier_scope,
    normalized_value: input.normalized_value
})
WITH input, identifier,
     (input.fanout_cap IS NOT NULL
      AND coalesce(identifier.participant_count, 0) > input.fanout_cap) AS over_cap,
     CASE WHEN input.identifier_type = 'nric' THEN 1 ELSE 0 END AS exact_nric
CALL {
    WITH identifier, exact_nric, over_cap
    MATCH (person:Person {status: 'active'})-[:HAS_IDENTIFIER]->(identifier)
    RETURN 'person' AS participant_kind, person.person_id AS participant_id,
           exact_nric AS exact_nric, over_cap AS over_cap, identifier AS identifier
    UNION
    WITH identifier, exact_nric, over_cap
    MATCH (profile:Profile {status: 'open'})-[:HAS_IDENTIFIER]->(identifier)
    RETURN 'profile' AS participant_kind, profile.profile_id AS participant_id,
           exact_nric AS exact_nric, over_cap AS over_cap, identifier AS identifier
    UNION
    WITH identifier, exact_nric, over_cap
    MATCH (record:SourceRecord)-[:ASSERTS]->(identifier)
    WHERE record.lifecycle_status = 'parked'
      AND record.link_status = 'unlinked'
      AND record.attribution_status = 'parked'
      AND record.reeval_pending = true
      AND record.source_record_pk <> $source_record_pk
    WITH record ORDER BY record.source_record_pk LIMIT 200
    RETURN 'source_record' AS participant_kind, record.source_record_pk AS participant_id,
           exact_nric AS exact_nric, over_cap AS over_cap, identifier AS identifier
}
WITH participant_kind, participant_id,
     count(DISTINCT identifier) AS shared_key_count,
     max(exact_nric) AS exact_nric,
     sum(CASE WHEN over_cap THEN 0 ELSE 1 END) AS direct_key_count
WHERE direct_key_count >= 1
RETURN participant_kind, participant_id, shared_key_count
ORDER BY exact_nric DESC, shared_key_count DESC, participant_kind, participant_id
LIMIT 100
"""

CANDIDATE_SNAPSHOT_BATCH = """
UNWIND $candidates AS candidate
MATCH (person:Person {person_id: candidate.person_id})
CALL {
    WITH person
    OPTIONAL MATCH (person)-[link:HAS_IDENTIFIER]->(identifier:Identifier)
    WHERE identifier.identifier_type IN ['phone', 'email']
    WITH person, identifier, link
    OPTIONAL MATCH (record:SourceRecord)-[linked:LINKED_TO]->(person)
    WHERE coalesce(linked.is_active, true) = true
      AND coalesce(record.link_status, 'linked') <> 'pending_review'
      AND EXISTS { MATCH (record)-[:ASSERTS]->(identifier) }
    WITH identifier, link,
         max(CASE WHEN record.record_type IS NOT NULL
                   AND record.record_type <> 'conversation' THEN 1 ELSE 0 END) = 1
             AS is_system_sourced
    RETURN collect(CASE WHEN identifier IS NULL THEN null ELSE {
        identifier_type: identifier.identifier_type,
        normalized_value: identifier.normalized_value,
        is_verified: coalesce(link.verified_count, 0) > 0,
        last_confirmed_at: link.last_confirmed_at,
        is_system_sourced: is_system_sourced
    } END) AS has_identifiers
}
CALL {
    WITH person
    OPTIONAL MATCH (person)-[legacy:IDENTIFIED_BY]->(identifier:Identifier)
    WHERE $include_legacy
      AND coalesce(legacy.is_active, true) = true
      AND identifier.identifier_type IN ['phone', 'email']
    OPTIONAL MATCH (src:SourceRecord {source_record_pk: legacy.source_record_pk})
    WITH identifier,
         max(CASE WHEN legacy.is_verified = true THEN 1 ELSE 0 END) = 1 AS is_verified,
         max(legacy.last_confirmed_at) AS last_confirmed_at,
         max(CASE WHEN src.record_type IS NOT NULL
                   AND src.record_type <> 'conversation' THEN 1 ELSE 0 END) = 1
             AS is_system_sourced
    RETURN collect(CASE WHEN identifier IS NULL THEN null ELSE {
        identifier_type: identifier.identifier_type,
        normalized_value: identifier.normalized_value,
        is_verified: is_verified,
        last_confirmed_at: last_confirmed_at,
        is_system_sourced: is_system_sourced
    } END) AS legacy_identifiers
}
CALL {
    WITH person
    OPTIONAL MATCH (person)-[fact:HAS_FACT]->(:SourceRecord)
    WHERE coalesce(fact.is_active, true) = true
      AND fact.attribute_name IN ['full_name', 'preferred_name', 'legal_name', 'dob']
    RETURN collect(DISTINCT CASE WHEN fact IS NULL THEN null ELSE {
        attribute_name: fact.attribute_name,
        attribute_value: fact.attribute_value
    } END) AS facts
}
CALL {
    WITH person
    OPTIONAL MATCH (person)-[lives:LIVES_AT]->(address:Address)
    WHERE coalesce(lives.is_active, true) = true
    RETURN collect(DISTINCT CASE WHEN address IS NULL THEN null ELSE {
        normalized_full: address.normalized_full
    } END) AS addresses
}
RETURN candidate.input_index AS input_index,
       candidate.person_id AS candidate_person_id,
       has_identifiers,
       legacy_identifiers,
       facts,
       addresses
ORDER BY input_index
"""


# --- v2 Person evidence (HAS_IDENTIFIER projection) -----------------------

FIND_ACTIVE_NO_MATCH_LOCKS_V2 = """
UNWIND $candidate_inputs AS candidate_input
MATCH (candidate:Person {person_id: candidate_input.person_id})
UNWIND $identifier_inputs AS identifier_input
MATCH (id:Identifier {
    identifier_type: identifier_input.identifier_type,
    normalized_value: identifier_input.normalized_value
})<-[:HAS_IDENTIFIER]-(owner:Person {status: 'active'})
WHERE owner.person_id <> candidate.person_id
MATCH (owner)-[lock:NO_MATCH_LOCK]-(candidate)
WHERE lock.expires_at IS NULL OR lock.expires_at > datetime()
WITH candidate_input, identifier_input, owner
ORDER BY candidate_input.input_index, identifier_input.input_index, owner.person_id
RETURN candidate_input.person_id AS candidate_person_id,
       owner.person_id AS owner_person_id
"""

PERSON_HAS_IDENTIFIER_V2 = """
MATCH (p:Person {person_id: $person_id})
      -[:HAS_IDENTIFIER]->(id:Identifier {
          identifier_type: $identifier_type,
          normalized_value: $normalized_value
      })
RETURN p.person_id AS person_id
LIMIT 1
"""

PERSON_HAS_VALID_GOVT_ID_V2 = """
MATCH (p:Person {person_id: $person_id})
      -[:HAS_IDENTIFIER]->(id:Identifier {
          identifier_type: 'nric'
      })
WHERE id.normalized_value IN $values
RETURN p.person_id AS person_id
LIMIT 1
"""

PERSON_HAS_CONFLICTING_GOVT_ID_V2 = """
MATCH (p:Person {person_id: $person_id})
      -[:HAS_IDENTIFIER]->(id:Identifier {
          identifier_type: 'nric'
      })
WHERE NOT id.normalized_value IN $values
RETURN id.normalized_value AS conflicting_value
LIMIT 1
"""
