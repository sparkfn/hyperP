"""Cypher for person-pair auto-merge: pair attribute lookup and review-case
redirects on the absorbed person, ported from the API's merge repository
(``services/api/src/graph/queries/merge.py``) so an ingestion-driven merge
keeps the review queue consistent the same way a human-triggered merge does.
"""

from __future__ import annotations

FETCH_PAIR_MERGE_ATTRS = """
MATCH (a:Person {person_id: $left_person_id})
MATCH (b:Person {person_id: $right_person_id})
RETURN a.person_id AS left_person_id,
       a.status AS left_status,
       a.profile_completeness_score AS left_completeness,
       toString(a.created_at) AS left_created_at,
       b.person_id AS right_person_id,
       b.status AS right_status,
       b.profile_completeness_score AS right_completeness,
       toString(b.created_at) AS right_created_at
"""

CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT {entity_type: 'person'}]->(a:Person)
MATCH (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(b:Person)
WHERE rc.queue_state IN ['open', 'assigned', 'deferred']
  AND (
    (a.person_id = $absorbed_id AND b.person_id = $survivor_id)
    OR (b.person_id = $absorbed_id AND a.person_id = $survivor_id)
  )
SET rc.queue_state = 'cancelled',
    rc.resolution = 'cancelled_superseded',
    rc.resolved_at = datetime(),
    rc.closed_by_merge_event_id = $merge_event_id,
    rc.updated_at = datetime()
"""

REDIRECT_PERSON_PAIR_CASES_ABSORBED_LEFT = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[old_left:ABOUT_LEFT {entity_type: 'person'}]->(:Person {person_id: $absorbed_id})
MATCH (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(other:Person)
WHERE rc.queue_state IN ['open', 'assigned', 'deferred']
  AND other.person_id <> $survivor_id
MATCH (survivor:Person {person_id: $survivor_id})
CREATE (md)-[:ABOUT_LEFT {entity_type: 'person'}]->(survivor)
DELETE old_left
SET rc.redirected_pair_by_merge_event_id = $merge_event_id,
    rc.redirected_pair_from_person_id = $absorbed_id,
    rc.redirected_pair_side = 'left',
    rc.updated_at = datetime()
RETURN rc.review_case_id AS review_case_id
"""

REDIRECT_PERSON_PAIR_CASES_ABSORBED_RIGHT = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT {entity_type: 'person'}]->(other:Person)
MATCH (md)-[old_right:ABOUT_RIGHT {entity_type: 'person'}]->(:Person {person_id: $absorbed_id})
WHERE rc.queue_state IN ['open', 'assigned', 'deferred']
  AND other.person_id <> $survivor_id
MATCH (survivor:Person {person_id: $survivor_id})
CREATE (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(survivor)
DELETE old_right
SET rc.redirected_pair_by_merge_event_id = $merge_event_id,
    rc.redirected_pair_from_person_id = $absorbed_id,
    rc.redirected_pair_side = 'right',
    rc.updated_at = datetime()
RETURN rc.review_case_id AS review_case_id
"""

REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
WHERE rc.queue_state IN ['open', 'assigned', 'deferred']
MATCH (md)-[old_right:ABOUT_RIGHT {entity_type: 'person'}]->(absorbed:Person {person_id: $absorbed_id})
MATCH (md)-[:ABOUT_LEFT {entity_type: 'source_record'}]->(:SourceRecord)
MATCH (survivor:Person {person_id: $survivor_id})
CREATE (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(survivor)
DELETE old_right
SET rc.redirected_by_merge_event_id = $merge_event_id,
    rc.redirected_from_person_id = $absorbed_id,
    rc.updated_at = datetime()
"""
