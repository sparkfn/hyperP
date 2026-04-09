"""Cypher constants for the human review queue (cases, assignment, lock-on-reject)."""

from __future__ import annotations

LIST_REVIEW_CASES = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
WHERE ($queue_state IS NULL OR rc.queue_state = $queue_state)
  AND ($assigned_to IS NULL OR rc.assigned_to = $assigned_to)
  AND ($priority_lte IS NULL OR rc.priority <= $priority_lte)
RETURN rc {
  .review_case_id, .queue_state, .priority, .assigned_to,
  .follow_up_at, .sla_due_at, .resolution, .resolved_at,
  .actions, .created_at, .updated_at
} AS review_case,
md {
  .match_decision_id, .engine_type, .engine_version, .policy_version,
  .decision, .confidence, .reasons, .blocking_conflicts, .created_at
} AS match_decision
ORDER BY rc.priority, rc.sla_due_at, rc.created_at
SKIP $skip LIMIT $limit
"""

GET_REVIEW_CASE = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
OPTIONAL MATCH (md)-[:ABOUT_LEFT]->(left)
OPTIONAL MATCH (md)-[:ABOUT_RIGHT]->(right)
OPTIONAL MATCH (left_addr:Address) WHERE left:Person AND left_addr.address_id = left.preferred_address_id
OPTIONAL MATCH (right_addr:Address) WHERE right:Person AND right_addr.address_id = right.preferred_address_id
RETURN rc {
  .review_case_id, .queue_state, .priority, .assigned_to,
  .follow_up_at, .sla_due_at, .resolution, .resolved_at,
  .actions, .created_at, .updated_at
} AS review_case,
md {
  .match_decision_id, .engine_type, .engine_version, .policy_version,
  .decision, .confidence, .reasons, .blocking_conflicts, .created_at
} AS match_decision,
CASE WHEN left:Person THEN 'person'
     WHEN left:SourceRecord THEN 'source_record'
     ELSE null END AS left_kind,
CASE WHEN left:Person
     THEN left { .person_id, .status, .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob }
     WHEN left:SourceRecord
     THEN left { .source_record_pk, .source_record_id, .normalized_payload, .observed_at }
     ELSE null END AS left_entity,
left_addr { .address_id, .unit_number, .street_number, .street_name, .city, .postal_code, .country_code, .normalized_full } AS left_address,
CASE WHEN right:Person THEN 'person'
     WHEN right:SourceRecord THEN 'source_record'
     ELSE null END AS right_kind,
CASE WHEN right:Person
     THEN right { .person_id, .status, .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob }
     WHEN right:SourceRecord
     THEN right { .source_record_pk, .source_record_id, .normalized_payload, .observed_at }
     ELSE null END AS right_entity,
right_addr { .address_id, .unit_number, .street_number, .street_name, .city, .postal_code, .country_code, .normalized_full } AS right_address
"""

ASSIGN_REVIEW_CASE = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})
WHERE rc.queue_state IN ['open', 'assigned']
SET rc.assigned_to = $assigned_to,
    rc.queue_state = 'assigned',
    rc.updated_at = datetime(),
    rc.actions = rc.actions + [{
      action_type: 'assign',
      actor_type: 'system',
      actor_id: $assigned_to,
      notes: null,
      created_at: toString(datetime())
    }]
RETURN rc {
  .review_case_id, .queue_state, .assigned_to, .priority,
  .follow_up_at, .sla_due_at, .updated_at
} AS review_case
"""

CREATE_NO_MATCH_LOCK_FROM_REVIEW = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT]->(left:Person)
MATCH (md)-[:ABOUT_RIGHT]->(right:Person)
WITH left, right,
     CASE WHEN left.person_id < right.person_id THEN left ELSE right END AS a,
     CASE WHEN left.person_id < right.person_id THEN right ELSE left END AS b
CREATE (a)-[:NO_MATCH_LOCK {
  lock_id: randomUUID(),
  lock_type: 'manual_no_match',
  reason: $notes,
  actor_type: 'reviewer',
  actor_id: 'current_user',
  expires_at: null,
  created_at: datetime()
}]->(b)
"""
