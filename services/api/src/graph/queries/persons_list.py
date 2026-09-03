"""Generalized, pagination-aware Person listing query builder."""

from __future__ import annotations

from src.graph.queries.persons_list_filters import (
    ADDRESS_FILTERS,
    build_common_filter_clause,
    build_entity_filter_clause,
)

GET_PERSON_LIST_CORE_SUMMARY = """
MATCH (p:Person)
WHERE p.status <> 'merged'
RETURN count(p) AS all_profiles_count,
       sum(CASE WHEN p.is_high_risk = true THEN 1 ELSE 0 END) AS high_risk_count,
       sum(CASE WHEN p.is_high_value = true THEN 1 ELSE 0 END) AS high_value_count,
       sum(CASE WHEN p.preferred_phone IS NULL AND p.preferred_email IS NULL THEN 1 ELSE 0 END)
         AS no_contact_count
"""

GET_PERSON_LIST_CRM_SUMMARY = """
MATCH (deal:SourceRecord {record_type: 'crm_deal'})-[deal_link:LINKED_TO]->(p:Person)
WHERE p.status <> 'merged'
  AND coalesce(deal_link.is_active, true) = true
  AND (deal.lifecycle_status = 'active'
    OR (deal.lifecycle_status IS NULL AND deal.is_latest = true))
RETURN count(DISTINCT CASE
         WHEN deal.observed_at >= datetime.truncate('month', datetime()) THEN deal
       END) AS deals_this_month_count,
       count(DISTINCT deal) AS all_deals_count
"""

# Compatibility alias for external callers. Repository code intentionally runs
# the two bounded summary components independently so CRM contention/failure
# cannot block the core Person counts.
GET_PERSON_LIST_SUMMARY = GET_PERSON_LIST_CORE_SUMMARY

_SOURCE_RECORD_COUNT = """
CALL (p) {
  RETURN count {
    (sr:SourceRecord)-[link:LINKED_TO]->(p)
    WHERE coalesce(link.is_active, true) = true
      AND (sr.history_family IS NULL OR sr.history_family = 'activity')
      AND (sr.lifecycle_status = 'active'
        OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))
  } AS source_record_count
}
"""

_ENTITY_ENRICHMENT = """
CALL (p) {
  OPTIONAL MATCH (sr_ent:SourceRecord)-[link:LINKED_TO]->(p)
  WHERE coalesce(link.is_active, true) = true
    AND (sr_ent.history_family IS NULL OR sr_ent.history_family = 'activity')
    AND (sr_ent.lifecycle_status = 'active'
      OR (sr_ent.lifecycle_status IS NULL AND sr_ent.is_latest = true))
  OPTIONAL MATCH (sr_ent)-[:FROM_SOURCE]->(ss_ent:SourceSystem)
  OPTIONAL MATCH (sr_ent)-[:OWNED_BY]->(record_entity:Entity)
  OPTIONAL MATCH (ss_ent)-[:OPERATED_BY]->(source_entity:Entity)
  WITH coalesce(record_entity, source_entity) AS e, sr_ent
  WITH e, count(DISTINCT sr_ent) AS e_sr_count
  WHERE e IS NOT NULL
  WITH collect({
    entity_key: e.entity_key,
    display_name: e.display_name,
    entity_type: e.entity_type,
    country_code: e.country_code,
    is_active: e.is_active,
    source_record_count: e_sr_count
  }) AS entities
  RETURN entities{entity_count_return}
}
"""

_ENTITY_COUNT = """
CALL (p) {
  OPTIONAL MATCH (sr_ent:SourceRecord)-[link:LINKED_TO]->(p)
  WHERE coalesce(link.is_active, true) = true
    AND (sr_ent.history_family IS NULL OR sr_ent.history_family = 'activity')
    AND (sr_ent.lifecycle_status = 'active'
      OR (sr_ent.lifecycle_status IS NULL AND sr_ent.is_latest = true))
  OPTIONAL MATCH (sr_ent)-[:FROM_SOURCE]->(ss_ent:SourceSystem)
  OPTIONAL MATCH (sr_ent)-[:OWNED_BY]->(record_entity:Entity)
  OPTIONAL MATCH (ss_ent)-[:OPERATED_BY]->(source_entity:Entity)
  WITH DISTINCT coalesce(record_entity, source_entity) AS entity
  WHERE entity IS NOT NULL
  RETURN count(entity) AS entity_count
}
"""

