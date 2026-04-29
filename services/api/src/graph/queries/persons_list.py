"""Generalized person listing query with multi-filter + single-column sort.

Powers ``GET /v1/persons``. Supports fulltext (``q``) or structured-only mode,
plus optional filters: status, entity_key, is_high_value, is_high_risk,
has_phone, has_email, updated_after, updated_before.

The ``q`` parameter searches across preferred_full_name, preferred_nric,
preferred_email, and preferred_phone via the ``person_name_search`` fulltext index.
"""

from __future__ import annotations

_COMMON_FILTER_CLAUSE = """
WHERE p.status <> 'merged'
  AND ($is_high_value IS NULL OR p.is_high_value = $is_high_value)
  AND ($is_high_risk IS NULL OR p.is_high_risk = $is_high_risk)
  AND ($has_phone IS NULL
       OR ($has_phone = true  AND p.preferred_phone IS NOT NULL)
       OR ($has_phone = false AND p.preferred_phone IS NULL))
  AND ($has_email IS NULL
       OR ($has_email = true  AND p.preferred_email IS NOT NULL)
       OR ($has_email = false AND p.preferred_email IS NULL))
  AND ($updated_after  IS NULL OR p.updated_at >= datetime($updated_after))
  AND ($updated_before IS NULL OR p.updated_at <= datetime($updated_before))
  AND ($has_dob IS NULL
       OR ($has_dob = true  AND p.preferred_dob IS NOT NULL)
       OR ($has_dob = false AND p.preferred_dob IS NULL))
  AND ($dob_from IS NULL OR p.preferred_dob >= $dob_from)
  AND ($dob_to   IS NULL OR p.preferred_dob <= $dob_to)
  AND ($has_address IS NULL
       OR ($has_address = true  AND p.preferred_address_id IS NOT NULL)
       OR ($has_address = false AND p.preferred_address_id IS NULL))
  AND ($addr_street IS NULL  OR toLower(addr.street_name)     CONTAINS toLower($addr_street))
  AND ($addr_unit   IS NULL   OR toLower(addr.unit_number)    CONTAINS toLower($addr_unit))
  AND ($addr_city   IS NULL   OR toLower(addr.city)           CONTAINS toLower($addr_city))
  AND ($addr_postal IS NULL   OR toLower(addr.postal_code)    CONTAINS toLower($addr_postal))
  AND ($addr_country IS NULL  OR toLower(addr.country_code)   CONTAINS toLower($addr_country))
"""

_ENTITY_FILTER_CLAUSE = """
WITH p, score, addr WHERE $entity_key IS NULL OR EXISTS {
  MATCH (sr_e:SourceRecord)-[:LINKED_TO]->(p)
  MATCH (sr_e)-[:FROM_SOURCE]->(:SourceSystem)-[:OPERATED_BY]->(:Entity {entity_key: $entity_key})
}
WITH DISTINCT p, score
OPTIONAL MATCH (p)-[:LIVES_AT]->(addr:Address {address_id: p.preferred_address_id})
"""

_ENRICH_AND_RETURN = """
CALL {
  WITH p
  OPTIONAL MATCH (sr:SourceRecord)-[:LINKED_TO]->(p)
  RETURN count(sr) AS source_record_count
}
CALL {
  WITH p
  OPTIONAL MATCH (sr_ent:SourceRecord)-[:LINKED_TO]->(p)
  OPTIONAL MATCH (sr_ent)-[:FROM_SOURCE]->(:SourceSystem)-[:OPERATED_BY]->(e:Entity)
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
CALL {
  WITH p
  OPTIONAL MATCH (p)-[:IDENTIFIED_BY]->(:Identifier)<-[:IDENTIFIED_BY]-(ci:Person)
    WHERE ci.person_id <> p.person_id AND ci.status <> 'merged'
  OPTIONAL MATCH (p)-[:LIVES_AT]->(:Address)<-[:LIVES_AT]-(ca:Person)
    WHERE ca.person_id <> p.person_id AND ca.status <> 'merged'
  OPTIONAL MATCH (p)-[:KNOWS]-(ck:Person)
    WHERE ck.person_id <> p.person_id AND ck.status <> 'merged'
  WITH collect(DISTINCT ci) + collect(DISTINCT ca) + collect(DISTINCT ck) AS all_conn
  UNWIND all_conn AS c
  RETURN count(DISTINCT c) AS connection_count
}
CALL {
  WITH p
  OPTIONAL MATCH (p)-[pi:IDENTIFIED_BY]->(phone_id:Identifier)
  WHERE phone_id.identifier_type = 'phone'
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
CALL {
  WITH p
  OPTIONAL MATCH (p)-[:IDENTIFIED_BY]->(idc:Identifier)
  RETURN count(idc) AS identifier_count
}
CALL {
  WITH p
  RETURN count{ (p)-[:PURCHASED]->(:Order) } AS order_count
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
size(entities) AS entity_count, identifier_count, order_count, score
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
    "identifier_count": "identifier_count",
    "order_count": "order_count",
    "phone_confidence": "phone_confidence",
    "updated_at": "person.updated_at",
    "profile_completeness_score": "person.profile_completeness_score",
    "relevance": "score",
}

_DEFAULT_SORT_WITH_Q = "relevance"
_DEFAULT_SORT_WITHOUT_Q = "profile_completeness_score"
_DEFAULT_ORDER_WITH_Q = "DESC"
_DEFAULT_ORDER_WITHOUT_Q = "DESC"


def _resolve_sort(sort_by: str | None, sort_order: str | None, *, has_q: bool) -> tuple[str, str]:
    default_col = _DEFAULT_SORT_WITH_Q if has_q else _DEFAULT_SORT_WITHOUT_Q
    default_dir = _DEFAULT_ORDER_WITH_Q if has_q else _DEFAULT_ORDER_WITHOUT_Q
    col_key = sort_by if sort_by and sort_by in _SORT_COLUMNS else default_col
    if col_key == "relevance" and not has_q:
        col_key = default_col
    direction = "DESC" if (sort_order or default_dir).upper() == "DESC" else "ASC"
    return _SORT_COLUMNS[col_key], direction


def build_list_persons_query(sort_by: str | None, sort_order: str | None, *, has_q: bool) -> str:
    """Build the list query for ``GET /v1/persons``.

    When ``has_q`` is true, prefixes a fulltext index match; otherwise scans
    Person directly. All non-q filters are parameterised and applied uniformly.
    """
    col, direction = _resolve_sort(sort_by, sort_order, has_q=has_q)
    return (
        _head(has_q=has_q)
        + _COMMON_FILTER_CLAUSE
        + _ENTITY_FILTER_CLAUSE
        + _ENRICH_AND_RETURN
        + f"ORDER BY {col} {direction}\nSKIP $skip LIMIT $limit\n"
    )


def build_count_persons_query(*, has_q: bool) -> str:
    """Build the total-count query matching :func:`build_list_persons_query`'s filters."""
    return (
        _head(has_q=has_q)
        + _COMMON_FILTER_CLAUSE
        + _ENTITY_FILTER_CLAUSE
        + "RETURN count(p) AS total\n"
    )


def _head(*, has_q: bool) -> str:
    if has_q:
        return (
            "CALL db.index.fulltext.queryNodes('person_name_search', $q) YIELD node AS p, score\n"
            "OPTIONAL MATCH (p)-[:LIVES_AT]->(addr:Address)\n"
            "WITH p, addr, score\n"
        )
    return (
        "MATCH (p:Person)\n"
        "OPTIONAL MATCH (p)-[:LIVES_AT]->(addr:Address)\n"
        "WITH p, addr, null AS score\n"
    )
