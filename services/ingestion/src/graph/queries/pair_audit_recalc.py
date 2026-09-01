"""Cypher queries for recalculating person-pair (pair-audit) review cases."""

from __future__ import annotations

GET_PERSON_PAIR_REVIEW_CASE = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
WHERE md.engine_type = 'pair_audit'
  AND rc.queue_state IN ['open', 'assigned', 'deferred']
MATCH (md)-[:ABOUT_LEFT {entity_type: 'person'}]->(left:Person)
MATCH (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(right:Person)
RETURN left.person_id AS left_person_id,
       right.person_id AS right_person_id,
       md.feature_snapshot AS feature_snapshot
"""

UPDATE_PAIR_AUDIT_MATCH_DECISION = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
WHERE rc.queue_state IN ['open', 'assigned', 'deferred']
SET md.confidence = $confidence,
    md.decision = $decision,
    md.reasons = $reasons,
    md.feature_snapshot = $feature_snapshot,
    md.engine_version = $engine_version,
    md.policy_version = $policy_version,
    md.updated_at = datetime(),
    rc.updated_at = datetime()
RETURN rc.review_case_id AS review_case_id, rc.queue_state AS queue_state,
       md.confidence AS confidence, md.reasons AS reasons,
       md.feature_snapshot AS feature_snapshot, md.engine_version AS engine_version,
       md.policy_version AS policy_version
"""

READ_PAIR_AUDIT_BRIDGE = """
MATCH (left:Person {person_id: $left_person_id})-[left_link:IDENTIFIED_BY]->(identifier:Identifier)
  <-[right_link:IDENTIFIED_BY]-(right:Person {person_id: $right_person_id})
WHERE coalesce(left_link.is_active, true) = true AND coalesce(right_link.is_active, true) = true
RETURN count(identifier) AS active_bridge_count
"""

CANCEL_STALE_OPEN_PAIR_AUDIT_CASE = """
MATCH (review:ReviewCase {review_case_id: $review_case_id, queue_state: 'open'})-[:FOR_DECISION]->
  (decision:MatchDecision {engine_type: 'pair_audit'})
SET review.queue_state = 'resolved', review.resolution = 'cancelled_stale_repair_bridge',
    review.resolved_at = datetime(), review.updated_at = datetime(), decision.updated_at = datetime()
RETURN review.review_case_id AS review_case_id, review.queue_state AS queue_state,
       review.resolution AS resolution
"""
