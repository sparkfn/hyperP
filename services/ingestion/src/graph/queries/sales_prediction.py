"""Read-only queries for the accepted CRM stage release consumed by #125.

Shapes mirror the Gate 1 evidence contract from issue #149 exactly (release
row, keyset-paginated stage events, per-parent deal versions) so the dataset
builder reads the same accepted facts the gate decision was made against. The
stage-event page additionally selects the projection's raw category/stage
identity columns, which the v1 feature set needs but the gate report did not.
"""

from __future__ import annotations

SALES_PREDICTION_RELEASE = """
MATCH (release:CrmStageAnalyticalRelease {release_key: 'crm_stage_timeline'})
CALL (release) {
  OPTIONAL MATCH (projection:CrmStageTimelineProjection {active: true})
  RETURN count(projection) AS projection_count,
         count(DISTINCT projection.event_identity) AS distinct_projection_count,
         count(CASE WHEN projection.event_at IS NULL OR projection.available_at IS NULL THEN 1 END)
           AS invalid_projection_timestamp_count,
         count(CASE WHEN projection.authority_head_version > 1 THEN 1 END)
           AS restated_event_count,
         max(projection.event_at) AS max_event_at,
         max(projection.available_at) AS max_available_at,
         count(CASE WHEN projection.mapping_version <> release.mapping_version THEN 1 END)
           AS wrong_mapping_count,
         count(CASE WHEN projection.policy_version <> release.policy_version THEN 1 END)
           AS wrong_policy_count
}
RETURN coalesce(release.enabled, false) AS enabled,
       release.mapping_version AS mapping_version,
       release.policy_version AS policy_version,
       toString(release.accepted_at) AS accepted_at,
       release.boundary_digest IS NOT NULL AS boundary_bound,
       release.reconciliation_digest IS NOT NULL AS reconciliation_bound,
       release.mapping_digest IS NOT NULL AS mapping_bound,
       projection_count,
       distinct_projection_count,
       invalid_projection_timestamp_count,
       restated_event_count,
       wrong_mapping_count,
       wrong_policy_count,
       toString(max_event_at) AS max_event_at,
       toString(max_available_at) AS max_available_at
"""

SALES_PREDICTION_STAGE_EVENTS_PAGE = """
MATCH (release:CrmStageAnalyticalRelease {release_key: 'crm_stage_timeline', enabled: true})
MATCH (projection:CrmStageTimelineProjection {
  active: true,
  mapping_version: release.mapping_version,
  policy_version: release.policy_version
})
WHERE $after_event_identity IS NULL OR projection.event_identity > $after_event_identity
RETURN projection.event_identity AS event_identity,
       projection.parent_source_system AS parent_source_system,
       projection.parent_source_record_id AS parent_source_record_id,
       projection.mapped_state AS mapped_state,
       projection.category_id AS category_id,
       projection.stage_id AS stage_id,
       projection.source_semantic AS source_semantic,
       toString(projection.event_at) AS event_at,
       toString(projection.available_at) AS available_at,
       projection.authority_head_version AS authority_head_version
ORDER BY event_identity
LIMIT $limit
"""

SALES_PREDICTION_DEAL_VERSIONS_FOR_PARENTS = """
UNWIND $parents AS parent
MATCH (deal:SourceRecord {
  record_type: 'crm_deal',
  source_record_id: parent.source_record_id
})-[:FROM_SOURCE]->(:SourceSystem {source_key: parent.source_system})
OPTIONAL MATCH (deal)-[link:LINKED_TO]->(person:Person)
WHERE coalesce(link.is_active, true) = true
RETURN parent.source_system AS parent_source_system,
       parent.source_record_id AS parent_source_record_id,
       elementId(deal) AS version_key,
       toInteger(coalesce(deal.source_record_version, '1')) AS source_record_version,
       deal.entity_key AS entity_key,
       toString(deal.observed_at) AS observed_at,
       toString(deal.ingested_at) AS ingested_at,
       toString(deal.activated_at) AS activated_at,
       toString(deal.superseded_at) AS superseded_at,
       toString(deal.rejected_at) AS rejected_at,
       toString(deal.link_failed_at) AS link_failed_at,
       deal.raw_payload AS raw_payload,
       deal.lifecycle_status AS lifecycle_status,
       collect(DISTINCT elementId(person)) AS linked_person_ids,
       collect(DISTINCT CASE WHEN coalesce(person.status, 'active') = 'active'
         THEN elementId(person) END) AS active_person_ids,
       count(DISTINCT person) AS linked_person_count,
       count(DISTINCT CASE WHEN coalesce(person.status, 'active') = 'active' THEN person END)
         AS active_person_count,
       toString(max(link.linked_at)) AS latest_linked_at
ORDER BY parent_source_system, parent_source_record_id, source_record_version, version_key
"""
