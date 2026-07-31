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

# Person-pair cases have no involved SourceRecord and remain actionable. If a
# case does involve source evidence, every such record must still be pending or
# be an explicitly-latest legacy record awaiting lifecycle backfill.
# Replacement/rejection therefore removes stale cases from the queue and makes
# concurrent actions naturally non-applicable. Missing ``is_latest`` is not
# accepted: legacy eligibility requires an explicit true value.
_INVOLVED_SOURCE_IS_PENDING = """NOT EXISTS {
    MATCH (md)-[:ABOUT_LEFT|ABOUT_RIGHT]->(involved:SourceRecord)
    WHERE (involved.lifecycle_status IS NOT NULL
           AND involved.lifecycle_status <> 'pending_review')
       OR (involved.lifecycle_status IS NULL
           AND coalesce(involved.is_latest, false) = false)
  }
"""

# Non-search filters, shared by list and count. Every condition is a
# parameterised IS NULL guard so absent filters pass harmlessly.
_REVIEW_FILTER_BASE = f"""WHERE {_INVOLVED_SOURCE_IS_PENDING.rstrip()}
  AND ($queue_state IS NULL OR rc.queue_state = $queue_state)
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
OPTIONAL MATCH (left)-[:FROM_SOURCE]->(left_src:SourceSystem)
OPTIONAL MATCH (right)-[:FROM_SOURCE]->(right_src:SourceSystem)
OPTIONAL MATCH (sales_o:Order)-[:INVOLVES_VEHICLE {source_record_pk: left.source_record_pk}]->(sales_v:Vehicle)
  WHERE left:SourceRecord AND left.record_type = 'sales'
WITH rc, md, left, right, left_addr, right_addr, left_src, right_src,
     collect(DISTINCT CASE WHEN sales_o IS NOT NULL
       THEN sales_o { .order_id, .order_no, .total_amount, .currency, .non_vehicle_lines, ordered_at: toString(sales_o.ordered_at) }
       END) AS sales_orders,
     collect(DISTINCT CASE WHEN sales_v IS NOT NULL
       THEN sales_v { .vehicle_id, .product, .product_sku,
                      .normalized_lta_tag, .normalized_serial_number,
                      conflict_flag: coalesce(sales_v.conflict_flag, false) }
       END) AS sales_vehicles
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
     THEN left { .source_record_pk, .source_record_id, .normalized_payload, .observed_at,
                 .linked_person_id, .record_type, source_system_key: left_src.source_key }
     ELSE null END AS left_entity,
left_addr { .address_id, .unit_number, .street_number, .street_name, .city, .postal_code, .country_code, .normalized_full } AS left_address,
CASE WHEN right:Person THEN 'person'
     WHEN right:SourceRecord THEN 'source_record'
     ELSE null END AS right_kind,
CASE WHEN right:Person
     THEN right { .person_id, .status, .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob }
     WHEN right:SourceRecord
     THEN right { .source_record_pk, .source_record_id, .normalized_payload, .observed_at,
                  .linked_person_id, .record_type, source_system_key: right_src.source_key }
     ELSE null END AS right_entity,
right_addr { .address_id, .unit_number, .street_number, .street_name, .city, .postal_code, .country_code, .normalized_full } AS right_address,
sales_orders[0] AS sales_order,
sales_vehicles AS sales_vehicles,
sales_orders[0].non_vehicle_lines AS non_vehicle_lines
"""

ASSIGN_REVIEW_CASE = (
    """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})
MATCH (rc)-[:FOR_DECISION]->(md:MatchDecision)
WHERE rc.queue_state IN ['open', 'assigned']
  AND """
    + _INVOLVED_SOURCE_IS_PENDING
    + """
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
        "MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision) "
        "WHERE rc.queue_state IN ['open', 'assigned', 'deferred'] "
        "AND " + _INVOLVED_SOURCE_IS_PENDING + " "
        "SET " + joined + " "
        "RETURN rc {.review_case_id, .queue_state, .resolution} AS review_case"
    )


def build_claimed_review_action_cypher(
    resolution: str | None,
    follow_up_at: str | None,
) -> LiteralString:
    """Build a close guarded by the stable claim acquired before side effects."""
    query = build_review_action_cypher(resolution, follow_up_at)
    eligibility = "WHERE rc.queue_state IN ['open', 'assigned', 'deferred'] AND "
    claim_guard = (
        "WHERE rc.queue_state = $claim_status "
        "AND rc.lifecycle_claim_token = $claim_token "
        "AND rc.lifecycle_claim_version = $claim_version "
        "AND rc.lifecycle_claim_status = $claim_status "
        "AND rc.lifecycle_claimed_by = $actor_id AND "
    )
    return query.replace(eligibility + _INVOLVED_SOURCE_IS_PENDING + " ", claim_guard)


# ---------------------------------------------------------------------------
# Sales-record approval / rejection — no-op naturally for person-vs-person cases
# because the ABOUT_LEFT SourceRecord MATCH simply finds nothing.
# ---------------------------------------------------------------------------

LINK_REVIEW_SALES_PURCHASED_ORDER = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT]->(sr:SourceRecord {record_type: 'sales'})
MATCH (md)-[:ABOUT_RIGHT]->(p:Person {status: 'active'})
WITH sr, p
MATCH (o:Order)-[:INVOLVES_VEHICLE {source_record_pk: sr.source_record_pk}]->(:Vehicle)
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

LINK_REVIEW_SALES_BOUGHT_VEHICLE = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT]->(sr:SourceRecord {record_type: 'sales'})
MATCH (md)-[:ABOUT_RIGHT]->(p:Person {status: 'active'})
MATCH (o:Order)-[:INVOLVES_VEHICLE {source_record_pk: sr.source_record_pk}]->(v:Vehicle)
MERGE (p)-[rel:BOUGHT_VEHICLE {
    source_system_key: o.source_system_key,
    source_order_id:   o.source_order_id
}]->(v)
ON CREATE SET rel.created_at = datetime(), rel.first_seen_at = datetime()
SET rel.source_record_pk  = sr.source_record_pk,
    rel.last_seen_at      = datetime(),
    rel.last_confirmed_at = datetime(),
    rel.updated_at        = datetime()
"""

MARK_REVIEW_SALES_RECORD_LINKED = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT]->(sr:SourceRecord {record_type: 'sales'})
MATCH (md)-[:ABOUT_RIGHT]->(person:Person {status: 'active'})
SET sr.link_status = 'linked',
    sr.updated_at  = datetime(),
    person.analysis_input_revision = coalesce(person.analysis_input_revision, 0) + 1,
    person.analysis_dirty_at = datetime()
