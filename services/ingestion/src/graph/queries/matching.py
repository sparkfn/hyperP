"""Cypher constants for candidate generation, MatchDecision persistence, and ReviewCase creation."""

from __future__ import annotations

# --- Candidate generation -------------------------------------------------

FIND_CANDIDATES_BY_IDENTIFIER = """
MATCH (id:Identifier {
    identifier_type: $identifier_type,
    normalized_value: $normalized_value
})
<-[rel:IDENTIFIED_BY]-(candidate:Person {status: 'active'})
WHERE rel.is_active = true
  AND rel.quality_flag IN ['valid', 'partial_parse']
RETURN DISTINCT candidate.person_id AS person_id
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
WHERE rel.is_active = true
  AND rel.quality_flag IN ['valid', 'partial_parse']
RETURN DISTINCT candidate.person_id AS person_id
"""

CHECK_IDENTIFIER_FANOUT = """
MATCH (id:Identifier {identifier_type: $identifier_type, normalized_value: $normalized_value})
      <-[:IDENTIFIED_BY]-(p:Person {status: 'active'})
RETURN count(p) AS fanout
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
    actions: '[]',
    created_at: datetime(),
    updated_at: datetime()
})-[:FOR_DECISION]->(md)
RETURN rc.review_case_id AS review_case_id
"""
