"""Cypher constants for the human review queue (cases, assignment, lock-on-reject)."""

from __future__ import annotations

from typing import LiteralString

_REVIEW_MATCH = "MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)\n"

# Person joins are only added when `q` is present (search needs left/right
# names). ABOUT_LEFT/ABOUT_RIGHT only bind to :Person nodes, so source-record
# sides yield null and are coalesced away. The WITH turns the following filter
# WHERE into a standalone row filter (a bare WHERE after OPTIONAL MATCH would
# otherwise attach to the optional match instead of filtering rows).
_REVIEW_SEARCH_JOINS = """OPTIONAL MATCH (md)-[:ABOUT_LEFT]->(left:Person)
OPTIONAL MATCH (md)-[:ABOUT_RIGHT]->(right:Person)
WITH rc, md, left, right
"""

# Non-search filters, shared by list and count. Every condition is a
# parameterised IS NULL guard so absent filters pass harmlessly.
_REVIEW_FILTER_BASE = """WHERE ($queue_state IS NULL OR rc.queue_state = $queue_state)
  AND ($assigned_to IS NULL OR rc.assigned_to = $assigned_to)
  AND ($priority_lte IS NULL OR rc.priority <= $priority_lte)
  AND ($priority_gte IS NULL OR rc.priority >= $priority_gte)
  AND ($decision IS NULL OR md.decision = $decision)
  AND ($engine_type IS NULL OR md.engine_type = $engine_type)
  AND ($confidence_gte IS NULL OR md.confidence >= $confidence_gte)
  AND ($confidence_lte IS NULL OR md.confidence <= $confidence_lte)
  AND ($created_after  IS NULL OR rc.created_at >= datetime($created_after))
  AND ($created_before IS NULL OR rc.created_at <= datetime($created_before))
  AND ($sla_due_after  IS NULL OR rc.sla_due_at >= datetime($sla_due_after))
  AND ($sla_due_before IS NULL OR rc.sla_due_at <= datetime($sla_due_before))
  AND ($overdue_sla IS NULL OR $overdue_sla = false
       OR (rc.sla_due_at IS NOT NULL AND rc.sla_due_at < datetime()
           AND NOT rc.queue_state IN ['resolved', 'cancelled']))
"""

# Appended only when searching; references the joined left/right persons.
_REVIEW_SEARCH_FILTER = """  AND ($q IS NULL
       OR toLower(rc.review_case_id) CONTAINS toLower($q)
       OR toLower(md.decision) CONTAINS toLower($q)
       OR toLower(md.engine_type) CONTAINS toLower($q)
       OR toLower(coalesce(rc.assigned_to, '')) CONTAINS toLower($q)
       OR toLower(coalesce(left.preferred_full_name, '')) CONTAINS toLower($q)
       OR toLower(coalesce(left.preferred_phone, '')) CONTAINS toLower($q)
       OR toLower(coalesce(left.preferred_email, '')) CONTAINS toLower($q)
       OR toLower(coalesce(right.preferred_full_name, '')) CONTAINS toLower($q)
       OR toLower(coalesce(right.preferred_phone, '')) CONTAINS toLower($q)
       OR toLower(coalesce(right.preferred_email, '')) CONTAINS toLower($q))
"""


def _review_body(*, has_q: bool) -> str:
    """MATCH + filter prefix shared by the list and count queries.

    With ``has_q`` the person joins and the search predicate are included;
    without it the query never touches the ABOUT_LEFT/ABOUT_RIGHT persons.
    """
    if has_q:
        return _REVIEW_MATCH + _REVIEW_SEARCH_JOINS + _REVIEW_FILTER_BASE + _REVIEW_SEARCH_FILTER
    return _REVIEW_MATCH + _REVIEW_FILTER_BASE