RETURN sr.source_record_pk AS source_record_pk
"""

MARK_REVIEW_SALES_RECORD_UNRESOLVED = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT]->(sr:SourceRecord {record_type: 'sales'})
SET sr.link_status = 'unresolved',
    sr.updated_at  = datetime()
"""

GET_REVIEW_SALES_RECORD = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT]->(sr:SourceRecord {record_type: 'sales'})
RETURN sr.source_record_pk AS source_record_pk,
       sr.lifecycle_status AS lifecycle_status,
       coalesce(sr.staged_sales_ready, false) AS staged_sales_ready
"""

PRECHECK_STAGED_REVIEW_SALE = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
WHERE rc.lifecycle_claim_token = $claim_token
  AND rc.lifecycle_claim_version = $claim_version
  AND rc.lifecycle_claim_status = $claim_status
  AND rc.lifecycle_claimed_by = $actor_id
MATCH (md)-[:ABOUT_LEFT]->(sr:SourceRecord {
  record_type: 'sales', lifecycle_status: 'pending_review', staged_sales_ready: true
})-[:FROM_SOURCE]->(source:SourceSystem)
MATCH (md)-[:ABOUT_RIGHT]->(:Person {status: 'active'})
SET sr.sales_stage_lock_version = coalesce(sr.sales_stage_lock_version, 0) + 1
MATCH (stage:StagedSalesOrder {stage_order_key: sr.source_record_pk,
                               source_record_pk: sr.source_record_pk})
WHERE stage.source_system_key = source.source_key
  AND stage.stage_hash = sr.staged_sales_hash
  AND stage.expected_line_count = sr.staged_sales_line_count
  AND stage.expected_observation_count = sr.staged_sales_observation_count
  AND stage.source_order_id IS NOT NULL AND stage.source_order_id <> ''
  AND stage.entity_key IS NOT NULL AND stage.entity_key <> ''
SET stage.lock_version = coalesce(stage.lock_version, 0) + 1
MATCH (stage)-[:STAGED_CONTAINS]->(line:StagedSalesLine)
WITH sr, source, stage, line ORDER BY line.line_index
WITH sr, source, stage, collect(line) AS lines
WHERE size(lines) = stage.expected_line_count
  AND all(item IN lines WHERE item.stage_line_key = sr.source_record_pk + ':' +
    toString(item.line_index) AND item.line_hash IS NOT NULL AND item.line_hash <> ''
    AND item.source_line_item_id IS NOT NULL AND item.source_line_item_id <> ''
    AND item.source_product_id IS NOT NULL AND item.source_product_id <> '')
  AND all(item IN lines WHERE single(other IN lines WHERE
      other.line_index = item.line_index))
  AND all(item IN lines WHERE single(other IN lines WHERE
      other.source_line_item_id = item.source_line_item_id))
OPTIONAL MATCH (stage)-[:STAGED_OBSERVATION]->(observation:StagedSalesVehicleObservation)
WITH sr, stage, lines, observation ORDER BY observation.observation_index
WITH sr, stage, lines, [item IN collect(observation) WHERE item IS NOT NULL] AS observations
WHERE size(observations) = stage.expected_observation_count
  AND all(item IN observations WHERE
    item.stage_observation_key = sr.source_record_pk + ':' + toString(item.observation_index)
    AND item.observation_hash IS NOT NULL AND item.observation_hash <> ''
    AND item.source_system_key = stage.source_system_key
    AND item.source_record_id = sr.source_record_id
    AND (item.normalized_lta_tag IS NOT NULL OR
         (item.normalized_serial_number IS NOT NULL AND item.product_sku IS NOT NULL)))
  AND all(item IN observations WHERE single(other IN observations WHERE
      other.observation_index = item.observation_index))
RETURN sr.source_record_pk AS source_record_pk,
       sr.source_record_id AS source_record_id,
       stage.source_system_key AS source_system_key,
       sr.sales_stage_lock_version AS source_lock_version,
       stage.lock_version AS lock_version,
       stage.order_hash AS order_hash,
       stage.stage_hash AS stage_hash,
       size(lines) AS expected_line_count,
       size(observations) AS expected_observation_count,
       {source_order_id: stage.source_order_id, entity_key: stage.entity_key,
        order_no: stage.order_no, ordered_at: stage.ordered_at,
        release_date: stage.release_date, status: stage.status,
        total_amount: stage.total_amount, currency: stage.currency,
        item_count: stage.item_count, metadata: stage.metadata,
        non_vehicle_lines: stage.non_vehicle_lines,
        points_used: stage.points_used, points_gained: stage.points_gained,
        did_redeem_discount: stage.did_redeem_discount,
        is_purchase_points: stage.is_purchase_points} AS order,
       [item IN lines | {line_index: item.line_index, line_hash: item.line_hash,
        source_line_item_id: item.source_line_item_id,
        source_product_id: item.source_product_id, line_no: item.line_no,
        quantity: item.quantity, unit_price: item.unit_price,
        line_total: item.line_total, currency: item.currency,
        discount_amount: item.discount_amount, tax_amount: item.tax_amount,
        metadata: item.metadata, product_sku: item.product_sku,
        product_name: item.product_name, product_display_name: item.product_display_name,
        product_category: item.product_category,
        product_subcategory: item.product_subcategory,
        product_manufacturer: item.product_manufacturer,
        product_attributes: item.product_attributes,
        product_is_active: item.product_is_active}] AS lines,
       [item IN observations | {observation_index: item.observation_index,
        observation_hash: item.observation_hash,
        source_system_key: item.source_system_key, source_record_id: item.source_record_id,
        product_sku: item.product_sku, product: item.product,
        manufacturer: item.manufacturer, model: item.model, unit_label: item.unit_label,
        lta_tag: item.lta_tag, normalized_lta_tag: item.normalized_lta_tag,
        serial_number: item.serial_number,
        normalized_serial_number: item.normalized_serial_number,
        source_kind: item.source_kind, observed_at: item.observed_at,
        confidence: item.confidence, quality_flag: item.quality_flag,
        raw_context: item.raw_context}] AS observations
"""

