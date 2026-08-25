"""Read-only Cypher inventory for historical CRM-deal identity repair."""

from __future__ import annotations

INVENTORY_ACTIVE_CRM_DEALS = """
MATCH (deal:SourceRecord {record_type: 'crm_deal'})-[:FROM_SOURCE]->(
    :SourceSystem {source_key: $source_system}
)
WHERE deal.lifecycle_status = 'active'
   OR (deal.lifecycle_status IS NULL AND deal.is_latest = true)
CALL {
    WITH deal
    OPTIONAL MATCH (deal)-[link:LINKED_TO]->(owner:Person)
    WITH deal, link, owner
    WHERE link IS NOT NULL
    RETURN collect({
        person_id: owner.person_id,
        is_active: coalesce(link.is_active, true),
        relationship_type: type(link),
        relationship_properties: properties(link),
        start_endpoint: {
            labels: labels(deal),
            source_record_pk: deal.source_record_pk,
            source_record_id: deal.source_record_id
        },
        end_endpoint: {
            labels: labels(owner),
            person_id: owner.person_id
        }
    }) AS linked_people
}
CALL {
    WITH deal
    MATCH (version:SourceRecord {
        source_record_id: deal.source_record_id,
        record_type: 'crm_deal'
    })-[:FROM_SOURCE]->(:SourceSystem {source_key: $source_system})
    RETURN collect({
        source_record_pk: version.source_record_pk,
        source_record_version: version.source_record_version,
        lifecycle_status: version.lifecycle_status,
        is_latest: version.is_latest
    }) AS logical_versions
}
RETURN deal.source_record_pk AS source_record_pk,
       deal.source_record_id AS source_record_id,
       deal.source_record_version AS source_record_version,
       deal.lifecycle_status AS lifecycle_status,
       deal.is_latest AS is_latest,
       deal.record_hash AS record_hash,
       toString(deal.observed_at) AS observed_at,
       deal.raw_payload AS raw_payload,
       deal.normalized_payload AS normalized_payload,
       linked_people,
       logical_versions
ORDER BY deal.source_record_id, deal.source_record_pk
"""

INVENTORY_CRM_DEAL_PROJECTIONS = """
MATCH (start)-[projection]->(target)
WHERE projection.source_record_pk IS NOT NULL
MATCH (deal:SourceRecord {
    source_record_pk: projection.source_record_pk,
    record_type: 'crm_deal'
})-[:FROM_SOURCE]->(:SourceSystem {source_key: $source_system})
WHERE (deal.lifecycle_status = 'active'
   OR (deal.lifecycle_status IS NULL AND deal.is_latest = true))
  AND NOT (start = deal AND target:Person AND type(projection) = 'LINKED_TO')
RETURN deal.source_record_pk AS source_record_pk,
       {
         relationship_type: type(projection),
         is_active: coalesce(projection.is_active, true),
         relationship_properties: properties(projection),
         start_endpoint: {
           labels: labels(start),
           endpoint_properties: properties(start)
         },
         end_endpoint: {
           labels: labels(target),
           endpoint_properties: properties(target)
         },
         owner_person_id: start.person_id,
         identifier_type: target.identifier_type,
         identifier_value: target.normalized_value,
         address_id: target.address_id,
         target_source_record_pk: target.source_record_pk,
         source_record_pk: projection.source_record_pk
       } AS projection
UNION ALL
MATCH (deal:SourceRecord {record_type: 'crm_deal'})-[projection:DESCRIBES_ADDRESS]->(target)
MATCH (deal)-[:FROM_SOURCE]->(:SourceSystem {source_key: $source_system})
WHERE (deal.lifecycle_status = 'active'
   OR (deal.lifecycle_status IS NULL AND deal.is_latest = true))
  AND projection.source_record_pk IS NULL
RETURN deal.source_record_pk AS source_record_pk,
       {
         relationship_type: type(projection),
         is_active: coalesce(projection.is_active, true),
         relationship_properties: properties(projection),
         start_endpoint: {
           labels: labels(deal),
           endpoint_properties: properties(deal)
         },
         end_endpoint: {
           labels: labels(target),
           endpoint_properties: properties(target)
         },
         owner_person_id: null,
         identifier_type: target.identifier_type,
         identifier_value: target.normalized_value,
         address_id: target.address_id,
         target_source_record_pk: target.source_record_pk,
         source_record_pk: null
       } AS projection
"""
