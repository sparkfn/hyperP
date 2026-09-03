"""Cypher queries for entity listing and entity-person lookup."""

from __future__ import annotations

LIST_ENTITIES = """
MATCH (e:Entity)
CALL (e) {
  CALL (e) {
    MATCH (e)<-[:OWNED_BY]-(sr:SourceRecord)-[link:LINKED_TO]->(p:Person)
    WHERE coalesce(link.is_active, true) = true
      AND (sr.history_family IS NULL OR sr.history_family = 'activity')
      AND (sr.lifecycle_status = 'active'
        OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))
      AND p.status <> 'merged'
    RETURN sr, p

    UNION ALL

    WITH e
    MATCH (e)<-[:OPERATED_BY]-(:SourceSystem)<-[:FROM_SOURCE]-(sr:SourceRecord)-[link:LINKED_TO]->(p:Person)
    WHERE coalesce(link.is_active, true) = true
      AND (sr.history_family IS NULL OR sr.history_family = 'activity')
      AND (sr.lifecycle_status = 'active'
        OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))
      AND p.status <> 'merged'
      AND NOT EXISTS { MATCH (sr)-[:OWNED_BY]->(:Entity) }
    RETURN sr, p
  }
  RETURN count(DISTINCT p) AS person_count,
         count(DISTINCT sr) AS source_record_count,
         max(sr.ingested_at) AS last_ingested_at
}
RETURN e {
  .entity_key, .display_name, .entity_type, .country_code, .is_active
} AS entity,
person_count, source_record_count, last_ingested_at, 0 AS active_review_cases
ORDER BY e.display_name
"""

LIST_ENTITY_METADATA = """
MATCH (e:Entity)
RETURN e { .entity_key, .display_name, .entity_type, .country_code, .is_active } AS entity
ORDER BY coalesce(e.display_name, e.entity_key), e.entity_key
"""

LIST_ENTITY_FILTER_OPTIONS = """
MATCH (e:Entity)
RETURN e.entity_key AS entity_key,
       e.display_name AS display_name
ORDER BY coalesce(e.display_name, e.entity_key), e.entity_key
"""

LIST_FILTER_SOURCE_SYSTEMS = """
MATCH (ss:SourceSystem)
CALL {
  WITH ss
  OPTIONAL MATCH (ss)<-[:FROM_SOURCE]-(sr:SourceRecord)-[link:LINKED_TO]->(:Person)
  WHERE coalesce(link.is_active, true) = true
    AND (sr.history_family IS NULL OR sr.history_family = 'activity')
    AND (sr.lifecycle_status = 'active'
      OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))
  RETURN count(sr) AS source_record_count,
         max(sr.ingested_at) AS last_ingested_at
}
RETURN ss {
  .source_key, .display_name, .system_type, .is_active
} AS source_system,
source_record_count, last_ingested_at
ORDER BY ss.display_name
"""

_SORT_COLUMNS: dict[str, str] = {
    "preferred_full_name": "p.preferred_full_name",
    "status": "p.status",
    "preferred_phone": "p.preferred_phone",
    "preferred_email": "p.preferred_email",
    "source_record_count": "source_record_count",
    "connection_count": "connection_count",
    "phone_confidence": "phone_confidence",
}
_NATIVE_SORTS = frozenset({"preferred_full_name", "status", "preferred_phone", "preferred_email"})
_DEFAULT_SORT = "preferred_full_name"

_ENTITY_PERSONS_HEAD = """
MATCH (e:Entity {entity_key: $entity_key})
CALL (e) {
  MATCH (e)<-[:OWNED_BY]-(sr:SourceRecord)<-[entity_fact:HAS_FACT]-(p:Person)
  WHERE p.status <> 'merged'
    AND coalesce(entity_fact.is_active, true) = true
    AND (sr.history_family IS NULL OR sr.history_family = 'activity')
    AND (sr.lifecycle_status = 'active'
      OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))
  RETURN p

  UNION ALL

  WITH e
  MATCH (e)<-[:OPERATED_BY]-(:SourceSystem)<-[:FROM_SOURCE]-(sr:SourceRecord)<-[entity_fact:HAS_FACT]-(p:Person)
  WHERE p.status <> 'merged'
    AND coalesce(entity_fact.is_active, true) = true
    AND (sr.history_family IS NULL OR sr.history_family = 'activity')
    AND (sr.lifecycle_status = 'active'
      OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))
    AND NOT EXISTS { MATCH (sr)-[:OWNED_BY]->(:Entity) }
  RETURN p
}
"""

