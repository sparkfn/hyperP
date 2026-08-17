CREATE_CRM_STAGE_BACKFILL_CONSTRAINTS: tuple[str, ...] = (
    """CREATE CONSTRAINT crm_stage_timeline_projection_unique IF NOT EXISTS
FOR (projection:CrmStageTimelineProjection)
REQUIRE (projection.mapping_version, projection.event_identity) IS UNIQUE""",
    """CREATE INDEX crm_stage_timeline_parent IF NOT EXISTS
FOR (projection:CrmStageTimelineProjection)
ON (projection.parent_source_system, projection.parent_source_record_id, projection.active)""",
    """CREATE CONSTRAINT crm_stage_analytical_release_unique IF NOT EXISTS
FOR (release:CrmStageAnalyticalRelease)
REQUIRE release.release_key IS UNIQUE""",
)

"""Cypher for #148 CRM stage mapping, projection rebuild, and release control."""

CRM_STAGE_MAPPING_INVENTORY = """
MATCH (record:SourceRecord {
  record_type: 'crm_history',
  history_family: 'stage'
})
MATCH (variant:CrmHistoryHashVariant)-[:EVIDENCED_BY]->(record)
OPTIONAL MATCH (head:CrmHistoryAuthorityHead {event_identity: variant.event_identity})
WITH record.event_category_id AS category_id,
     record.event_stage_id AS stage_id,
     record.event_stage_semantic_id AS source_semantic,
     $entity_type_id AS entity_type_id,
     count(record) AS observation_count,
     count(DISTINCT variant.event_identity) AS event_identity_count,
     min(record.event_at) AS first_event_at,
     max(record.event_at) AS last_event_at,
     count(CASE WHEN head.authority_state IN ['effective', 'corrected']
                AND head.selected_variant_hash = variant.canonical_hash THEN 1 END)
       AS effective_count,
     count(CASE WHEN head.authority_state IN ['withheld_parent', 'withheld_conflict']
                THEN 1 END) AS withheld_count
RETURN entity_type_id, category_id, stage_id, source_semantic,
       observation_count, event_identity_count, first_event_at, last_event_at,
       effective_count, withheld_count
ORDER BY entity_type_id, category_id, stage_id, source_semantic
"""

CRM_STAGE_CURRENT_EFFECTIVE_ROWS = """
MATCH (head:CrmHistoryAuthorityHead)
WHERE head.authority_state IN ['effective', 'corrected']
MATCH (decision:CrmHistoryAuthorityDecision {decision_id: head.decision_id})
MATCH (decision)-[:SELECTS_VARIANT]->(variant:CrmHistoryHashVariant)
MATCH (variant)-[:EVIDENCED_BY]->(record:SourceRecord {
  record_type: 'crm_history', history_family: 'stage'
})
MATCH (decision)-[:USES_PARENT_ASSOCIATION]->(
  association:CrmHistoryParentAssociationDecision
)
WHERE head.selected_variant_hash = variant.canonical_hash
  AND head.association_decision_id = association.decision_id
  AND association.association_state IN ['selected_active', 'selected_pending_review']
RETURN head.event_identity AS event_identity,
       head.decision_id AS authority_decision_id,
       head.head_version AS authority_head_version,
       head.authority_token AS authority_token,
       decision.available_at AS available_at,
       decision.logical_parent_source_system AS parent_source_system,
       decision.logical_parent_source_record_id AS parent_source_record_id,
       $entity_type_id AS entity_type_id,
       record.event_category_id AS category_id,
       record.event_stage_id AS stage_id,
       record.event_stage_semantic_id AS source_semantic,
       record.event_at AS event_at
ORDER BY parent_source_system, parent_source_record_id, event_at,
         authority_head_version, event_identity
"""

UPSERT_CRM_STAGE_TIMELINE_PROJECTIONS = """
UNWIND $rows AS row
MERGE (projection:CrmStageTimelineProjection {
  mapping_version: $mapping_version,
  event_identity: row.event_identity
})
SET projection.policy_version = $policy_version,
    projection.mapping_digest = $mapping_digest,
    projection.authority_decision_id = row.authority_decision_id,
    projection.authority_head_version = row.authority_head_version,
    projection.authority_token = row.authority_token,
    projection.available_at = datetime(row.available_at),
    projection.parent_source_system = row.parent_source_system,
    projection.parent_source_record_id = row.parent_source_record_id,
    projection.entity_type_id = row.entity_type_id,
    projection.category_id = row.category_id,
    projection.stage_id = row.stage_id,
    projection.source_semantic = row.source_semantic,
    projection.mapped_state = row.mapped_state,
    projection.mapping_reason = row.mapping_reason,
    projection.event_at = datetime(row.event_at),
    projection.active = true,
    projection.updated_at = datetime(),
    projection.created_at = coalesce(projection.created_at, datetime())
RETURN count(projection) AS projection_count
"""

RETIRE_STALE_CRM_STAGE_TIMELINE_PROJECTIONS = """
MATCH (projection:CrmStageTimelineProjection {mapping_version: $mapping_version})
WHERE projection.active = true
  AND NOT projection.event_identity IN $active_event_identities
SET projection.active = false,
    projection.retired_at = datetime(),
    projection.updated_at = datetime()
RETURN count(projection) AS retired_count
"""

PUBLISH_CRM_STAGE_INVALIDATIONS = """
MATCH (intent:CrmHistoryInvalidationIntent {target_kind: 'crm_stage_timeline'})
WHERE intent.status IN ['pending', 'failed']
SET intent.status = 'published',
    intent.published_mapping_version = $mapping_version,
    intent.published_policy_version = $policy_version,
    intent.published_at = datetime(),
    intent.updated_at = datetime()
RETURN count(intent) AS published_count
"""