PROMOTE_STAGED_REVIEW_SALE = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
WHERE rc.lifecycle_claim_token = $claim_token
  AND rc.lifecycle_claim_version = $claim_version
  AND rc.lifecycle_claim_status = $claim_status
  AND rc.lifecycle_claimed_by = $actor_id
MATCH (md)-[:ABOUT_LEFT]->(sr:SourceRecord {
  record_type: 'sales', lifecycle_status: 'pending_review', staged_sales_ready: true
})-[:FROM_SOURCE]->(source:SourceSystem)
WHERE sr.sales_stage_lock_version = $source_lock_version
MATCH (md)-[:ABOUT_RIGHT]->(person:Person {status: 'active'})
MATCH (stage:StagedSalesOrder {stage_order_key: sr.source_record_pk,
                               source_record_pk: sr.source_record_pk,
                               lock_version: $stage_lock_version,
                               stage_hash: $stage_hash})
WHERE stage.source_system_key = source.source_key
  AND stage.expected_line_count = $expected_line_count
  AND stage.expected_observation_count = $expected_observation_count
MATCH (stage)-[:STAGED_CONTAINS]->(staged_line:StagedSalesLine)
WITH sr, source, person, stage, collect(DISTINCT staged_line) AS staged_lines
OPTIONAL MATCH (stage)-[:STAGED_OBSERVATION]->(observation:StagedSalesVehicleObservation)
WITH sr, source, person, stage, staged_lines,
     [item IN collect(observation) WHERE item IS NOT NULL] AS observations
WHERE size(staged_lines) = stage.expected_line_count
  AND size(observations) = stage.expected_observation_count
OPTIONAL MATCH (old:SourceRecord {source_record_id: sr.source_record_id})-[:FROM_SOURCE]->(source)
WHERE old <> sr AND (old.lifecycle_status = 'active'
  OR (old.lifecycle_status IS NULL AND old.is_latest = true))
WITH sr, source, person, stage, staged_lines, observations, collect(old) AS old_versions
WHERE (sr.expected_active_source_record_pk IS NULL AND size(old_versions) = 0)
   OR (sr.expected_active_source_record_pk IS NOT NULL AND size(old_versions) = 1
       AND old_versions[0].source_record_pk = sr.expected_active_source_record_pk)
WITH sr, source, person, stage, staged_lines, observations, old_versions
CALL (old_versions) {
  UNWIND old_versions AS old
  OPTIONAL MATCH (old_owner)-[rel:PURCHASED|BOUGHT_VEHICLE|INVOLVES_VEHICLE]->()
  WHERE rel.source_record_pk = old.source_record_pk
  FOREACH (_ IN CASE WHEN old_owner:Person THEN [1] ELSE [] END |
    SET old_owner.analysis_input_revision =
          coalesce(old_owner.analysis_input_revision, 0) + 1,
        old_owner.analysis_dirty_at = datetime()
  )
  DELETE rel
  RETURN count(rel) AS retired_count
}
MERGE (order:Order {source_system_key: source.source_key,
                    source_order_id: stage.source_order_id})
ON CREATE SET order.order_id = randomUUID(), order.created_at = datetime()
SET order.order_no = stage.order_no, order.ordered_at = stage.ordered_at,
    order.release_date = stage.release_date, order.status = stage.status,
    order.total_amount = stage.total_amount, order.currency = stage.currency,
    order.item_count = stage.item_count, order.metadata = stage.metadata,
    order.points_used = stage.points_used, order.points_gained = stage.points_gained,
    order.did_redeem_discount = stage.did_redeem_discount,
    order.is_purchase_points = stage.is_purchase_points,
    order.non_vehicle_lines = stage.non_vehicle_lines, order.updated_at = datetime()
