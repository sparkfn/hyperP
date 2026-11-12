"""Cypher queries for NRIC coverage, validity, and conflict metrics."""

from __future__ import annotations

FETCH_ACTIVE_PERSON_COUNT = """
MATCH (p:Person {status: 'active'})
RETURN count(p) AS active_person_count
"""

FETCH_NRIC_REVEAL_EVENT_COUNT = """
MATCH (e:NricRevealEvent)
RETURN count(e) AS reveal_count
"""

FETCH_ACTIVE_PERSON_NRIC_ROWS = """
MATCH (p:Person {status: 'active'})-[rel:IDENTIFIED_BY]->(id:Identifier {identifier_type: 'nric'})
WHERE coalesce(rel.is_active, true) = true
RETURN p.person_id AS person_id,
       id.normalized_value AS normalized_value,
       id.checksum_valid AS checksum_valid,
       rel.source_system_key AS source_system_key
"""