GET_CRM_STAGE_INVALIDATION_STATUS = """
OPTIONAL MATCH (intent:CrmHistoryInvalidationIntent {target_kind: 'crm_stage_timeline'})
WITH count(intent) AS total,
     count(CASE WHEN intent.status = 'pending' THEN 1 END) AS pending,
     count(CASE WHEN intent.status = 'claimed' THEN 1 END) AS claimed,
     count(CASE WHEN intent.status = 'published' THEN 1 END) AS published,
     count(CASE WHEN intent.status = 'failed' THEN 1 END) AS failed,
     count(CASE WHEN intent.status = 'superseded' THEN 1 END) AS superseded
OPTIONAL MATCH (projection:CrmStageTimelineProjection {active: true})
RETURN total, pending, claimed, published, failed, superseded,
       count(projection) AS active_projection_count,
       count(DISTINCT projection.parent_source_record_id) AS projected_parent_count,
       collect(DISTINCT projection.mapping_version) AS active_mapping_versions,
       collect(DISTINCT projection.policy_version) AS active_policy_versions
"""

REHEARSE_CRM_STAGE_PROJECTION_ROLLBACK = """
MATCH (projection:CrmStageTimelineProjection {mapping_version: $mapping_version, active: true})
WITH collect(projection) AS projections, count(projection) AS candidate_count
FOREACH (projection IN projections |
  SET projection.rollback_probe = $probe_id,
      projection.rollback_probe_at = datetime()
)
FOREACH (projection IN projections |
  REMOVE projection.rollback_probe, projection.rollback_probe_at
)
RETURN candidate_count,
       size([projection IN projections
             WHERE projection.rollback_probe IS NOT NULL]) AS leaked_probe_count
"""


GET_CRM_STAGE_RECONCILIATION = """
OPTIONAL MATCH (occurrence:StageHistoryOccurrence)
WITH count(occurrence) AS occurrence_count,
     count(DISTINCT occurrence.occurrence_id) AS distinct_occurrence_count,
     count(CASE WHEN occurrence.terminal_disposition IS NULL THEN 1 END)
       AS nonterminal_occurrence_count
OPTIONAL MATCH (variant:CrmHistoryHashVariant)
WITH occurrence_count, distinct_occurrence_count, nonterminal_occurrence_count,
     count(variant) AS variant_count,
     count(DISTINCT variant.event_identity) AS variant_identity_count
OPTIONAL MATCH (head:CrmHistoryAuthorityHead)
OPTIONAL MATCH (decision:CrmHistoryAuthorityDecision {decision_id: head.decision_id})
WITH occurrence_count, distinct_occurrence_count, nonterminal_occurrence_count,
     variant_count, variant_identity_count,
     count(head) AS authority_head_count,
     count(CASE WHEN decision IS NULL THEN 1 END) AS missing_head_decision_count,
     count(CASE WHEN head.authority_state IN ['effective', 'corrected']
                AND (head.selected_variant_hash IS NULL
                     OR head.association_decision_id IS NULL) THEN 1 END)
       AS invalid_selected_authority_count
OPTIONAL MATCH (retry:StageHistoryRetry)
WITH occurrence_count, distinct_occurrence_count, nonterminal_occurrence_count,
     variant_count, variant_identity_count, authority_head_count,
     missing_head_decision_count, invalid_selected_authority_count,
     count(CASE WHEN retry.status IN ['pending', 'claimed'] THEN 1 END)
       AS unresolved_retry_count,
     count(CASE WHEN retry.status = 'quarantined' THEN 1 END) AS quarantined_retry_count
OPTIONAL MATCH (intent:CrmHistoryInvalidationIntent {target_kind: 'crm_stage_timeline'})
RETURN occurrence_count, distinct_occurrence_count, nonterminal_occurrence_count,
       variant_count, variant_identity_count, authority_head_count,
       missing_head_decision_count, invalid_selected_authority_count,
       unresolved_retry_count, quarantined_retry_count,
       count(intent) AS invalidation_count,
       count(CASE WHEN intent.status IN ['pending', 'claimed', 'failed'] THEN 1 END)
         AS unpublished_invalidation_count
"""

GET_CRM_STAGE_ANALYTICAL_RELEASE = """
OPTIONAL MATCH (release:CrmStageAnalyticalRelease {release_key: 'crm_stage_timeline'})
RETURN coalesce(release.enabled, false) AS enabled,
       release.mapping_version AS mapping_version,
       release.policy_version AS policy_version,
       release.mapping_digest AS mapping_digest,
       release.boundary_digest AS boundary_digest,
       release.reconciliation_digest AS reconciliation_digest,
       release.accepted_by AS accepted_by,
       release.accepted_at AS accepted_at
"""

ENABLE_CRM_STAGE_ANALYTICAL_RELEASE = """
MERGE (release:CrmStageAnalyticalRelease {release_key: 'crm_stage_timeline'})
SET release.enabled = true,
    release.mapping_version = $mapping_version,
    release.policy_version = $policy_version,
    release.mapping_digest = $mapping_digest,
    release.boundary_digest = $boundary_digest,
    release.reconciliation_digest = $reconciliation_digest,
    release.accepted_by = $accepted_by,
    release.accepted_at = datetime(),
    release.updated_at = datetime(),
    release.created_at = coalesce(release.created_at, datetime())
RETURN release.enabled AS enabled,
       release.mapping_version AS mapping_version,
       release.policy_version AS policy_version,
       release.mapping_digest AS mapping_digest,
       release.boundary_digest AS boundary_digest,
       release.reconciliation_digest AS reconciliation_digest,
       release.accepted_by AS accepted_by,
       release.accepted_at AS accepted_at
"""
