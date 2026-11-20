"""Parameterized reads for the bounded existing-Person NRIC anchor pass."""

from __future__ import annotations

NRIC_REHASH_ANCHOR_PAGE = """
MATCH (person:Person {status: 'active'})
WHERE ($after_person_id IS NULL OR person.person_id > $after_person_id)
  AND (
    EXISTS {
      MATCH (person)-[edge:IDENTIFIED_BY]->(:Identifier {identifier_type: 'nric'})
      WHERE coalesce(edge.is_active, true)
    }
    OR EXISTS {
      MATCH (person)-[edge:HAS_IDENTIFIER]->(:Identifier {identifier_type: 'nric'})
      WHERE edge.status = 'active'
    }
    OR EXISTS {
      MATCH (record:SourceRecord)-[link:LINKED_TO]->(person)
      WHERE link.is_active = true
      MATCH (record)-[:ASSERTS]->(:Identifier {identifier_type: 'nric'})
    }
  )
WITH person ORDER BY person.person_id
LIMIT $batch_size
CALL (person) {
  OPTIONAL MATCH (person)-[edge:IDENTIFIED_BY]->(identifier:Identifier {identifier_type: 'nric'})
  WHERE coalesce(edge.is_active, true)
  RETURN collect(DISTINCT identifier) AS identified
}
CALL (person) {
  OPTIONAL MATCH (person)-[edge:HAS_IDENTIFIER]->(identifier:Identifier {identifier_type: 'nric'})
  WHERE edge.status = 'active'
  RETURN collect(DISTINCT identifier) AS projected
}
CALL (person) {
  OPTIONAL MATCH (record:SourceRecord)-[link:LINKED_TO]->(person)
  WHERE link.is_active = true
  MATCH (record)-[:ASSERTS]->(identifier:Identifier {identifier_type: 'nric'})
  RETURN collect(DISTINCT identifier) AS asserted
}
WITH person,
     [identifier IN identified + projected + asserted WHERE identifier IS NOT NULL]
       AS candidates
UNWIND candidates AS candidate
WITH person, collect(DISTINCT candidate) AS identifiers
CALL (person, identifiers) {
  UNWIND identifiers AS identifier
  CALL (person, identifier) {
    MATCH (other:Person)
    WHERE other <> person AND other.nric_key = identifier.normalized_value
    RETURN count(DISTINCT other) AS other_key_holders
  }
  CALL (person, identifier) {
    OPTIONAL MATCH (identifier)<-[identified:IDENTIFIED_BY]-(other:Person {status: 'active'})
    WHERE coalesce(identified.is_active, true) AND other <> person
    WITH person, identifier, collect(DISTINCT other) AS identified_owners
    OPTIONAL MATCH (identifier)<-[projection:HAS_IDENTIFIER]-(other:Person {status: 'active'})
    WHERE projection.status = 'active' AND other <> person
    WITH person, identifier, identified_owners, collect(DISTINCT other) AS projected_owners
    OPTIONAL MATCH (record:SourceRecord)-[:ASSERTS]->(identifier)
    OPTIONAL MATCH (record)-[link:LINKED_TO]->(other:Person {status: 'active'})
    WHERE link.is_active = true AND other <> person
    WITH identified_owners, projected_owners, collect(DISTINCT other) AS linked_owners
    WITH identified_owners + projected_owners + linked_owners AS owners
    UNWIND owners AS owner
    RETURN count(DISTINCT owner) AS other_active_evidence
  }
  RETURN collect({
    identifier_id: identifier.identifier_id,
    normalized_value: identifier.normalized_value,
    checksum_valid: identifier.checksum_valid,
    other_key_holders: other_key_holders,
    other_active_evidence: other_active_evidence
  }) AS identifier_evidence
}
CALL (person, identifiers) {
  UNWIND identifiers AS identifier
  MATCH (record:SourceRecord)-[:ASSERTS]->(identifier)
  WHERE record.source_record_pk IS NOT NULL
    AND (
      EXISTS {
        MATCH (record)-[linked:LINKED_TO]->(person)
        WHERE linked.is_active = true
      }
      OR EXISTS {
        MATCH (person)-[identified:IDENTIFIED_BY]->(identifier)
        WHERE coalesce(identified.is_active, true)
          AND identified.source_record_pk = record.source_record_pk
      }
    )
  OPTIONAL MATCH (record)-[:FROM_SOURCE]->(system:SourceSystem)
  WITH DISTINCT record, system
  ORDER BY record.source_record_pk
  LIMIT 201
  WITH collect({
    source_record_pk: record.source_record_pk,
    source_key: system.source_key,
    record_type: record.record_type,
    normalized_payload: record.normalized_payload
  }) AS all_records
  RETURN all_records[0..200] AS records, size(all_records) > 200 AS evidence_truncated
}
WITH person, identifiers, identifier_evidence, records, evidence_truncated,
     EXISTS {
       MATCH (person)-[:ANCHORED_BY]->(anchor:Identifier {identifier_type: 'nric'})
       WHERE anchor IN identifiers
     } AS has_anchor
RETURN person.person_id AS person_id,
       person.nric_key AS nric_key,
       has_anchor,
       identifier_evidence,
       records,
       evidence_truncated
ORDER BY person.person_id
"""
