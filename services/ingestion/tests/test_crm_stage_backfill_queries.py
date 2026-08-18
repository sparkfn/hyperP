from src.graph.queries.crm_stage_backfill import (
    CLEAR_CRM_STAGE_PROJECTION_ROLLBACK_PROBES,
    COUNT_CRM_STAGE_PROJECTION_ROLLBACK_PROBE_LEAKS,
    CREATE_CRM_STAGE_BACKFILL_CONSTRAINTS,
    CRM_STAGE_CURRENT_EFFECTIVE_ROWS,
    CRM_STAGE_MAPPING_INVENTORY,
    ENABLE_CRM_STAGE_ANALYTICAL_RELEASE,
    GET_ACTIVE_CRM_STAGE_PROJECTION_IDENTITIES_PAGE,
    GET_CRM_STAGE_RECONCILIATION,
    PUBLISH_CRM_STAGE_INVALIDATIONS,
    RETAIN_REVIEWED_PENDING_PARENT_RETRIES,
    RETIRE_STALE_CRM_STAGE_TIMELINE_PROJECTIONS,
    SET_CRM_STAGE_PROJECTION_ROLLBACK_PROBES,
    UPSERT_CRM_STAGE_TIMELINE_PROJECTIONS,
)


def test_stage_backfill_queries_preserve_authority_and_append_projection() -> None:
    assert "CrmHistoryAuthorityHead" in CRM_STAGE_CURRENT_EFFECTIVE_ROWS
    assert "['effective', 'corrected']" in CRM_STAGE_CURRENT_EFFECTIVE_ROWS
    assert "USES_PARENT_ASSOCIATION" in CRM_STAGE_CURRENT_EFFECTIVE_ROWS
    assert "history_family: 'stage'" in CRM_STAGE_MAPPING_INVENTORY
    assert "MERGE (projection:CrmStageTimelineProjection" in (UPSERT_CRM_STAGE_TIMELINE_PROJECTIONS)
    assert "DELETE" not in UPSERT_CRM_STAGE_TIMELINE_PROJECTIONS.upper()


def test_stage_backfill_release_is_explicit_and_reconciliation_is_fail_closed() -> None:
    assert "release.enabled = true" in ENABLE_CRM_STAGE_ANALYTICAL_RELEASE
    assert "unresolved_retry_count" in GET_CRM_STAGE_RECONCILIATION
    assert "unpublished_invalidation_count" in GET_CRM_STAGE_RECONCILIATION
    assert "intent.status IN ['pending', 'failed']" in PUBLISH_CRM_STAGE_INVALIDATIONS
    assert "LIMIT $limit" in CRM_STAGE_CURRENT_EFFECTIVE_ROWS
    assert "ORDER BY event_identity" in CRM_STAGE_CURRENT_EFFECTIVE_ROWS
    assert "projection.rebuild_id = $rebuild_id" in UPSERT_CRM_STAGE_TIMELINE_PROJECTIONS
    assert "coalesce(projection.rebuild_id, '') <> $rebuild_id" in (
        RETIRE_STALE_CRM_STAGE_TIMELINE_PROJECTIONS
    )
    assert "LIMIT $limit" in RETIRE_STALE_CRM_STAGE_TIMELINE_PROJECTIONS
    assert "last_event_identity" in RETIRE_STALE_CRM_STAGE_TIMELINE_PROJECTIONS
    assert "LIMIT $limit" in PUBLISH_CRM_STAGE_INVALIDATIONS
    assert "last_intent_id" in PUBLISH_CRM_STAGE_INVALIDATIONS
    assert "LIMIT $limit" in GET_ACTIVE_CRM_STAGE_PROJECTION_IDENTITIES_PAGE
    assert "collect(" not in GET_ACTIVE_CRM_STAGE_PROJECTION_IDENTITIES_PAGE.lower()
    assert "projection.rollback_probe = $probe_id" in SET_CRM_STAGE_PROJECTION_ROLLBACK_PROBES
    assert "REMOVE projection.rollback_probe" in CLEAR_CRM_STAGE_PROJECTION_ROLLBACK_PROBES
    assert "projection.rollback_probe IS NOT NULL" in (
        COUNT_CRM_STAGE_PROJECTION_ROLLBACK_PROBE_LEAKS
    )
    assert len(CREATE_CRM_STAGE_BACKFILL_CONSTRAINTS) == 3


def test_reviewed_parent_retry_retention_is_guarded_and_auditable() -> None:
    query = RETAIN_REVIEWED_PENDING_PARENT_RETRIES
    assert "unresolved_count = $expected_count" in query
    assert "size(candidates) = $expected_count" in query
    assert "reason_code = 'canonical_pending_parent'" in query
    assert "association_state = 'selected_pending_review'" in query
    assert "authority_state = 'withheld_parent'" in query
    assert "active_parent_count = 0" in query
    assert "pending_parent_count = 1" in query
    assert "retry.status = 'quarantined'" in query
    assert "retry.retention_reason = $reason" in query
    assert "retry.retained_by = $accepted_by" in query
    assert "DELETE" not in query.upper()
