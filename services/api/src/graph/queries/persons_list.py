"""Generalized person listing query with multi-filter + single-column sort.

Powers ``GET /v1/persons``. Supports fulltext (``q``) or structured-only mode,
plus optional filters: status, entity_key, is_high_value, is_high_risk,
has_phone, has_email, updated_after, updated_before.

The ``q`` parameter searches across preferred_full_name, preferred_nric,
preferred_email, and preferred_phone via the ``person_name_search`` fulltext index.
"""

from __future__ import annotations

from src.graph.queries.persons_list_filters import (
    ADDRESS_FILTERS,
    build_common_filter_clause,
    build_entity_filter_clause,
)

GET_PERSON_LIST_SUMMARY = """
MATCH (p:Person)
WHERE p.status <> 'merged'
RETURN count(p) AS all_profiles_count,
       sum(CASE WHEN p.is_high_risk = true THEN 1 ELSE 0 END) AS high_risk_count,
       sum(CASE WHEN p.is_high_value = true THEN 1 ELSE 0 END) AS high_value_count,
       sum(CASE WHEN p.preferred_phone IS NULL AND p.preferred_email IS NULL THEN 1 ELSE 0 END)
         AS no_contact_count
"""


_ENRICH_AND_RETURN = """
CALL (p) {
  OPTIONAL MATCH (sr:SourceRecord)-[link:LINKED_TO]->(p)
  WHERE coalesce(link.is_active, true) = true
    AND (sr.lifecycle_status = 'active'
      OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))
  RETURN count(sr) AS source_record_count
}
CALL (p) {
  OPTIONAL MATCH (sr_ent:SourceRecord)-[link:LINKED_TO]->(p)
  WHERE coalesce(link.is_active, true) = true
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
  RETURN entities
}
CALL (p) {
  OPTIONAL MATCH (p)-[p_addr:LIVES_AT]->(:Address)
    <-[ca_addr:LIVES_AT]-(ca:Person)
    WHERE coalesce(p_addr.is_active, true) = true
      AND coalesce(ca_addr.is_active, true) = true
      AND ca.person_id <> p.person_id AND ca.status <> 'merged'
  OPTIONAL MATCH (p)-[p_knows:KNOWS]-(ck:Person)
    WHERE coalesce(p_knows.is_active, true) = true
      AND ck.person_id <> p.person_id AND ck.status <> 'merged'
  WITH collect(DISTINCT ca) + collect(DISTINCT ck) AS all_conn
  UNWIND all_conn AS c
  RETURN count(DISTINCT c) AS connection_count
}
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
CALL (p) {
  OPTIONAL MATCH (p)-[id_count:IDENTIFIED_BY]->(idc:Identifier)
  WHERE coalesce(id_count.is_active, true) = true
  RETURN count(idc) AS identifier_count
}
CALL (p) {
  OPTIONAL MATCH (p)-[p_shared_id:IDENTIFIED_BY]->(shared_id:Identifier)
    <-[other_shared_id:IDENTIFIED_BY]-(other:Person)
    WHERE coalesce(p_shared_id.is_active, true) = true
      AND coalesce(other_shared_id.is_active, true) = true
      AND other.person_id <> p.person_id AND other.status <> 'merged'
  RETURN count(DISTINCT other) AS possible_match_count
}
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
CALL (p) {
  RETURN count{ (p)-[:PURCHASED]->(:Order) } AS order_count
}
CALL (p) {
  OPTIONAL MATCH (p)-[bankruptcy_rel:HAS_BANKRUPTCY_CASE]->(:BankruptcyCase)
  WHERE coalesce(bankruptcy_rel.is_active, true) = true
  RETURN count(bankruptcy_rel) AS bankruptcy_case_count
}
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
size(entities) AS entity_count, identifier_count, possible_match_count, system_match_count, order_count, bankruptcy_case_count, score
"""

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

# Sort columns that are native Person node properties (or fulltext score).
# For these, SKIP/LIMIT can happen BEFORE the 8 CALL enrichments so only
# the requested page of rows is ever enriched instead of the full match set.
_PRE_ENRICH_SORT_MAP: dict[str, str] = {
    "person.preferred_full_name": "p.preferred_full_name",
    "person.preferred_phone": "p.preferred_phone",
    "person.preferred_email": "p.preferred_email",
    "person.preferred_dob": "p.preferred_dob",
    "person.preferred_nric": "p.preferred_nric",
    "person.updated_at": "p.updated_at",
    "person.profile_completeness_score": "p.profile_completeness_score",
    "score": "score",
}


def _resolve_sort(sort_by: str | None, sort_order: str | None, *, has_q: bool) -> tuple[str, str]:
    default_col = _DEFAULT_SORT_WITH_Q if has_q else _DEFAULT_SORT_WITHOUT_Q
    default_dir = _DEFAULT_ORDER_WITH_Q if has_q else _DEFAULT_ORDER_WITHOUT_Q
    col_key = sort_by if sort_by and sort_by in _SORT_COLUMNS else default_col
    if col_key == "relevance" and not has_q:
        col_key = default_col
    direction = "DESC" if (sort_order or default_dir).upper() == "DESC" else "ASC"
    return _SORT_COLUMNS[col_key], direction


def build_list_persons_query(
    sort_by: str | None,
    sort_order: str | None,
    *,
    has_q: bool,
    active_filters: frozenset[str] = frozenset(),
    entity_mode: str = "or",
    source_mode: str = "or",
) -> str:
    """Build the list query for ``GET /v1/persons``.

    For stored-property sorts and full-text relevance, pagination occurs before
    enrichment. Computed sorts still calculate their ordering metric before
    pagination, but all inactive filters are omitted from both query paths.
    """
    col, direction = _resolve_sort(sort_by, sort_order, has_q=has_q)
    entity_clause = build_entity_filter_clause(
        entity_mode,
        source_mode,
        active_filters,
        include_preferred_address=True,
    )
    common_clause = build_common_filter_clause(active_filters)
    pre_col = _PRE_ENRICH_SORT_MAP.get(col)
    head = _head(has_q=has_q, skip_address=not bool(active_filters & ADDRESS_FILTERS))
    if pre_col:
        return (
            head
            + common_clause
            + entity_clause
            + f"WITH p, addr, score\nORDER BY {pre_col} {direction}\nSKIP $skip LIMIT $limit\n"
            + _ENRICH_AND_RETURN
        )
    return (
        head
        + common_clause
        + entity_clause
        + _ENRICH_AND_RETURN
        + f"ORDER BY {col} {direction}\nSKIP $skip LIMIT $limit\n"
    )


def build_count_persons_query(
    *,
    has_q: bool,
    active_filters: frozenset[str] = frozenset(),
    entity_mode: str = "or",
    source_mode: str = "or",
) -> str:
    """Build the total-count query with the same active filters as the list."""
    return (
        _head(has_q=has_q, skip_address=not bool(active_filters & ADDRESS_FILTERS))
        + build_common_filter_clause(active_filters)
        + build_entity_filter_clause(
            entity_mode,
            source_mode,
            active_filters,
            include_preferred_address=False,
        )
        + "RETURN count(p) AS total\n"
    )


def _head(*, has_q: bool, skip_address: bool = False) -> str:
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
    if skip_address:
        return "MATCH (p:Person)\nWITH p, null AS addr, null AS score\n"
    return (
        "MATCH (p:Person)\n"
        "OPTIONAL MATCH (p)-[addr_link:LIVES_AT]->(addr:Address)\n"
        "WHERE coalesce(addr_link.is_active, true) = true\n"
        "WITH p, addr, null AS score\n"
    )