MERGE (order)-[:SOLD_THROUGH]->(source)
WITH sr, source, person, stage, staged_lines, observations, order
OPTIONAL MATCH (order)-[stale_contains:CONTAINS]->(stale_line:LineItem)
OPTIONAL MATCH (stale_line)-[stale_product:OF_PRODUCT]->(:Product)
DELETE stale_contains, stale_product
WITH DISTINCT sr, source, person, stage, staged_lines, observations, order
CALL (source, sr, stage, order, staged_lines) {
  UNWIND staged_lines AS line
  MATCH (entity:Entity {entity_key: stage.entity_key})
  MERGE (product:Product {source_system_key: source.source_key,
                          source_product_id: line.source_product_id})
  ON CREATE SET product.product_id = randomUUID(), product.created_at = datetime(),
                product.first_seen_at = datetime()
  SET product.sku = line.product_sku, product.name = line.product_name,
      product.display_name = line.product_display_name,
      product.category = line.product_category,
      product.subcategory = line.product_subcategory,
      product.manufacturer = line.product_manufacturer,
      product.attributes = line.product_attributes,
      product.is_active = line.product_is_active,
      product.updated_at = datetime(), product.last_seen_at = datetime()
  MERGE (product)-[:SOLD_BY]->(entity)
  MERGE (canonical:LineItem {source_system_key: source.source_key,
                             source_line_item_id: line.source_line_item_id})
  ON CREATE SET canonical.line_item_id = randomUUID(), canonical.created_at = datetime()
  SET canonical.line_no = line.line_no, canonical.quantity = line.quantity,
      canonical.unit_price = line.unit_price, canonical.line_total = line.line_total,
      canonical.currency = line.currency, canonical.discount_amount = line.discount_amount,
      canonical.tax_amount = line.tax_amount, canonical.metadata = line.metadata
  WITH source, sr, order, line, product, canonical
  OPTIONAL MATCH (:Order)-[prior_contains:CONTAINS]->(canonical)
  DELETE prior_contains
  WITH DISTINCT source, sr, order, line, product, canonical
  OPTIONAL MATCH (canonical)-[prior_product:OF_PRODUCT]->(:Product)
  DELETE prior_product
  WITH DISTINCT sr, order, product, canonical
  MERGE (order)-[:CONTAINS {source_record_pk: sr.source_record_pk}]->(canonical)
  MERGE (canonical)-[:OF_PRODUCT {source_record_pk: sr.source_record_pk}]->(product)
  RETURN count(DISTINCT canonical) AS promoted_line_count
}
CALL (source, sr, person, stage, order, observations) {
  UNWIND observations AS observation
  OPTIONAL MATCH (lta_match:Vehicle)
    WHERE observation.normalized_lta_tag IS NOT NULL
      AND lta_match.normalized_lta_tag = observation.normalized_lta_tag
  OPTIONAL MATCH (serial_match:Vehicle)
    WHERE observation.normalized_serial_number IS NOT NULL
      AND source.source_key IN serial_match.source_systems
      AND serial_match.normalized_serial_number = observation.normalized_serial_number
      AND (observation.product_sku IN coalesce(serial_match.observed_product_skus_s, [])
           OR serial_match.product_sku = observation.product_sku)
  WITH source, sr, person, stage, order, observation, lta_match, serial_match,
       coalesce(lta_match, serial_match) AS matched,
       lta_match IS NOT NULL AND serial_match IS NOT NULL AND
         lta_match <> serial_match AS identifier_conflict
  FOREACH (_ IN CASE WHEN identifier_conflict THEN [1] ELSE [] END |
    SET lta_match.conflict_flag = true, lta_match.conflict_reason = 'identifier_conflict',
        serial_match.conflict_flag = true,
        serial_match.conflict_reason = 'identifier_conflict')
  CALL (matched, observation) {
    WITH matched, observation WHERE matched IS NOT NULL RETURN matched AS vehicle
    UNION
    WITH matched, observation WHERE matched IS NULL
    CREATE (vehicle:Vehicle {vehicle_id: randomUUID(), created_at: observation.observed_at})
    RETURN vehicle
  }
  FOREACH (_ IN CASE WHEN identifier_conflict THEN [] ELSE [1] END |
    SET vehicle.normalized_lta_tag = coalesce(vehicle.normalized_lta_tag,
        observation.normalized_lta_tag),
      vehicle.normalized_serial_number = coalesce(vehicle.normalized_serial_number,
        observation.normalized_serial_number),
      vehicle.lta_tag = coalesce(vehicle.lta_tag, observation.lta_tag),
      vehicle.serial_number = coalesce(vehicle.serial_number, observation.serial_number),
      vehicle.product_sku = coalesce(vehicle.product_sku, observation.product_sku),
      vehicle.product = coalesce(vehicle.product, observation.product),
      vehicle.manufacturer = coalesce(vehicle.manufacturer, observation.manufacturer),
      vehicle.model = coalesce(vehicle.model, observation.model),
      vehicle.source_systems = CASE WHEN source.source_key IN coalesce(vehicle.source_systems, [])
        THEN vehicle.source_systems ELSE coalesce(vehicle.source_systems, []) + [source.source_key] END,
      vehicle.observed_product_skus_s = CASE WHEN observation.product_sku IS NULL OR
        observation.product_sku IN coalesce(vehicle.observed_product_skus_s, [])
        THEN vehicle.observed_product_skus_s
        ELSE coalesce(vehicle.observed_product_skus_s, []) + [observation.product_sku] END)
  SET vehicle.updated_at = observation.observed_at,
      vehicle.conflict_flag = coalesce(vehicle.conflict_flag, identifier_conflict),
      vehicle.conflict_reason = CASE WHEN identifier_conflict THEN 'identifier_conflict'
        ELSE vehicle.conflict_reason END
  MERGE (order)-[involves:INVOLVES_VEHICLE {source_system_key: source.source_key,
    source_record_pk: sr.source_record_pk,
    observation_index: observation.observation_index}]->(vehicle)
  SET involves.raw_context = observation.raw_context,
      involves.observed_at = observation.observed_at,
      involves.confidence = observation.confidence,
      involves.quality_flag = observation.quality_flag
  MERGE (person)-[bought:BOUGHT_VEHICLE {source_system_key: source.source_key,
    source_order_id: stage.source_order_id,
    observation_index: observation.observation_index}]->(vehicle)
  SET bought.source_record_pk = sr.source_record_pk, bought.is_active = true,
      bought.raw_context = observation.raw_context,
      bought.observed_at = observation.observed_at,
      bought.confidence = observation.confidence,
      bought.quality_flag = observation.quality_flag, bought.updated_at = datetime()
  RETURN count(observation) AS promoted_observation_count
}
MERGE (person)-[purchase:PURCHASED {source_system_key: source.source_key,
  source_order_id: stage.source_order_id}]->(order)