_CONNECTION_COUNT = """
CALL (p) {
  OPTIONAL MATCH (p)-[p_addr:LIVES_AT]->(:Address)
    <-[ca_addr:LIVES_AT]-(ca:Person)
    WHERE coalesce(p_addr.is_active, true) = true
      AND coalesce(ca_addr.is_active, true) = true
      AND ca.person_id <> p.person_id AND ca.status <> 'merged'
  WITH p, collect(DISTINCT ca) AS address_conn
  OPTIONAL MATCH (p)-[p_knows:KNOWS]-(ck:Person)
    WHERE coalesce(p_knows.is_active, true) = true
      AND ck.person_id <> p.person_id AND ck.status <> 'merged'
  WITH address_conn, collect(DISTINCT ck) AS knows_conn
  WITH address_conn + knows_conn AS all_conn
  UNWIND all_conn AS c
  RETURN count(DISTINCT c) AS connection_count
}
"""

_PHONE_CONFIDENCE = """
CALL (p) {
  OPTIONAL MATCH (p)-[pi:IDENTIFIED_BY]->(phone_id:Identifier)
  WHERE coalesce(pi.is_active, true) = true
    AND phone_id.identifier_type = 'phone'
    AND phone_id.normalized_value = p.preferred_phone
  WITH pi.quality_flag AS qf
  ORDER BY CASE qf WHEN 'valid' THEN 0 ELSE 1 END
  LIMIT 1
  RETURN CASE qf
    WHEN 'valid' THEN 1.0
    WHEN 'partial_parse' THEN 0.8
    WHEN 'stale' THEN 0.6
    WHEN 'source_untrusted' THEN 0.4
    WHEN 'shared_suspected' THEN 0.3
    WHEN 'placeholder_value' THEN 0.1
    WHEN 'invalid_format' THEN 0.0
    ELSE null
  END AS phone_confidence
}
"""

_IDENTIFIER_COUNT = """
CALL (p) {
  RETURN count {
    (p)-[id_count:IDENTIFIED_BY]->(idc:Identifier)
    WHERE coalesce(id_count.is_active, true) = true
  } AS identifier_count
}
"""

_POSSIBLE_MATCH_COUNT = """
CALL (p) {
  OPTIONAL MATCH (p)-[p_shared_id:IDENTIFIED_BY]->(shared_id:Identifier)
  WHERE coalesce(p_shared_id.is_active, true) = true
  WITH DISTINCT p, shared_id
  OPTIONAL MATCH (shared_id)<-[other_shared_id:IDENTIFIED_BY]-(other:Person)
  WHERE shared_id IS NOT NULL
    AND coalesce(other_shared_id.is_active, true) = true
    AND other.person_id <> p.person_id AND other.status <> 'merged'
  WITH DISTINCT other
  RETURN count(other) AS possible_match_count
}
"""

_SYSTEM_MATCH_COUNT = """
CALL (p) {
  OPTIONAL MATCH (md:MatchDecision)-[:ABOUT_LEFT|ABOUT_RIGHT]->(p)
  WHERE EXISTS { (md)-[:ABOUT_LEFT]->(:Person) }
    AND EXISTS { (md)-[:ABOUT_RIGHT]->(:Person) }
    AND EXISTS {
      (rc:ReviewCase)-[:FOR_DECISION]->(md)
      WHERE NOT rc.queue_state IN ['resolved', 'cancelled']
    }
  RETURN count(DISTINCT md) AS system_match_count
}
"""

_ORDER_COUNT = """
CALL (p) {
  RETURN count {
    (p)-[purchase:PURCHASED]->(:Order)
    WHERE coalesce(purchase.is_active, true) = true
  } AS order_count
}
"""

_BANKRUPTCY_CASE_COUNT = """
CALL (p) {
  RETURN count {
    (p)-[bankruptcy_rel:HAS_BANKRUPTCY_CASE]->(:BankruptcyCase)
    WHERE coalesce(bankruptcy_rel.is_active, true) = true
  } AS bankruptcy_case_count
}
"""

_METRIC_CALLS: dict[str, str] = {
    "source_record_count": _SOURCE_RECORD_COUNT,
    "connection_count": _CONNECTION_COUNT,
    "phone_confidence": _PHONE_CONFIDENCE,
    "identifier_count": _IDENTIFIER_COUNT,
    "possible_match_count": _POSSIBLE_MATCH_COUNT,
    "system_match_count": _SYSTEM_MATCH_COUNT,
    "order_count": _ORDER_COUNT,
    "bankruptcy_case_count": _BANKRUPTCY_CASE_COUNT,
}

_SORT_COLUMNS: dict[str, str] = {
    "preferred_full_name": "person.preferred_full_name",
    "preferred_phone": "person.preferred_phone",
    "preferred_email": "person.preferred_email",
    "preferred_dob": "person.preferred_dob",
    "preferred_nric": "person.preferred_nric",
    "source_record_count": "source_record_count",
    "connection_count": "connection_count",
    "entity_count": "entity_count",
    "possible_match_count": "possible_match_count",
    "system_match_count": "system_match_count",
    "order_count": "order_count",
    "crm_deal_count": "crm_deal_count",
    "bankruptcy_case_count": "bankruptcy_case_count",
    "phone_confidence": "phone_confidence",
    "updated_at": "person.updated_at",
    "profile_completeness_score": "person.profile_completeness_score",
    "relevance": "score",
}