_REVIEW_RETURN = """
RETURN rc {
  .review_case_id, .queue_state, .priority, .assigned_to,
  .follow_up_at, .sla_due_at, .resolution, .resolved_at,
  .actions, .created_at, .updated_at
} AS review_case,
md {
  .match_decision_id, .engine_type, .engine_version, .policy_version,
  .decision, .confidence, .reasons, .blocking_conflicts, .created_at
} AS match_decision
"""

# Whitelist: API sort key -> Cypher ORDER BY expression. Anything outside this
# map is rejected with HTTP 400 by the route before reaching the builder.
_SORT_COLUMNS: dict[str, str] = {
    "priority": "rc.priority",
    "confidence": "md.confidence",
    "sla_due_at": "rc.sla_due_at",
    "created_at": "rc.created_at",
    "updated_at": "rc.updated_at",
    "queue_state": "rc.queue_state",
}

# Public view of the sortable keys, used by the route for request validation.
REVIEW_SORT_KEYS: frozenset[str] = frozenset(_SORT_COLUMNS)

_DEFAULT_SORT_KEY = "priority"
_DEFAULT_ORDER: dict[str, str] = {
    "priority": "ASC",
    "confidence": "DESC",
    "sla_due_at": "ASC",
    "created_at": "DESC",
    "updated_at": "DESC",
    "queue_state": "ASC",
}


def _resolve_sort(sort_by: str | None, sort_order: str | None) -> tuple[str, str]:
    key = sort_by if sort_by in _SORT_COLUMNS else _DEFAULT_SORT_KEY
    if sort_order is not None:
        direction = "DESC" if sort_order.upper() == "DESC" else "ASC"
    else:
        direction = _DEFAULT_ORDER[key]
    return _SORT_COLUMNS[key], direction


def build_list_review_cases_query(
    sort_by: str | None, sort_order: str | None, *, has_q: bool
) -> str:
    """Build the paginated list query for ``GET /v1/review-cases``.

    Applies all parameterised filters, then orders by the requested column with
    ``sla_due_at, created_at`` appended as stable tiebreakers (which also
    preserves the historical default ordering when ``sort_by`` is omitted).
    """
    col, direction = _resolve_sort(sort_by, sort_order)
    order_by = f"ORDER BY {col} {direction}, rc.sla_due_at ASC, rc.created_at ASC\n"
    return _review_body(has_q=has_q) + _REVIEW_RETURN + order_by + "SKIP $skip LIMIT $limit\n"


def build_count_review_cases_query(*, has_q: bool) -> str:
    """Build the total-count query matching the list query's filters."""
    return _review_body(has_q=has_q) + "RETURN count(rc) AS total\n"


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

GET_PERSONS_FOR_REVIEW_MERGE = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT]->(left:Person)
MATCH (md)-[:ABOUT_RIGHT]->(right:Person)
RETURN left.person_id AS left_person_id, right.person_id AS right_person_id
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
  actor_id: $actor_id,
  expires_at: null,
  created_at: datetime()
}]->(b)
"""


def build_review_action_cypher(
    resolution: str | None,
    follow_up_at: str | None,
) -> LiteralString:
    """Build the SET clause for a review-case action."""
    clauses: list[LiteralString] = [
        "rc.queue_state = $new_state",
        "rc.updated_at = datetime()",
        "rc.actions = rc.actions + [{"
        " action_type: $action_type, actor_type: 'reviewer', actor_id: $actor_id,"
        " notes: $notes, created_at: toString(datetime())}]",
    ]
    if resolution is not None:
        clauses.append("rc.resolution = $resolution")
        clauses.append("rc.resolved_at = datetime()")
    if follow_up_at is not None:
        clauses.append("rc.follow_up_at = datetime($follow_up_at)")
    joined: LiteralString = ", ".join(clauses)
    return (
        "MATCH (rc:ReviewCase {review_case_id: $review_case_id}) "
        "WHERE rc.queue_state IN ['open', 'assigned', 'deferred'] "
        "SET " + joined + " "
        "RETURN rc {.review_case_id, .queue_state, .resolution} AS review_case"
    )