SET purchase.source_record_pk = sr.source_record_pk, purchase.is_active = true,
    purchase.updated_at = datetime(),
    person.analysis_input_revision = coalesce(person.analysis_input_revision, 0) + 1,
    person.analysis_dirty_at = datetime()
RETURN sr.source_record_pk AS source_record_pk,
       promoted_line_count, promoted_observation_count
"""

FINALIZE_STAGED_REVIEW_SALE = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
WHERE rc.lifecycle_claim_token = $claim_token
  AND rc.lifecycle_claim_version = $claim_version
  AND rc.lifecycle_claim_status = $claim_status
  AND rc.lifecycle_claimed_by = $actor_id
MATCH (md)-[:ABOUT_LEFT]->(sr:SourceRecord {
  record_type: 'sales', lifecycle_status: 'pending_review', staged_sales_ready: true
})-[:FROM_SOURCE]->(source:SourceSystem)
WHERE sr.sales_stage_lock_version = $source_lock_version
MATCH (stage:StagedSalesOrder {stage_order_key: sr.source_record_pk,
                               source_record_pk: sr.source_record_pk,
                               lock_version: $stage_lock_version,
                               stage_hash: $stage_hash})
WHERE stage.expected_line_count = $promoted_line_count
  AND stage.expected_observation_count = $promoted_observation_count
OPTIONAL MATCH (old:SourceRecord {source_record_id: sr.source_record_id})-[:FROM_SOURCE]->(source)
WHERE old <> sr AND (old.lifecycle_status = 'active'
  OR (old.lifecycle_status IS NULL AND old.is_latest = true))
WITH sr, stage, collect(old) AS old_versions
WHERE (sr.expected_active_source_record_pk IS NULL AND size(old_versions) = 0)
   OR (sr.expected_active_source_record_pk IS NOT NULL AND size(old_versions) = 1
       AND old_versions[0].source_record_pk = sr.expected_active_source_record_pk)
FOREACH (old IN old_versions |
  SET old.lifecycle_status = 'superseded', old.is_latest = false, old.superseded_at = datetime()
  MERGE (old)-[:PREVIOUS_VERSION_OF]->(sr)
)
SET sr.lifecycle_status = 'active', sr.is_latest = true, sr.link_status = 'linked',
    sr.activated_at = datetime(), sr.updated_at = datetime()
WITH sr, stage
MATCH (stage)-[:STAGED_CONTAINS]->(line:StagedSalesLine)
WITH sr, stage, collect(line) AS staged_lines
OPTIONAL MATCH (stage)-[:STAGED_OBSERVATION]->(observation:StagedSalesVehicleObservation)
WITH sr, stage, staged_lines,
     [item IN collect(observation) WHERE item IS NOT NULL] AS observations
CALL (stage, staged_lines, observations) {
  UNWIND staged_lines AS line
  DETACH DELETE line
  WITH DISTINCT stage, observations
  UNWIND observations AS observation
  DETACH DELETE observation
  WITH DISTINCT stage
  DETACH DELETE stage
  RETURN count(*) AS cleaned_count
}
RETURN sr.source_record_pk AS source_record_pk
"""

REJECT_STAGED_REVIEW_SALE = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
WHERE rc.lifecycle_claim_token = $claim_token
  AND rc.lifecycle_claim_version = $claim_version
  AND rc.lifecycle_claim_status = $claim_status
  AND rc.lifecycle_claimed_by = $actor_id
MATCH (md)-[:ABOUT_LEFT]->(sr:SourceRecord {
  record_type: 'sales', lifecycle_status: 'pending_review'
})
SET sr.sales_stage_lock_version = coalesce(sr.sales_stage_lock_version, 0) + 1
OPTIONAL MATCH (stage:StagedSalesOrder {source_record_pk: sr.source_record_pk})
CALL (stage) {
  OPTIONAL MATCH (stage)-[:STAGED_CONTAINS]->(line:StagedSalesLine)
  WITH stage, collect(line) AS lines
  CALL (lines) {
    UNWIND lines AS item
    DETACH DELETE item
    RETURN count(*) AS deleted_lines
  }
  WITH stage
  OPTIONAL MATCH (stage)-[:STAGED_OBSERVATION]->(observation:StagedSalesVehicleObservation)
  DETACH DELETE observation
  WITH DISTINCT stage
  DETACH DELETE stage
  RETURN count(*) AS cleaned_count
}
SET sr.lifecycle_status = 'rejected', sr.is_latest = false, sr.link_status = 'unresolved',
    sr.rejected_at = datetime(), sr.updated_at = datetime()
RETURN sr.source_record_pk AS source_record_pk
"""

# Pending identity-record review lifecycle.  The lookup deliberately anchors the
# record identity through FROM_SOURCE; source_record_id alone is not globally unique.
GET_PENDING_REVIEW_RECORD = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT]->(pending:SourceRecord {lifecycle_status: 'pending_review'})
WHERE pending.record_type <> 'sales'
MATCH (pending)-[:FROM_SOURCE]->(source:SourceSystem)
OPTIONAL MATCH (md)-[:ABOUT_RIGHT]->(proposed:Person {status: 'active'})
OPTIONAL MATCH (pending)-[:LINKED_TO]->(prior:Person)
RETURN pending.source_record_pk AS pending_source_record_pk,
       pending.source_record_id AS source_record_id,
       source.source_key AS source_system_key,
       pending.normalized_payload AS normalized_payload,
       pending.expected_active_source_record_pk AS expected_active_source_record_pk,
       toString(pending.observed_at) AS observed_at,
       collect(DISTINCT prior.person_id) AS prior_person_ids,
       proposed.person_id AS proposed_person_id
"""