_SOURCE_RECORD_COUNT = """
CALL (p) {
  MATCH (p)-[fact:HAS_FACT]->(sr2:SourceRecord)
  WHERE coalesce(fact.is_active, true) = true
  RETURN count(sr2) AS source_record_count
}
"""

_CONNECTION_COUNT = """
CALL (p) {
  OPTIONAL MATCH (p)-[p_id:IDENTIFIED_BY]->(:Identifier)
    <-[c_id:IDENTIFIED_BY]-(ci:Person)
    WHERE coalesce(p_id.is_active, true) = true
      AND coalesce(c_id.is_active, true) = true
      AND ci.person_id <> p.person_id AND ci.status <> 'merged'
  OPTIONAL MATCH (p)-[p_addr:LIVES_AT]->(:Address)
    <-[c_addr:LIVES_AT]-(ca:Person)
    WHERE coalesce(p_addr.is_active, true) = true
      AND coalesce(c_addr.is_active, true) = true
      AND ca.person_id <> p.person_id AND ca.status <> 'merged'
  OPTIONAL MATCH (p)-[p_knows:KNOWS]-(ck:Person)
    WHERE coalesce(p_knows.is_active, true) = true
      AND ck.person_id <> p.person_id AND ck.status <> 'merged'
  WITH collect(DISTINCT ci) + collect(DISTINCT ca) + collect(DISTINCT ck) AS all_conn
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

_METRICS: dict[str, str] = {
    "source_record_count": _SOURCE_RECORD_COUNT,
    "connection_count": _CONNECTION_COUNT,
    "phone_confidence": _PHONE_CONFIDENCE,
}

_RETURN = """
OPTIONAL MATCH (addr:Address {address_id: p.preferred_address_id})
RETURN p {
  .person_id, .status, .is_high_value, .is_high_risk,
  .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob,
  .preferred_race_ethnicity,
  .profile_completeness_score, .created_at, .updated_at
} AS person,
addr {
  .address_id, .unit_number, .street_number, .street_name,
  .city, .postal_code, .country_code, .normalized_full
} AS preferred_address,
source_record_count, connection_count, phone_confidence
"""


def _enrich(excluded: str | None = None) -> str:
    return "".join(call for key, call in _METRICS.items() if key != excluded)


def get_entity_persons_query(sort_by: str, sort_order: str) -> str:
    """Build an Entity-scoped Person query that paginates before page enrichment."""
    key = sort_by if sort_by in _SORT_COLUMNS else _DEFAULT_SORT
    direction = "DESC" if sort_order.upper() == "DESC" else "ASC"
    if key in _NATIVE_SORTS:
        final_col = _SORT_COLUMNS[key].replace("p.", "person.")
        return (
            _ENTITY_PERSONS_HEAD
            + f"WITH DISTINCT p\nORDER BY {_SORT_COLUMNS[key]} {direction}, p.person_id ASC\n"
            + "SKIP $skip LIMIT $limit\n"
            + _enrich()
            + _RETURN
            + f"ORDER BY {final_col} {direction}, person.person_id ASC\n"
        )
    return (
        _ENTITY_PERSONS_HEAD
        + "WITH DISTINCT p\n"
        + _METRICS[key]
        + f"WITH p, {key}\nORDER BY {key} {direction}, p.person_id ASC\n"
        + "SKIP $skip LIMIT $limit\n"
        + _enrich(excluded=key)
        + _RETURN
        + f"ORDER BY {key} {direction}, person.person_id ASC\n"
    )
