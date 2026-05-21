"""Cypher constants for bankruptcy case materialization."""

from __future__ import annotations

MERGE_BANKRUPTCY_CASE = """
MATCH (p:Person {person_id: $person_id})
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
MERGE (bc:BankruptcyCase {
    source_system_key: $source_system_key,
    source_case_id: $source_case_id
})
ON CREATE SET
    bc.bankruptcy_case_id = randomUUID(),
    bc.created_at = datetime()
SET
    bc.case_number = $case_number,
    bc.document_type = $document_type,
    bc.document_date = $document_date,
    bc.event_type = $event_type,
    bc.event_date = $event_date,
    bc.trustee_name = $trustee_name,
    bc.trustee_firm = $trustee_firm,
    bc.source_url = $source_url,
    bc.first_seen_at = CASE WHEN $first_seen_at IS NULL THEN null ELSE datetime($first_seen_at) END,
    bc.last_seen_at = CASE WHEN $last_seen_at IS NULL THEN null ELSE datetime($last_seen_at) END,
    bc.raw_payload = $raw_payload,
    bc.updated_at = datetime()
MERGE (p)-[person_rel:HAS_BANKRUPTCY_CASE]->(bc)
ON CREATE SET
    person_rel.first_seen_at = datetime(),
    person_rel.source_record_pk = $source_record_pk,
    person_rel.observed_at = datetime($observed_at)
ON MATCH SET
    person_rel.source_record_pk = $source_record_pk,
    person_rel.observed_at = datetime($observed_at),
    person_rel.last_seen_at = datetime()
MERGE (sr)-[record_rel:DESCRIBES_CASE]->(bc)
ON CREATE SET record_rel.linked_at = datetime()
"""
