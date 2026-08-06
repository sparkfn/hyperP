"""Cypher for immutable Bitrix CRM activities and their detailed calls."""

from __future__ import annotations

FIND_ANY_SOURCE_RECORD = """
MATCH (sr:SourceRecord {source_record_id: $source_record_id})
      -[:FROM_SOURCE]->(:SourceSystem {source_key: $source_system})
RETURN sr.source_record_pk AS source_record_pk
      ,sr.record_hash AS record_hash
      ,sr.history_family AS history_family
      ,sr.history_kind AS history_kind
      ,sr.history_source AS history_source
      ,toString(sr.event_at) AS event_at
      ,sr.projection_version AS projection_version
      ,sr.projection_source AS projection_source
LIMIT 1
"""

REMATERIALIZE_CRM_HISTORY_PROJECTION = """
MATCH (history:SourceRecord {
  source_record_pk: $source_record_pk,
  record_type: 'crm_history',
  record_hash: $record_hash
})
WHERE coalesce(history.projection_version, 0) < $projection_version
FOREACH (_ IN CASE WHEN history.projection_version IS NULL THEN [] ELSE [1] END |
  CREATE (prior:CrmHistoryProjection {
    source_record_pk: history.source_record_pk,
    projection_version: history.projection_version,
    history_family: history.history_family,
    history_kind: history.history_kind,
    history_source: history.history_source,
    event_at: history.event_at,
    projection_source: history.projection_source,
    superseded_at: datetime()
  })
  CREATE (prior)-[:PRIOR_PROJECTION_OF]->(history)
)
SET history.history_family = $history_family,
    history.history_kind = $history_kind,
    history.history_source = $history_source,
    history.event_category_id = null,
    history.event_stage_id = null,
    history.event_at = CASE WHEN $event_at IS NULL THEN null ELSE datetime($event_at) END,
    history.projection_version = $projection_version,
    history.projection_time = datetime(),
    history.projection_source = $projection_source
RETURN history.source_record_pk AS source_record_pk
"""

CREATE_CRM_HISTORY = """
MATCH (ss:SourceSystem {source_key: $source_system})
MATCH (parent:SourceRecord {
    source_record_id: $parent_source_record_id,
    record_type: 'crm_deal'
})-[:FROM_SOURCE]->(:SourceSystem {source_key: $parent_source_system})
WHERE parent.lifecycle_status IN ['active', 'pending_review']
MATCH (parent)-[:OWNED_BY]->(entity:Entity)
WHERE entity.entity_key = parent.entity_key
WITH ss, parent, entity
ORDER BY CASE parent.lifecycle_status WHEN 'active' THEN 0 ELSE 1 END,
         toInteger(parent.source_record_version) DESC
LIMIT 1
CREATE (history:SourceRecord {
    source_record_pk: randomUUID(),
    source_record_id: $source_record_id,
    source_record_version: '1',
    source_version_key: $source_version_key,
    entity_key: parent.entity_key,
    expected_active_source_record_pk: null,
    lifecycle_status: 'active',
    record_type: 'crm_history',
    extraction_confidence: null,
    extraction_method: null,
    conversation_ref: null,
    parent_source_system: $parent_source_system,
    parent_source_record_id: $parent_source_record_id,
    parent_record_type: 'crm_deal',
    link_status: 'linked',
    observed_at: datetime($observed_at),
    ingested_at: datetime(),
    record_hash: $record_hash,
    raw_payload: $raw_payload,
    history_family: $history_family,
    history_kind: $history_kind,
    history_source: $history_source,
    event_category_id: null,
    event_stage_id: null,
    event_at: CASE WHEN $event_at IS NULL THEN null ELSE datetime($event_at) END,
    projection_version: $projection_version,
    projection_time: datetime(),
    projection_source: $projection_source,
    normalized_payload: '{}',
    is_latest: true,
    retention_expires_at: null
})-[:FROM_SOURCE]->(ss)
FOREACH (_ IN CASE WHEN entity IS NULL THEN [] ELSE [1] END |
    CREATE (history)-[:OWNED_BY]->(entity)
)
CREATE (history)-[:CHILD_OF]->(parent)
RETURN history.source_record_pk AS source_record_pk
"""

