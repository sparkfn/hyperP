"""Cypher queries for audited NRIC reveal and event recording."""

from __future__ import annotations

GET_PERSON_NRIC_IDENTIFIERS = """
MATCH (p:Person {person_id: $person_id})
WHERE p.status <> 'merged'
OPTIONAL MATCH (p)-[rel:IDENTIFIED_BY]->(id:Identifier {identifier_type: 'nric'})
WHERE coalesce(rel.is_active, true) = true
RETURN p.person_id AS person_id,
       [item IN collect(DISTINCT CASE WHEN id IS NOT NULL THEN {
         normalized_value: id.normalized_value,
         nric_encrypted: id.nric_encrypted
       } END) WHERE item IS NOT NULL] AS identifiers
"""

CREATE_NRIC_REVEAL_EVENT = """
MATCH (p:Person {person_id: $person_id})
CREATE (re:NricRevealEvent {
  reveal_id: $reveal_id,
  person_id: $person_id,
  actor_email: $actor_email,
  reason: $reason,
  request_id: $request_id,
  revealed_at: datetime($revealed_at),
  value_count: $value_count,
  unavailable_count: $unavailable_count,
  created_at: datetime()
})
CREATE (re)-[:REVEALED_FOR]->(p)
RETURN re.reveal_id AS reveal_id
"""
