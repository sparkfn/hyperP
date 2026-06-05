"""Cypher constants for person↔person review-case detection (shared-identifier bridges)."""

from __future__ import annotations

FIND_PERSONS_SHARING_IDENTIFIER = """
MATCH (id:Identifier {identifier_type: $identifier_type, normalized_value: $normalized_value})
      <-[rel:IDENTIFIED_BY]-(p:Person {status: 'active'})
WHERE rel.is_active = true
  AND rel.quality_flag IN ['valid', 'partial_parse']
RETURN collect(DISTINCT p.person_id) AS person_ids
"""

CHECK_OPEN_PERSON_PAIR_CASE = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT {entity_type: 'person'}]->(a:Person)
MATCH (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(b:Person)
WHERE rc.queue_state IN ['open', 'assigned', 'deferred']
  AND ((a.person_id = $left_person_id AND b.person_id = $right_person_id)
    OR (a.person_id = $right_person_id AND b.person_id = $left_person_id))
RETURN rc.review_case_id AS review_case_id
LIMIT 1
"""

CREATE_PERSON_PAIR_REVIEW_CASE = """
MATCH (a:Person {person_id: $left_person_id})
MATCH (b:Person {person_id: $right_person_id})
CREATE (md:MatchDecision {
    match_decision_id: randomUUID(),
    engine_type: 'pair_audit',
    engine_version: $engine_version,
    decision: 'review',
    confidence: 0.0,
    reasons: $reasons,
    blocking_conflicts: [],
    feature_snapshot: $feature_snapshot,
    policy_version: $policy_version,
    created_at: datetime(),
    retention_expires_at: null
})
CREATE (md)-[:ABOUT_LEFT {entity_type: 'person'}]->(a)
CREATE (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(b)
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