CLAIM_PENDING_REVIEW_RESOLUTION = (
    """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
WHERE rc.queue_state IN ['open', 'assigned', 'deferred']
  AND (rc.assigned_to IS NULL OR rc.assigned_to = $actor_id)
  AND rc.lifecycle_claim_token IS NULL
  AND """
    + _INVOLVED_SOURCE_IS_PENDING
    + """
SET rc.lifecycle_claimed_at = datetime(),
    rc.lifecycle_claimed_by = $actor_id,
    rc.lifecycle_claim_token = randomUUID(),
    rc.lifecycle_claim_version = coalesce(rc.lifecycle_claim_version, 0) + 1,
    rc.lifecycle_claim_status = rc.queue_state,
    rc.updated_at = datetime()
RETURN rc.lifecycle_claim_token AS claim_token,
       rc.lifecycle_claim_version AS claim_version,
       rc.lifecycle_claim_status AS claim_status,
       rc.lifecycle_claimed_by AS claimed_by
"""
)

ACTIVATE_PENDING_REVIEW_RECORD = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT]->(pending:SourceRecord {
  source_record_pk: $pending_source_record_pk, lifecycle_status: 'pending_review'
})-[:FROM_SOURCE]->(source:SourceSystem {source_key: $source_system_key})
MATCH (md)-[:ABOUT_RIGHT]->(approved:Person {person_id: $approved_person_id, status: 'active'})
MERGE (identity_lock:SourceRecordIdentityLock {
  source_system: $source_system_key, source_record_id: pending.source_record_id
})
SET identity_lock.locked_at = datetime()
WITH rc, md, pending, source, approved, identity_lock
OPTIONAL MATCH (old:SourceRecord {source_record_id: pending.source_record_id})-[:FROM_SOURCE]->(source)
WHERE old <> pending AND (
  old.lifecycle_status = 'active'
  OR (old.lifecycle_status IS NULL AND old.is_latest = true)
)
WITH pending, approved, source, collect(old) AS old_versions
WHERE (pending.expected_active_source_record_pk IS NULL
       AND $expected_active_source_record_pk IS NULL
       OR pending.expected_active_source_record_pk = $expected_active_source_record_pk)
  AND ((
  pending.expected_active_source_record_pk IS NULL AND size(old_versions) = 0
) OR (
  pending.expected_active_source_record_pk IS NOT NULL AND size(old_versions) = 1
  AND old_versions[0].source_record_pk = pending.expected_active_source_record_pk
))
FOREACH (old IN old_versions |
  SET old.lifecycle_status = 'superseded', old.is_latest = false,
      old.superseded_at = datetime(), old.updated_at = datetime()
  MERGE (old)-[:PREVIOUS_VERSION_OF]->(pending)
)
SET pending.lifecycle_status = 'active', pending.is_latest = true,
    pending.activated_at = datetime(), pending.updated_at = datetime(),
    pending.link_status = 'linked'
WITH pending, approved, source, old_versions
OPTIONAL MATCH (pending)-[unsafe:LINKED_TO]->(provisional:Person)
WITH pending, approved, source, old_versions,
     collect(DISTINCT provisional.person_id) AS prior_person_ids,
     collect(unsafe) AS unsafe_links
FOREACH (rel IN unsafe_links | DELETE rel)
MERGE (pending)-[:LINKED_TO {linked_at: datetime()}]->(approved)
WITH pending, approved, source, old_versions, prior_person_ids
CALL (pending, approved, source) {
  OPTIONAL MATCH (call:SourceRecord {record_type: 'call', lifecycle_status: 'pending_review'})
        -[:CHILD_OF]->(:SourceRecord {record_type: 'crm_history'})
        -[:CHILD_OF]->(logical_deal:SourceRecord {record_type: 'crm_deal'})
        -[:FROM_SOURCE]->(source)
  WHERE pending.record_type = 'crm_deal'
    AND logical_deal.source_record_id = pending.source_record_id
  WITH approved, collect(DISTINCT call) AS calls
  CALL (calls) {
    UNWIND calls AS call
    OPTIONAL MATCH (call)-[old_call_link:LINKED_TO]->(:Person)
    DELETE old_call_link
    RETURN count(*) AS removed_call_link_count
  }
  FOREACH (call IN calls |
    SET call.lifecycle_status = 'active', call.link_status = 'linked',
        call.activated_at = datetime(), call.updated_at = datetime()
    CREATE (call)-[:LINKED_TO {linked_at: datetime()}]->(approved)
  )
  RETURN size(calls) AS activated_call_count
}
WITH pending, approved, source, old_versions, prior_person_ids
CALL (old_versions) {
  UNWIND old_versions AS old
  OPTIONAL MATCH (owner:Person)-[rel:IDENTIFIED_BY|LIVES_AT]->()
  WHERE rel.source_record_pk = old.source_record_pk
  SET rel.is_active = false, rel.updated_at = datetime()
  RETURN collect(DISTINCT owner.person_id) AS old_edge_owners
}
CALL (old_versions) {
  UNWIND old_versions AS old
  OPTIONAL MATCH (owner:Person)-[rel:HAS_FACT]->(old)
  WHERE rel.source_record_pk = old.source_record_pk
  SET rel.is_active = false, rel.updated_at = datetime()
  RETURN collect(DISTINCT owner.person_id) AS old_fact_owners
}
CALL (old_versions) {
  UNWIND old_versions AS old
  OPTIONAL MATCH (old)-[rel:DESCRIBES_ADDRESS]->(:Address)
  SET rel.is_active = false, rel.updated_at = datetime()
  RETURN count(rel) AS retired_address_descriptions
}
CALL (old_versions) {
  UNWIND old_versions AS old
  OPTIONAL MATCH (:Person)-[old_bankruptcy:HAS_BANKRUPTCY_CASE]->(:BankruptcyCase)
  WHERE old_bankruptcy.source_record_pk = old.source_record_pk
  SET old_bankruptcy.is_active = false, old_bankruptcy.retired_at = datetime(),
      old_bankruptcy.updated_at = datetime()
  WITH old
  OPTIONAL MATCH (old)-[mention:MENTIONS_VEHICLE]->(:Vehicle)
  SET mention.is_active = false, mention.retired_at = datetime(), mention.updated_at = datetime()
  RETURN count(*) AS retired_specialized_count
}
CALL (old_versions) {
  UNWIND old_versions AS old
  OPTIONAL MATCH ()-[old_knows:KNOWS]->()
  WHERE old_knows.source_record_pk = old.source_record_pk
  SET old_knows.is_active = false, old_knows.retired_at = datetime(),
      old_knows.updated_at = datetime()
  RETURN count(old_knows) AS retired_knows_count
}
WITH pending, approved, source, old_versions, prior_person_ids,
     old_edge_owners + old_fact_owners AS retired_owners
