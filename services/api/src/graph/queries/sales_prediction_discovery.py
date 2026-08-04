"""Read-only Cypher used by sales-prediction feasibility discovery."""

from __future__ import annotations

DISCOVERY_SOURCE_COVERAGE = """
MATCH (source:SourceSystem)
OPTIONAL MATCH (record:SourceRecord)-[:FROM_SOURCE]->(source)
WHERE record.ingested_at IS NULL OR record.ingested_at <= datetime($as_of_at)
WITH source, record
WHERE $entity_keys = [] OR record.entity_key IN $entity_keys
RETURN source.source_key AS source_key,
       coalesce(record.entity_key, '') AS entity_key,
       coalesce(record.record_type, '') AS record_type,
       count(record) AS record_count,
       count(CASE WHEN record IS NOT NULL AND record.observed_at IS NULL THEN 1 END)
           AS missing_observed_at_count,
       count(CASE WHEN record IS NOT NULL AND record.ingested_at IS NULL THEN 1 END)
           AS missing_ingested_at_count,
       count(CASE
           WHEN record IS NOT NULL
            AND coalesce(record.lifecycle_status, 'active') = 'active'
           THEN 1
       END)
           AS active_record_count,
       count(CASE WHEN record.lifecycle_status = 'superseded' THEN 1 END)
           AS superseded_record_count
ORDER BY entity_key, source_key, record_type
"""

DISCOVERY_DEAL_RECORDS = """
MATCH (record:SourceRecord {record_type: 'crm_deal'})-[:FROM_SOURCE]->(source:SourceSystem)
WHERE record.ingested_at IS NOT NULL
  AND record.ingested_at <= datetime($as_of_at)
  AND ($entity_keys = [] OR record.entity_key IN $entity_keys)
OPTIONAL MATCH (record)-[:LINKED_TO]->(person:Person)
RETURN coalesce(record.entity_key, '') AS entity_key,
       source.source_key AS source_key,
       record.source_record_id AS logical_record_id,
       toInteger(coalesce(record.source_record_version, '1')) AS source_record_version,
       coalesce(record.lifecycle_status, 'active') AS lifecycle_status,
       toString(record.observed_at) AS observed_at,
       toString(record.ingested_at) AS ingested_at,
       toString(record.activated_at) AS activated_at,
       toString(record.superseded_at) AS superseded_at,
       toString(record.rejected_at) AS rejected_at,
       toString(record.link_failed_at) AS link_failed_at,
       record.raw_payload AS raw_payload,
       count(DISTINCT person) AS linked_person_count
ORDER BY source_key, logical_record_id, source_record_version
"""

DISCOVERY_INTERACTION_RECORDS = """
MATCH (record:SourceRecord)-[:FROM_SOURCE]->(source:SourceSystem)
WHERE record.record_type IN ['conversation', 'call', 'crm_history']
  AND record.ingested_at IS NOT NULL
  AND record.ingested_at <= datetime($as_of_at)
  AND ($entity_keys = [] OR record.entity_key IN $entity_keys)
RETURN coalesce(record.entity_key, '') AS entity_key,
       source.source_key AS source_key,
       record.record_type AS record_type,
       record.source_record_id AS logical_record_id,
       toInteger(coalesce(record.source_record_version, '1')) AS source_record_version,
       coalesce(record.lifecycle_status, 'active') AS lifecycle_status,
       toString(record.observed_at) AS observed_at,
       toString(record.ingested_at) AS ingested_at,
       toString(record.activated_at) AS activated_at,
       toString(record.superseded_at) AS superseded_at,
       toString(record.rejected_at) AS rejected_at,
       toString(record.link_failed_at) AS link_failed_at,
       record.extraction_confidence AS extraction_confidence,
       record.raw_payload AS raw_payload
ORDER BY source_key, record_type, logical_record_id, source_record_version
"""

DISCOVERY_SALES_RECORDS = """
MATCH (record:SourceRecord {record_type: 'sales'})-[:FROM_SOURCE]->(source:SourceSystem)
WHERE record.ingested_at IS NOT NULL
  AND record.ingested_at <= datetime($as_of_at)
  AND ($entity_keys = [] OR record.entity_key IN $entity_keys)
OPTIONAL MATCH (record)-[:FOR_CUSTOMER_RECORD]->(:SourceRecord)-[:LINKED_TO]->(person:Person)
RETURN coalesce(record.entity_key, '') AS entity_key,
       source.source_key AS source_key,
       record.source_record_id AS logical_record_id,
       toInteger(coalesce(record.source_record_version, '1')) AS source_record_version,
       coalesce(record.lifecycle_status, 'active') AS lifecycle_status,
       toString(record.observed_at) AS observed_at,
       toString(record.ingested_at) AS ingested_at,
       toString(record.activated_at) AS activated_at,
       toString(record.superseded_at) AS superseded_at,
       toString(record.rejected_at) AS rejected_at,
       toString(record.link_failed_at) AS link_failed_at,
       record.raw_payload AS raw_payload,
       count(DISTINCT person) AS linked_person_count
ORDER BY source_key, logical_record_id, source_record_version
"""

DISCOVERY_DEAL_ORDER_LINKAGE = """
MATCH (deal:SourceRecord {record_type: 'crm_deal'})
WHERE deal.ingested_at IS NOT NULL
  AND deal.ingested_at <= datetime($as_of_at)
  AND ($entity_keys = [] OR deal.entity_key IN $entity_keys)
OPTIONAL MATCH (deal)-[relationship]-(order:Order)
RETURN coalesce(deal.entity_key, '') AS entity_key,
       count(DISTINCT deal.source_record_id) AS logical_deal_count,
       count(DISTINCT CASE WHEN order IS NOT NULL THEN deal.source_record_id END)
           AS directly_linked_deal_count,
       count(DISTINCT order) AS directly_linked_order_count
ORDER BY entity_key
"""

DISCOVERY_LATE_ARRIVAL = """
MATCH (record:SourceRecord)
WHERE record.observed_at IS NOT NULL
  AND record.ingested_at IS NOT NULL
  AND record.ingested_at <= datetime($as_of_at)
  AND ($entity_keys = [] OR record.entity_key IN $entity_keys)
WITH record,
     duration.inSeconds(record.observed_at, record.ingested_at).seconds AS delay_seconds
RETURN coalesce(record.entity_key, '') AS entity_key,
       record.record_type AS record_type,
       count(*) AS record_count,
       count(CASE WHEN delay_seconds < 0 THEN 1 END) AS negative_delay_count,
       count(CASE WHEN delay_seconds > $late_arrival_seconds THEN 1 END) AS late_arrival_count,
       max(delay_seconds) AS max_delay_seconds
ORDER BY entity_key, record_type
"""
