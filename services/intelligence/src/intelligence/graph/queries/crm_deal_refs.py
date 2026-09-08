"""Parameterized MATCH-only Cypher for the bounded CRM deal-reference export."""

VALIDATE_SOURCE_INSTANCE = """
MATCH (instance:BitrixSourceInstance {
  source_key: 'bitrix_chat', source_instance_id: $source_instance_id, status: 'active'
})-[:INSTANCE_OF]->(:SourceSystem {source_key: 'bitrix_chat', is_active: true})
RETURN instance.source_instance_id AS source_instance_id
"""

# This intentionally does not reuse GET_IDENTITY_LINK_COUNTER because that operational helper uses MERGE.
READ_IDENTITY_BOUNDARY = """
OPTIONAL MATCH (counter:IdentityLinkRevisionCounter {stream_key: 'identity_link_revision_stream_v1'})
OPTIONAL MATCH (migration:DataMigration {migration_key: 'identity_link_revision_baseline_v2'})
RETURN coalesce(counter.current_revision, 0) AS current_revision,
       counter.baseline_completed_at IS NOT NULL OR migration.completed_at IS NOT NULL AS baseline_ready
"""

LIST_DEAL_REFERENCE_PAGE = """
MATCH (record:SourceRecord {record_type: 'crm_deal'})-[:FROM_SOURCE]->
      (:SourceSystem {source_key: 'bitrix_chat'})
WHERE record.source_instance_id = $source_instance_id
  AND (record.ingested_at IS NULL OR record.ingested_at <= datetime($as_of))
  AND ($after_source_record_id IS NULL
       OR record.source_record_id > $after_source_record_id
       OR (record.source_record_id = $after_source_record_id
           AND toInteger(record.source_record_version) > $after_source_record_version)
       OR (record.source_record_id = $after_source_record_id
           AND toInteger(record.source_record_version) = $after_source_record_version
           AND record.source_record_pk > $after_source_record_pk))
OPTIONAL MATCH (record)-[:OWNED_BY]->(entity:Entity)
WITH record, collect(DISTINCT entity.entity_key) AS entity_keys,
     count(DISTINCT entity) AS entity_count
RETURN record.source_record_id AS source_record_id,
       toInteger(record.source_record_version) AS source_record_version,
       record.source_record_pk AS source_record_pk,
       record.record_hash AS record_hash,
       record.source_entity_type AS source_entity_type,
       record.source_entity_id AS source_entity_id,
       record.identity_policy_version AS identity_policy_version,
       record.entity_key AS record_entity_key,
       entity_keys AS owned_entity_keys,
       entity_count AS owned_entity_count,
       record.crm_deal_stage_id AS stage_id,
       toString(record.observed_at) AS observed_at,
       toString(record.ingested_at) AS ingested_at,
       record.lifecycle_status AS lifecycle_status,
       record.link_status AS link_status,
       CASE WHEN record.raw_payload IS NULL
                 OR size(record.raw_payload) <= $max_raw_payload_chars
            THEN record.raw_payload
            ELSE NULL END AS raw_payload,
       record.raw_payload IS NOT NULL
         AND size(record.raw_payload) > $max_raw_payload_chars AS raw_payload_oversize
ORDER BY record.source_record_id, toInteger(record.source_record_version), record.source_record_pk
LIMIT $limit
"""

LIST_IDENTITY_REVISION_PAGE = """
MATCH (revision:IdentityLinkRevision {
  source_system: 'bitrix_chat', source_entity_type: 'deal', identity_policy_version: $identity_policy_version
})
WHERE revision.source_instance_id = $source_instance_id
  AND revision.source_entity_id IN $source_entity_ids
  AND revision.global_revision <= $through_revision
  AND (revision.created_at IS NULL OR revision.created_at <= datetime($as_of))
  AND ($after_global_revision IS NULL OR revision.global_revision > $after_global_revision)
OPTIONAL MATCH (person:Person {person_id: revision.hyperp_person_id})
RETURN revision.event_id AS event_id,
       revision.global_revision AS global_revision,
       revision.source_instance_id AS source_instance_id,
       revision.source_entity_id AS source_entity_id,
       revision.identity_policy_version AS identity_policy_version,
       revision.link_status AS link_status,
       revision.hyperp_person_id AS hyperp_person_id,
       person.status AS person_status,
       revision.resolution_kind AS resolution_kind,
       revision.resolution_revision AS resolution_revision,
       toString(revision.effective_at) AS effective_at,
       toString(revision.created_at) AS created_at
ORDER BY revision.global_revision
LIMIT $limit
"""