CALL (pending, approved, source) {
  UNWIND $identifiers AS ident
  MERGE (id:Identifier {identifier_type: ident.identifier_type,
                        normalized_value: ident.normalized_value})
  ON CREATE SET id.identifier_id = randomUUID(), id.created_at = datetime()
  MERGE (approved)-[rel:IDENTIFIED_BY {
    source_system_key: source.source_key, source_record_pk: pending.source_record_pk
  }]->(id)
  SET rel.is_active = true, rel.is_verified = ident.is_verified,
      rel.quality_flag = ident.quality_flag, rel.source_system_key = source.source_key,
      rel.source_record_pk = pending.source_record_pk, rel.last_seen_at = datetime(),
      rel.last_confirmed_at = datetime()
  RETURN count(*) AS identifier_count
}
CALL (pending, approved, source) {
  UNWIND $addresses AS address
  MERGE (addr:Address {country_code: address.country_code, postal_code: address.postal_code,
    street_name: address.street_name, street_number: address.street_number,
    unit_number: address.unit_number})
  ON CREATE SET addr.address_id = randomUUID(), addr.normalized_full = address.normalized_full,
                addr.created_at = datetime()
  MERGE (approved)-[rel:LIVES_AT {
    source_system_key: source.source_key, source_record_pk: pending.source_record_pk
  }]->(addr)
  SET rel.is_active = true, rel.is_verified = false, rel.quality_flag = address.quality_flag,
      rel.source_system_key = source.source_key, rel.source_record_pk = pending.source_record_pk,
      rel.last_seen_at = datetime(), rel.last_confirmed_at = datetime()
  MERGE (pending)-[described:DESCRIBES_ADDRESS]->(addr)
  SET described.is_active = true, described.source_system_key = source.source_key,
      described.linked_at = datetime()
  RETURN count(*) AS address_count
}
CALL (pending, approved) {
  UNWIND $attributes AS attribute
  CREATE (approved)-[:HAS_FACT {attribute_name: attribute.attribute_name,
    attribute_value: attribute.attribute_value, source_record_pk: pending.source_record_pk,
    source_trust_tier: 'tier_3', confidence: 1.0, quality_flag: attribute.quality_flag,
    is_active: true, is_current_hint: false, observed_at: datetime($observed_at),
    created_at: datetime()}]->(pending)
  RETURN count(*) AS attribute_count
}
CALL (pending, approved) {
  UNWIND $bankruptcy_cases AS item
  MERGE (bc:BankruptcyCase {source_system_key: item.source_system_key,
                            source_case_id: item.source_case_id})
  ON CREATE SET bc.bankruptcy_case_id = randomUUID(), bc.created_at = datetime()
  SET bc.case_number = item.case_number, bc.document_type = item.document_type,
      bc.document_date = item.document_date, bc.event_type = item.event_type,
      bc.event_date = item.event_date, bc.trustee_name = item.trustee_name,
      bc.trustee_firm = item.trustee_firm, bc.source_url = item.source_url,
      bc.first_seen_at = CASE WHEN item.first_seen_at IS NULL THEN null
                             ELSE datetime(item.first_seen_at) END,
      bc.last_seen_at = CASE WHEN item.last_seen_at IS NULL THEN null
                            ELSE datetime(item.last_seen_at) END,
      bc.raw_payload = item.raw_payload, bc.updated_at = datetime()
  MERGE (approved)-[bankruptcy_rel:HAS_BANKRUPTCY_CASE {
    source_system_key: item.source_system_key, source_record_pk: pending.source_record_pk
  }]->(bc)
  SET bankruptcy_rel.source_record_pk = pending.source_record_pk,
      bankruptcy_rel.is_active = true,
      bankruptcy_rel.observed_at = datetime(item.observed_at),
      bankruptcy_rel.last_seen_at = datetime(),
      bankruptcy_rel.activated_at = coalesce(bankruptcy_rel.activated_at, datetime()),
      bankruptcy_rel.retired_at = null
  MERGE (pending)-[:DESCRIBES_CASE]->(bc)
  RETURN count(*) AS bankruptcy_count
}
CALL (pending) {
  UNWIND $vehicle_mentions AS item
  MATCH (vehicle:Vehicle)
  WHERE (item.normalized_lta_tag IS NOT NULL
         AND vehicle.normalized_lta_tag = item.normalized_lta_tag)
     OR (item.normalized_lta_tag IS NULL
         AND item.normalized_serial_number IS NOT NULL AND item.product IS NOT NULL
         AND vehicle.normalized_serial_number = item.normalized_serial_number
         AND vehicle.product IS NOT NULL
         AND toLower(trim(vehicle.product)) = toLower(trim(item.product)))
  WITH pending, item, collect(DISTINCT vehicle) AS vehicles
  WHERE size(vehicles) = 1
  WITH pending, item, vehicles[0] AS vehicle
  MERGE (pending)-[rel:MENTIONS_VEHICLE]->(vehicle)
  SET rel.source_system_key = item.source_system_key,
      rel.source_record_id = item.source_record_id, rel.raw_context = item.raw_context,
      rel.source_record_pk = pending.source_record_pk,
      rel.observed_at = item.observed_at, rel.confidence = item.confidence,
      rel.quality_flag = item.quality_flag, rel.is_active = true,
      rel.activated_at = coalesce(rel.activated_at, datetime()), rel.retired_at = null,
      rel.last_seen_at = datetime(), rel.updated_at = datetime()
  RETURN count(*) AS vehicle_mention_count
}
CALL (pending, approved) {
  UNWIND $knows_relationships AS item
  MATCH (declarer_sr:SourceRecord {source_record_id: item.declarer_source_record_id})
  MATCH (declarer_sr)-[:FROM_SOURCE]->(declarer_source:SourceSystem)
  MATCH (declarer_sr)
        -[:LINKED_TO]->(declarer:Person {status: 'active'})
  WHERE declarer_source.source_key = item.declarer_source_system_key
    AND (declarer_sr.lifecycle_status = 'active'
      OR (declarer_sr.lifecycle_status IS NULL AND declarer_sr.is_latest = true))
  FOREACH (_ IN CASE WHEN declarer <> approved THEN [1] ELSE [] END |
    MERGE (declarer)-[new_knows:KNOWS {
      source_system_key: item.source_system_key,
      source_record_pk: pending.source_record_pk
    }]->(approved)
    ON CREATE SET new_knows.knows_id = randomUUID(),
                  new_knows.first_seen_at = datetime(),
                  new_knows.created_at = datetime()
    SET new_knows.relationship_label = item.relationship_label,
        new_knows.relationship_category = item.relationship_category,
        new_knows.declared_by_person_id = declarer.person_id,
        new_knows.status = item.status, new_knows.approved_at = item.approved_at,
        new_knows.is_active = true,
        new_knows.activated_at = coalesce(new_knows.activated_at, datetime()),
        new_knows.retired_at = null, new_knows.last_seen_at = datetime(),
        new_knows.last_confirmed_at = datetime(), new_knows.updated_at = datetime()
  )
  RETURN count(item) AS activated_knows_count
}
WITH pending, approved, old_versions, prior_person_ids, retired_owners,
     activated_knows_count
