"""Cypher statements for immutable identity assertion edges and audits."""

from __future__ import annotations

MERGE_PROFILE_FACTS_BATCH = """
UNWIND $facts AS input
MERGE (fact:ProfileFact {
    attribute_name: input.attribute_name,
    attribute_value: input.attribute_value
})
ON CREATE SET fact.created_at = datetime()
RETURN count(fact) AS upserted_count
"""

MERGE_ASSERTION_IDENTIFIERS_BATCH = """
UNWIND $identifiers AS input
MERGE (id:Identifier {
    identifier_type: input.identifier_type,
    identifier_scope: input.identifier_scope,
    normalized_value: input.normalized_value
})
ON CREATE SET
    id.identifier_id = randomUUID(),
    id.created_at = datetime()
RETURN count(id) AS upserted_count
"""

MERGE_ASSERTION_ADDRESSES_BATCH = """
UNWIND $addresses AS input
MERGE (addr:Address {
    country_code: input.country_code,
    postal_code: input.postal_code,
    street_name: input.street_name,
    street_number: input.street_number,
    unit_number: input.unit_number
})
ON CREATE SET
    addr.address_id = randomUUID(),
    addr.created_at = datetime()
RETURN count(addr) AS upserted_count
"""

ASSERT_IDENTIFIERS = """
UNWIND $assertions AS input
MATCH (sr:SourceRecord {source_record_pk: input.source_record_pk})
MATCH (target:Identifier {
    identifier_type: input.identifier_type,
    identifier_scope: input.identifier_scope,
    normalized_value: input.normalized_value
})
MERGE (sr)-[assertion:ASSERTS {assertion_id: input.assertion_id}]->(target)
ON CREATE SET assertion.field_path = input.field_path,
              assertion.role = 'subject',
              assertion.created_at = datetime()
RETURN count(assertion) AS assertion_count
"""

ASSERT_ADDRESSES = """
UNWIND $assertions AS input
MATCH (sr:SourceRecord {source_record_pk: input.source_record_pk})
MATCH (target:Address {
    country_code: input.country_code,
    postal_code: input.postal_code,
    street_name: input.street_name,
    street_number: input.street_number,
    unit_number: input.unit_number
})
MERGE (sr)-[assertion:ASSERTS {assertion_id: input.assertion_id}]->(target)
ON CREATE SET assertion.field_path = input.field_path,
              assertion.role = 'subject',
              assertion.created_at = datetime()
RETURN count(assertion) AS assertion_count
"""

ASSERT_FACTS = """
UNWIND $assertions AS input
MATCH (sr:SourceRecord {source_record_pk: input.source_record_pk})
MATCH (target:ProfileFact {
    attribute_name: input.attribute_name,
    attribute_value: input.attribute_value
})
MERGE (sr)-[assertion:ASSERTS {assertion_id: input.assertion_id}]->(target)
ON CREATE SET assertion.field_path = input.field_path,
              assertion.role = 'subject',
              assertion.created_at = datetime()
RETURN count(assertion) AS assertion_count
"""

BACKFILL_ASSERTION_PAGE = """
MATCH (sr:SourceRecord)
WHERE (sr.lifecycle_status IN ['active', 'pending_review', 'parked']
       OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))
  AND ($after_source_record_pk IS NULL
       OR sr.source_record_pk > $after_source_record_pk)
WITH sr ORDER BY sr.source_record_pk LIMIT $batch_size
CALL {
    WITH sr
    OPTIONAL MATCH (:Person)-[edge:IDENTIFIED_BY]->(identifier:Identifier)
    WHERE edge.source_record_pk = sr.source_record_pk
    RETURN collect(DISTINCT CASE WHEN identifier IS NULL THEN null ELSE {
        identifier_type: identifier.identifier_type,
        identifier_scope: identifier.identifier_scope,
        normalized_value: identifier.normalized_value,
        source_instance_id: identifier.source_instance_id,
        quality_flag: coalesce(edge.quality_flag, 'valid')
    } END) AS fallback_identifiers
}
RETURN sr.source_record_pk AS source_record_pk,
       sr.normalized_payload AS normalized_payload,
       fallback_identifiers
ORDER BY sr.source_record_pk
"""

AUDIT_ASSERTION_PAGE = """
MATCH (sr:SourceRecord)
WHERE (sr.lifecycle_status IN ['active', 'pending_review', 'parked']
       OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))
  AND ($after_source_record_pk IS NULL
       OR sr.source_record_pk > $after_source_record_pk)
WITH sr ORDER BY sr.source_record_pk LIMIT $batch_size
CALL {
    WITH sr
    OPTIONAL MATCH (sr)-[assertion:ASSERTS]->(:Identifier)
    RETURN count(assertion) AS identifier_count
}
CALL {
    WITH sr
    OPTIONAL MATCH (sr)-[assertion:ASSERTS]->(:Address)
    RETURN count(assertion) AS address_count
}
CALL {
    WITH sr
    OPTIONAL MATCH (sr)-[assertion:ASSERTS]->(:ProfileFact)
    RETURN count(assertion) AS fact_count
}
CALL {
    WITH sr
    OPTIONAL MATCH (:Person)-[edge:IDENTIFIED_BY]->(identifier:Identifier)
    WHERE edge.source_record_pk = sr.source_record_pk
    RETURN collect(DISTINCT CASE WHEN identifier IS NULL THEN null ELSE {
        identifier_type: identifier.identifier_type,
        identifier_scope: identifier.identifier_scope,
        normalized_value: identifier.normalized_value,
        source_instance_id: identifier.source_instance_id,
        quality_flag: coalesce(edge.quality_flag, 'valid')
    } END) AS fallback_identifiers
}
RETURN sr.source_record_pk AS source_record_pk,
       sr.normalized_payload AS normalized_payload,
       fallback_identifiers,
       identifier_count,
       address_count,
       fact_count
ORDER BY sr.source_record_pk
"""
