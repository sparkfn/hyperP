"""Cypher constants for the human review queue (cases, assignment, lock-on-reject)."""

from __future__ import annotations

from typing import LiteralString

_REVIEW_MATCH = "MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)\n"

# Review actions are stored as a list of JSON strings (Neo4j cannot store maps
# as properties). Legacy nodes hold the literal string '[]', so coerce any
# non-list value to an empty list before appending.
_ACTIONS_AS_LIST: LiteralString = (
    "(CASE WHEN rc.actions IS :: STRING THEN [] ELSE coalesce(rc.actions, []) END)"
)

# Person joins are only added when `q` or `person_id` is present (search needs
# left/right names; the person filter needs their ids). ABOUT_LEFT/ABOUT_RIGHT
# only bind to :Person nodes, so source-record sides yield null and are
# coalesced away. The WITH turns the following filter WHERE into a standalone
# row filter (a bare WHERE after OPTIONAL MATCH would otherwise attach to the
# optional match instead of filtering rows).
_REVIEW_SEARCH_JOINS = """OPTIONAL MATCH (md)-[:ABOUT_LEFT]->(left:Person)
OPTIONAL MATCH (md)-[:ABOUT_RIGHT]->(right:Person)
WITH rc, md, left, right
"""

# Queue states that count as closed for the `resolved` and `overdue_sla` filters.
_CLOSED_STATES = "['resolved', 'cancelled']"

# Non-search filters, shared by list and count. Every condition is a
# parameterised IS NULL guard so absent filters pass harmlessly.
_REVIEW_FILTER_BASE = f"""WHERE ($queue_state IS NULL OR rc.queue_state = $queue_state)
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
           AND NOT rc.queue_state IN {_CLOSED_STATES}))
  AND ($resolved IS NULL OR (rc.queue_state IN {_CLOSED_STATES}) = $resolved)
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

# Appended only when filtering by person; matches either side of the decision.
_REVIEW_PERSON_FILTER = """  AND ($person_id IS NULL
       OR left.person_id = $person_id
       OR right.person_id = $person_id)
"""


def _review_body(*, has_q: bool, has_person: bool) -> str:
    """MATCH + filter prefix shared by the list and count queries.

    ``has_q`` adds the search predicate, ``has_person`` the person-id filter;
    either one pulls in the person joins. Without both, the query never touches
    the ABOUT_LEFT/ABOUT_RIGHT persons.
    """
    parts: list[str] = [_REVIEW_MATCH]
    if has_q or has_person:
        parts.append(_REVIEW_SEARCH_JOINS)
    parts.append(_REVIEW_FILTER_BASE)
    if has_q:
        parts.append(_REVIEW_SEARCH_FILTER)
    if has_person:
        parts.append(_REVIEW_PERSON_FILTER)
    return "".join(parts)


_REVIEW_DISPLAY_JOINS = """OPTIONAL MATCH (md)-[:ABOUT_LEFT]->(left_display:Person)
OPTIONAL MATCH (md)-[:ABOUT_RIGHT]->(right_display:Person)
WITH rc, md, left_display, right_display
"""

_REVIEW_RETURN = """
RETURN rc {
  .review_case_id, .queue_state, .priority, .assigned_to,
  .follow_up_at, .sla_due_at, .resolution, .resolved_at,
  .actions, .created_at, .updated_at
} AS review_case,
md {
  .match_decision_id, .engine_type, .engine_version, .policy_version,
  .decision, .confidence, .reasons, .blocking_conflicts, .created_at
} AS match_decision,
left_display.person_id AS left_person_id,
left_display.preferred_full_name AS left_person_name,
left_display.status AS left_person_status,
right_display.person_id AS right_person_id,
right_display.preferred_full_name AS right_person_name,
right_display.status AS right_person_status
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
    sort_by: str | None, sort_order: str | None, *, has_q: bool, has_person: bool = False
) -> str:
    """Build the paginated list query for ``GET /v1/review-cases``.

    Applies all parameterised filters, then orders by the requested column with
    ``sla_due_at, created_at`` appended as stable tiebreakers (which also
    preserves the historical default ordering when ``sort_by`` is omitted).
    """
    col, direction = _resolve_sort(sort_by, sort_order)
    order_by = f"ORDER BY {col} {direction}, rc.sla_due_at ASC, rc.created_at ASC\n"
    body = _review_body(has_q=has_q, has_person=has_person)
    return body + _REVIEW_DISPLAY_JOINS + _REVIEW_RETURN + order_by + "SKIP $skip LIMIT $limit\n"


def build_count_review_cases_query(*, has_q: bool, has_person: bool = False) -> str:
    """Build the total-count query matching the list query's filters."""
    return _review_body(has_q=has_q, has_person=has_person) + "RETURN count(rc) AS total\n"


GET_REVIEW_CASE = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
OPTIONAL MATCH (md)-[:ABOUT_LEFT]->(left)
OPTIONAL MATCH (md)-[:ABOUT_RIGHT]->(right)
OPTIONAL MATCH (left_addr:Address) WHERE left:Person AND left_addr.address_id = left.preferred_address_id
OPTIONAL MATCH (right_addr:Address) WHERE right:Person AND right_addr.address_id = right.preferred_address_id
OPTIONAL MATCH (sales_o:Order)-[:INVOLVES_UNIT {source_record_pk: left.source_record_pk}]->(sales_u:MachineUnit)
  WHERE left:SourceRecord AND left.record_type = 'sales'