WHERE activated_knows_count = size($knows_relationships)
WITH pending, approved, old_versions,
     retired_owners + prior_person_ids + [approved.person_id] AS direct_person_ids
OPTIONAL MATCH (changed_person:Person)-[changed_knows:KNOWS]-(:Person)
WHERE changed_knows.source_record_pk IN
      [old IN old_versions | old.source_record_pk] + [pending.source_record_pk]
WITH pending, approved, old_versions, direct_person_ids,
     collect(DISTINCT changed_person.person_id) AS changed_relationship_person_ids
CALL (direct_person_ids, changed_relationship_person_ids) {
  UNWIND direct_person_ids + changed_relationship_person_ids AS dirty_person_id
  WITH DISTINCT dirty_person_id
  MATCH (changed_person:Person {person_id: dirty_person_id, status: 'active'})
  SET changed_person.analysis_input_revision =
        coalesce(changed_person.analysis_input_revision, 0) + 1,
      changed_person.analysis_dirty_at = datetime()
  RETURN count(changed_person) AS dirtied_count
}
RETURN pending.source_record_pk AS pending_source_record_pk,
       [old IN old_versions | old.source_record_pk] AS old_source_record_pks,
       approved.person_id AS approved_person_id,
       direct_person_ids + changed_relationship_person_ids AS affected_person_ids
"""

REJECT_PENDING_REVIEW_RECORD = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT]->(pending:SourceRecord {lifecycle_status: 'pending_review'})
MATCH (pending)-[:FROM_SOURCE]->(source:SourceSystem)
SET pending.lifecycle_status = 'rejected', pending.is_latest = false,
    pending.rejection_reason = $reason, pending.rejected_at = datetime(),
    pending.resolved_at = datetime(), pending.updated_at = datetime()
WITH pending, source
OPTIONAL MATCH (call:SourceRecord {record_type: 'call', lifecycle_status: 'pending_review'})
      -[:CHILD_OF]->(:SourceRecord {record_type: 'crm_history'})
      -[:CHILD_OF]->(logical_deal:SourceRecord {record_type: 'crm_deal'})
      -[:FROM_SOURCE]->(source)
WHERE pending.record_type = 'crm_deal'
  AND logical_deal.source_record_id = pending.source_record_id
WITH pending, collect(DISTINCT call) AS calls
CALL (calls) {
  UNWIND calls AS call
  OPTIONAL MATCH (call)-[old_call_link:LINKED_TO]->(:Person)
  DELETE old_call_link
  RETURN count(*) AS removed_call_link_count
}
FOREACH (call IN calls |
  SET call.lifecycle_status = 'rejected', call.is_latest = false,
      call.link_status = 'unresolved', call.rejection_reason = $reason,
      call.rejected_at = datetime(), call.resolved_at = datetime(),
      call.updated_at = datetime()
)
RETURN pending.source_record_pk AS pending_source_record_pk
"""