CREATE_CALL_FROM_HISTORY = """
MATCH (ss:SourceSystem {source_key: $source_system})
MATCH (history:SourceRecord {
    source_record_id: $parent_source_record_id,
    record_type: 'crm_history'
})-[:FROM_SOURCE]->(:SourceSystem {source_key: $parent_source_system})
MATCH (history)-[:CHILD_OF]->(origin_deal:SourceRecord {record_type: 'crm_deal'})
      -[:FROM_SOURCE]->(deal_source:SourceSystem)
MATCH (deal:SourceRecord {
    source_record_id: origin_deal.source_record_id,
    record_type: 'crm_deal'
})-[:FROM_SOURCE]->(deal_source)
WHERE deal.lifecycle_status IN ['active', 'pending_review']
WITH ss, history, deal
ORDER BY CASE deal.lifecycle_status WHEN 'active' THEN 0 ELSE 1 END,
         toInteger(deal.source_record_version) DESC
LIMIT 1
MATCH (deal)-[:LINKED_TO]->(person:Person)
MATCH (deal)-[:OWNED_BY]->(entity:Entity)
WHERE entity.entity_key = deal.entity_key
WITH ss, history, deal, entity, collect(DISTINCT person) AS people
CREATE (call:SourceRecord {
    source_record_pk: randomUUID(),
    source_record_id: $source_record_id,
    source_record_version: '1',
    source_version_key: $source_version_key,
    entity_key: deal.entity_key,
    expected_active_source_record_pk: null,
    lifecycle_status: CASE deal.lifecycle_status
        WHEN 'pending_review' THEN 'pending_review'
        ELSE 'active'
    END,
    record_type: 'call',
    extraction_confidence: null,
    extraction_method: null,
    conversation_ref: null,
    parent_source_system: $parent_source_system,
    parent_source_record_id: $parent_source_record_id,
    parent_record_type: 'crm_history',
    link_status: CASE deal.lifecycle_status
        WHEN 'pending_review' THEN 'pending_review'
        ELSE 'linked'
    END,
    observed_at: datetime($observed_at),
    ingested_at: datetime(),
    record_hash: $record_hash,
    raw_payload: $raw_payload,
    normalized_payload: '{}',
    is_latest: true,
    retention_expires_at: null
})-[:FROM_SOURCE]->(ss)
FOREACH (_ IN CASE WHEN entity IS NULL THEN [] ELSE [1] END |
    CREATE (call)-[:OWNED_BY]->(entity)
)
CREATE (call)-[:CHILD_OF]->(history)
CREATE (call)-[:DETAILS_HISTORY_ITEM {crm_activity_id: $crm_activity_id}]->(history)
FOREACH (person IN people |
    CREATE (call)-[:LINKED_TO {linked_at: datetime()}]->(person)
)
RETURN call.source_record_pk AS source_record_pk,
       [person IN people | person.person_id][0] AS person_id,
       deal.lifecycle_status AS parent_lifecycle_status
LIMIT 1
"""

LINK_CONVERSATION_TO_CRM_HISTORY = """
MATCH (conversation:SourceRecord {source_record_pk: $conversation_source_record_pk})
UNWIND $crm_activity_ids AS crm_activity_id
MATCH (history:SourceRecord {
    source_record_id: 'bitrix-crm-history-' + crm_activity_id,
    record_type: 'crm_history'
})-[:FROM_SOURCE]->(:SourceSystem {source_key: $source_system})
MERGE (history)-[:LINKED_TO]->(conversation)
MERGE (conversation)-[:REPRESENTS_HISTORY_ITEM {
    crm_activity_id: crm_activity_id,
    link_method: 'crm_activity_id'
}]->(history)
RETURN count(history) AS linked_history_count
"""

LINK_CRM_HISTORY_TO_EXISTING_CONVERSATIONS = """
MATCH (history:SourceRecord {source_record_pk: $history_source_record_pk})
MATCH (conversation:SourceRecord {
    record_type: 'conversation',
    is_latest: true
})-[:FROM_SOURCE]->(:SourceSystem {source_key: $source_system})
WHERE conversation.source_record_id STARTS WITH
      'bitrix-openlines-chat-' + toString($bitrix_chat_id) + '-'
MERGE (history)-[:LINKED_TO]->(conversation)
MERGE (conversation)-[:REPRESENTS_HISTORY_ITEM {
    crm_activity_id: $crm_activity_id,
    link_method: 'crm_activity_id'
}]->(history)
RETURN count(conversation) AS linked_conversation_count
"""

ACTIVATE_PENDING_CALLS_FOR_DEAL = """
MATCH (deal:SourceRecord {
    source_record_pk: $deal_source_record_pk,
    record_type: 'crm_deal',
    lifecycle_status: 'active'
})-[:FROM_SOURCE]->(source:SourceSystem)
MATCH (deal)-[:LINKED_TO]->(person:Person)
WITH deal, source, collect(DISTINCT person) AS people
OPTIONAL MATCH (call:SourceRecord {record_type: 'call', lifecycle_status: 'pending_review'})
      -[:CHILD_OF]->(:SourceRecord {record_type: 'crm_history'})
      -[:CHILD_OF]->(logical_deal:SourceRecord {record_type: 'crm_deal'})
      -[:FROM_SOURCE]->(source)
WHERE logical_deal.source_record_id = deal.source_record_id
WITH people, collect(DISTINCT call) AS calls
CALL (calls) {
    UNWIND calls AS call
    OPTIONAL MATCH (call)-[old_link:LINKED_TO]->(:Person)
    DELETE old_link
    RETURN count(*) AS removed_link_count
}
FOREACH (call IN calls |
    SET call.lifecycle_status = 'active',
        call.link_status = 'linked',
        call.activated_at = datetime(),
        call.updated_at = datetime()
)
FOREACH (call IN calls |
    FOREACH (person IN people |
        CREATE (call)-[:LINKED_TO {linked_at: datetime()}]->(person)
    )
)
RETURN size(calls) AS activated_call_count
"""