_DEFAULT_SORT_WITH_Q = "relevance"
_DEFAULT_SORT_WITHOUT_Q = "profile_completeness_score"
_DEFAULT_ORDER_WITH_Q = "DESC"
_DEFAULT_ORDER_WITHOUT_Q = "DESC"
_COMPLETENESS_SCORE_PREDICATE = "p.profile_completeness_score IS NOT NULL"

_PRE_ENRICH_SORT_MAP: dict[str, str] = {
    "preferred_full_name": "p.preferred_full_name",
    "preferred_phone": "p.preferred_phone",
    "preferred_email": "p.preferred_email",
    "preferred_dob": "p.preferred_dob",
    "preferred_nric": "p.preferred_nric",
    "updated_at": "p.updated_at",
    "profile_completeness_score": "p.profile_completeness_score",
    "crm_deal_count": "p.crm_deal_count",
    "relevance": "score",
}

_FINAL_SORT_MAP: dict[str, str] = {
    "preferred_full_name": "person.preferred_full_name",
    "preferred_phone": "person.preferred_phone",
    "preferred_email": "person.preferred_email",
    "preferred_dob": "person.preferred_dob",
    "preferred_nric": "person.preferred_nric",
    "updated_at": "person.updated_at",
    "profile_completeness_score": "person.profile_completeness_score",
    "crm_deal_count": "crm_deal_count",
    "relevance": "score",
}


def _resolve_sort(sort_by: str | None, sort_order: str | None, *, has_q: bool) -> tuple[str, str]:
    default_key = _DEFAULT_SORT_WITH_Q if has_q else _DEFAULT_SORT_WITHOUT_Q
    default_dir = _DEFAULT_ORDER_WITH_Q if has_q else _DEFAULT_ORDER_WITHOUT_Q
    key = sort_by if sort_by and sort_by in _SORT_COLUMNS else default_key
    if key == "relevance" and not has_q:
        key = default_key
    direction = "DESC" if (sort_order or default_dir).upper() == "DESC" else "ASC"
    return key, direction


def _requires_completeness_score(*, sort_key: str, has_q: bool) -> bool:
    return sort_key == "profile_completeness_score" and not has_q


def _entity_enrichment(*, include_count: bool) -> str:
    suffix = ", size(entities) AS entity_count" if include_count else ""
    return _ENTITY_ENRICHMENT.replace("{entity_count_return}", suffix)


def _page_enrichment(excluded_metrics: frozenset[str] = frozenset()) -> str:
    calls: list[str] = []
    if "entity_count" in excluded_metrics:
        calls.append(_entity_enrichment(include_count=False))
    else:
        calls.append(_entity_enrichment(include_count=True))
    for metric, call in _METRIC_CALLS.items():
        if metric not in excluded_metrics:
            calls.append(call)
    return "".join(calls)


def _crm_deal_count_predicates(
    active_filters: frozenset[str],
    *,
    include_non_null: bool,
) -> tuple[str, ...]:
    conditions: list[str] = []
    if include_non_null:
        conditions.append("p.crm_deal_count IS NOT NULL")
    if "crm_deal_count_min" in active_filters:
        conditions.append("p.crm_deal_count >= $crm_deal_count_min")
    if "crm_deal_count_max" in active_filters:
        conditions.append("p.crm_deal_count <= $crm_deal_count_max")
    return tuple(conditions)


def _crm_deal_count_post_bind_clause(active_filters: frozenset[str]) -> str:
    predicates = _crm_deal_count_predicates(active_filters, include_non_null=False)
    if not predicates:
        return ""
    return "WHERE " + " AND ".join(predicates) + "\nWITH p, addr, score\n"


def _return_clause() -> str:
    return """
RETURN p {
  .person_id, .status, .is_high_value, .is_high_risk,
  .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob, .preferred_nric,
  .profile_completeness_score, .golden_profile_computed_at, .golden_profile_version,
  .created_at, .updated_at
} AS person,
addr {
  .address_id, .unit_number, .street_number, .street_name,
  .city, .postal_code, .country_code, .normalized_full
} AS preferred_address,
source_record_count, connection_count, phone_confidence, entities,
entity_count, identifier_count, possible_match_count, system_match_count, order_count,
bankruptcy_case_count, p.crm_deal_count AS crm_deal_count, score
"""


