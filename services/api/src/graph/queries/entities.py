"""Cypher queries for entity listing and entity-person lookup."""

from __future__ import annotations

LIST_ENTITIES = """
MATCH (e:Entity)
OPTIONAL MATCH (e)<-[:OPERATED_BY]-(ss:SourceSystem)<-[:FROM_SOURCE]-(sr:SourceRecord)
    <-[:HAS_FACT]-(p:Person)
WHERE p.status <> 'merged'
WITH e, count(DISTINCT p) AS person_count
RETURN e {
  .entity_key, .display_name, .entity_type, .country_code, .is_active
} AS entity, person_count
ORDER BY e.display_name
"""

# Allowlisted sort columns for entity persons query.
_SORT_COLUMNS: dict[str, str] = {
    "preferred_full_name": "p.preferred_full_name",
    "status": "p.status",
    "preferred_phone": "p.preferred_phone",
    "preferred_email": "p.preferred_email",
    "source_record_count": "source_record_count",
    "connection_count": "connection_count",
    "phone_confidence": "phone_confidence",
}

_DEFAULT_SORT = "preferred_full_name"
_DEFAULT_ORDER = "ASC"

_ENTITY_PERSONS_BODY = """
MATCH (e:Entity {entity_key: $entity_key})
    <-[:OPERATED_BY]-(ss:SourceSystem)<-[:FROM_SOURCE]-(sr:SourceRecord)
    <-[:HAS_FACT]-(p:Person)
WHERE p.status <> 'merged'
WITH DISTINCT p
OPTIONAL MATCH (addr:Address {address_id: p.preferred_address_id})
CALL {
  WITH p
  MATCH (p)-[:HAS_FACT]->(sr2:SourceRecord)
  RETURN count(sr2) AS source_record_count
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
  ORDER BY CASE qf
    WHEN 'valid' THEN 0
    ELSE 1
  END
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
RETURN p {
  .person_id, .status, .is_high_value, .is_high_risk,
  .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob,
  .profile_completeness_score, .created_at, .updated_at
} AS person,
addr {
  .address_id, .unit_number, .street_number, .street_name,
  .city, .postal_code, .country_code, .normalized_full
} AS preferred_address,
source_record_count, connection_count, phone_confidence
"""


def get_entity_persons_query(sort_by: str, sort_order: str) -> str:
    """Build entity-persons query with validated sort column and direction."""
    col = _SORT_COLUMNS.get(sort_by, _SORT_COLUMNS[_DEFAULT_SORT])
    direction = "DESC" if sort_order.upper() == "DESC" else "ASC"
    return f"{_ENTITY_PERSONS_BODY}ORDER BY {col} {direction}\nSKIP $skip LIMIT $limit\n"