WITH rc, md, left, right, left_addr, right_addr,
     collect(DISTINCT CASE WHEN sales_o IS NOT NULL
       THEN sales_o { .order_id, .order_no, .total_amount, .currency, ordered_at: toString(sales_o.ordered_at) }
       END) AS sales_orders,
     collect(DISTINCT CASE WHEN sales_u IS NOT NULL
       THEN sales_u { .machine_unit_id, .machine_product, .normalized_lta_tag, .normalized_serial_number,
                      conflict_flag: coalesce(sales_u.conflict_flag, false) }
       END) AS sales_units
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
right_addr { .address_id, .unit_number, .street_number, .street_name, .city, .postal_code, .country_code, .normalized_full } AS right_address,
sales_orders[0] AS sales_order,
sales_units AS sales_units
"""

ASSIGN_REVIEW_CASE = (
    """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})
WHERE rc.queue_state IN ['open', 'assigned']
SET rc.assigned_to = $assigned_to,
    rc.queue_state = 'assigned',
    rc.updated_at = datetime(),
    rc.actions = """
    + _ACTIONS_AS_LIST
    + """ + [$action_json]
RETURN rc {
  .review_case_id, .queue_state, .assigned_to, .priority,
  .follow_up_at, .sla_due_at, .updated_at
} AS review_case
"""
)

RECREATE_REVIEW_CASE = """
MATCH (old:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
OPTIONAL MATCH (existing:ReviewCase)-[:FOR_DECISION]->(md)
WHERE existing.queue_state IN ['open', 'assigned', 'deferred']
WITH old, md, collect(existing)[0] AS existing
CALL (old, md, existing) {
  WITH old, md, existing
  WHERE existing IS NOT NULL
  RETURN existing.review_case_id AS review_case_id
  UNION
  WITH old, md, existing
  WHERE existing IS NULL
  CREATE (rc:ReviewCase {
    review_case_id: randomUUID(),
    priority: old.priority,
    queue_state: 'open',
    assigned_to: null,
    follow_up_at: null,
    sla_due_at: old.sla_due_at,
    resolution: null,
    resolved_at: null,
    actions: [$action_json],
    recreated_from_review_case_id: old.review_case_id,
    created_at: datetime(),
    updated_at: datetime()
  })-[:FOR_DECISION]->(md)
  RETURN rc.review_case_id AS review_case_id
}
RETURN review_case_id
"""

GET_REVIEW_CASE_BY_MATCH_DECISION = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(:MatchDecision {match_decision_id: $match_decision_id})
RETURN rc.review_case_id AS review_case_id
ORDER BY rc.updated_at DESC
LIMIT 1
"""

GET_PERSONS_FOR_REVIEW_MERGE = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT]->(left:Person)
MATCH (md)-[:ABOUT_RIGHT]->(right:Person)
RETURN
  left.person_id AS left_person_id,
  right.person_id AS right_person_id,
  size([x IN [left.preferred_full_name, left.preferred_phone, left.preferred_email,
              left.preferred_dob, left.preferred_nric, left.preferred_address_id]
        WHERE x IS NOT NULL]) AS left_completion,
  size([x IN [right.preferred_full_name, right.preferred_phone, right.preferred_email,
              right.preferred_dob, right.preferred_nric, right.preferred_address_id]
        WHERE x IS NOT NULL]) AS right_completion
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
        "rc.actions = " + _ACTIONS_AS_LIST + " + [$action_json]",
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


# ---------------------------------------------------------------------------
# Sales-record approval / rejection — no-op naturally for person-vs-person cases
# because the ABOUT_LEFT SourceRecord MATCH simply finds nothing.
# ---------------------------------------------------------------------------

LINK_REVIEW_SALES_PURCHASED_ORDER = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT]->(sr:SourceRecord {record_type: 'sales'})
MATCH (md)-[:ABOUT_RIGHT]->(p:Person {status: 'active'})
WITH sr, p
MATCH (o:Order)-[:INVOLVES_UNIT {source_record_pk: sr.source_record_pk}]->(:MachineUnit)
WITH DISTINCT sr, p, o
MERGE (p)-[rel:PURCHASED {
    source_system_key: o.source_system_key,
    source_order_id:   o.source_order_id
}]->(o)
ON CREATE SET rel.first_seen_at = datetime(), rel.created_at = datetime()
SET rel.source_record_pk  = sr.source_record_pk,
    rel.last_seen_at      = datetime(),
    rel.last_confirmed_at = datetime()
"""

LINK_REVIEW_SALES_BOUGHT_UNIT = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT]->(sr:SourceRecord {record_type: 'sales'})
MATCH (md)-[:ABOUT_RIGHT]->(p:Person {status: 'active'})
MATCH (o:Order)-[:INVOLVES_UNIT {source_record_pk: sr.source_record_pk}]->(u:MachineUnit)
MERGE (p)-[rel:BOUGHT_UNIT {
    source_system_key: o.source_system_key,
    source_order_id:   o.source_order_id
}]->(u)
ON CREATE SET rel.created_at = datetime(), rel.first_seen_at = datetime()
SET rel.source_record_pk  = sr.source_record_pk,
    rel.last_seen_at      = datetime(),
    rel.last_confirmed_at = datetime(),
    rel.updated_at        = datetime()
"""

MARK_REVIEW_SALES_RECORD_LINKED = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT]->(sr:SourceRecord {record_type: 'sales'})
SET sr.link_status = 'linked',
    sr.updated_at  = datetime()
RETURN sr.source_record_pk AS source_record_pk
"""

MARK_REVIEW_SALES_RECORD_UNRESOLVED = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT]->(sr:SourceRecord {record_type: 'sales'})
SET sr.link_status = 'unresolved',
    sr.updated_at  = datetime()
"""