def build_list_persons_query(
    sort_by: str | None,
    sort_order: str | None,
    *,
    has_q: bool,
    active_filters: frozenset[str] = frozenset(),
    entity_mode: str = "or",
    source_mode: str = "or",
) -> str:
    """Build a list query that enriches only the requested page whenever possible."""
    key, direction = _resolve_sort(sort_by, sort_order, has_q=has_q)
    entity_clause = build_entity_filter_clause(
        entity_mode,
        source_mode,
        active_filters,
        include_preferred_address=True,
    )
    requires_completeness_score = _requires_completeness_score(sort_key=key, has_q=has_q)
    crm_predicates = _crm_deal_count_predicates(
        active_filters,
        include_non_null=key == "crm_deal_count" and not has_q,
    )
    common_clause = build_common_filter_clause(active_filters)
    head = _head(
        has_q=has_q,
        skip_address=not bool(active_filters & ADDRESS_FILTERS),
        requires_completeness_score=requires_completeness_score,
        person_predicates=crm_predicates,
    )
    post_bind_clause = _crm_deal_count_post_bind_clause(active_filters) if has_q else ""
    pre_col = _PRE_ENRICH_SORT_MAP.get(key)
    pre_metrics: set[str] = set()
    before_page = ""

    if pre_col is not None:
        page_projection = f"WITH p, addr, score\nORDER BY {pre_col} {direction}, p.person_id ASC\n"
        return (
            head
            + post_bind_clause
            + common_clause
            + entity_clause
            + page_projection
            + "SKIP $skip LIMIT $limit\n"
            + _page_enrichment()
            + _return_clause()
            + f"ORDER BY {_FINAL_SORT_MAP[key]} {direction}, person.person_id ASC\n"
        )

    pre_metric = _ENTITY_COUNT if key == "entity_count" else _METRIC_CALLS[key]
    before_page += pre_metric
    pre_metrics.add(key)
    metric_projection = "WITH p, addr, score"
    for metric in sorted(pre_metrics):
        metric_projection += f", {metric}"
    metric_projection += f"\nORDER BY {key} {direction}, p.person_id ASC\n"
    return (
        head
        + post_bind_clause
        + common_clause
        + entity_clause
        + before_page
        + metric_projection
        + "SKIP $skip LIMIT $limit\n"
        + _page_enrichment(frozenset(pre_metrics))
        + _return_clause()
        + f"ORDER BY {key} {direction}, person.person_id ASC\n"
    )


def build_count_persons_query(
    sort_by: str | None = None,
    sort_order: str | None = None,
    *,
    has_q: bool,
    active_filters: frozenset[str] = frozenset(),
    entity_mode: str = "or",
    source_mode: str = "or",
) -> str:
    """Build the exact-count query over the same row set as the resolved list sort."""
    key, _direction = _resolve_sort(sort_by, sort_order, has_q=has_q)
    requires_completeness_score = _requires_completeness_score(sort_key=key, has_q=has_q)
    crm_predicates = _crm_deal_count_predicates(
        active_filters,
        include_non_null=key == "crm_deal_count" and not has_q,
    )
    return (
        _head(
            has_q=has_q,
            skip_address=not bool(active_filters & ADDRESS_FILTERS),
            requires_completeness_score=requires_completeness_score,
            person_predicates=crm_predicates,
        )
        + (_crm_deal_count_post_bind_clause(active_filters) if has_q else "")
        + build_common_filter_clause(active_filters)
        + build_entity_filter_clause(
            entity_mode,
            source_mode,
            active_filters,
            include_preferred_address=False,
        )
        + "RETURN count(p) AS total\n"
    )


def _head(
    *,
    has_q: bool,
    skip_address: bool = False,
    requires_completeness_score: bool = False,
    person_predicates: tuple[str, ...] = (),
) -> str:
    if has_q and requires_completeness_score:
        raise ValueError("Full-text person lists must not require a completeness score.")
    if has_q:
        if skip_address:
            return (
                "CALL db.index.fulltext.queryNodes('person_name_search', $q) YIELD node AS p, score\n"
                "WITH p, null AS addr, score\n"
            )
        return (
            "CALL db.index.fulltext.queryNodes('person_name_search', $q) YIELD node AS p, score\n"
            "OPTIONAL MATCH (p)-[addr_link:LIVES_AT]->(addr:Address)\n"
            "WHERE coalesce(addr_link.is_active, true) = true\n"
            "WITH p, addr, score\n"
        )
    predicates = list(person_predicates)
    if requires_completeness_score:
        predicates.insert(0, _COMPLETENESS_SCORE_PREDICATE)
    person_predicate = "WHERE " + " AND ".join(predicates) + "\n" if predicates else ""
    if skip_address:
        return f"MATCH (p:Person)\n{person_predicate}WITH p, null AS addr, null AS score\n"
    return (
        "MATCH (p:Person)\n"
        + person_predicate
        + "OPTIONAL MATCH (p)-[addr_link:LIVES_AT]->(addr:Address)\n"
        "WHERE coalesce(addr_link.is_active, true) = true\n"
        "WITH p, addr, null AS score\n"
    )
