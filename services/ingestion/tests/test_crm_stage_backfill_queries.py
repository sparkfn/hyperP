from src.graph.queries.crm_stage_backfill import (
    CREATE_CRM_STAGE_BACKFILL_CONSTRAINTS,
    CRM_STAGE_CURRENT_EFFECTIVE_ROWS,
    CRM_STAGE_MAPPING_INVENTORY,
    ENABLE_CRM_STAGE_ANALYTICAL_RELEASE,
    GET_CRM_STAGE_RECONCILIATION,
    PUBLISH_CRM_STAGE_INVALIDATIONS,
    REHEARSE_CRM_STAGE_PROJECTION_ROLLBACK,
    UPSERT_CRM_STAGE_TIMELINE_PROJECTIONS,
)


def test_stage_backfill_queries_preserve_authority_and_append_projection() -> None:
    assert "CrmHistoryAuthorityHead" in CRM_STAGE_CURRENT_EFFECTIVE_ROWS
    assert "['effective', 'corrected']" in CRM_STAGE_CURRENT_EFFECTIVE_ROWS
    assert "USES_PARENT_ASSOCIATION" in CRM_STAGE_CURRENT_EFFECTIVE_ROWS
    assert "history_family: 'stage'" in CRM_STAGE_MAPPING_INVENTORY
    assert "MERGE (projection:CrmStageTimelineProjection" in (
        UPSERT_CRM_STAGE_TIMELINE_PROJECTIONS
    )
    assert "DELETE" not in UPSERT_CRM_STAGE_TIMELINE_PROJECTIONS.upper()


def test_stage_backfill_release_is_explicit_and_reconciliation_is_fail_closed() -> None:
    assert "release.enabled = true" in ENABLE_CRM_STAGE_ANALYTICAL_RELEASE
    assert "unresolved_retry_count" in GET_CRM_STAGE_RECONCILIATION
    assert "unpublished_invalidation_count" in GET_CRM_STAGE_RECONCILIATION
    assert "intent.status IN ['pending', 'failed']" in PUBLISH_CRM_STAGE_INVALIDATIONS
    assert "REMOVE projection.rollback_probe" in REHEARSE_CRM_STAGE_PROJECTION_ROLLBACK
    assert len(CREATE_CRM_STAGE_BACKFILL_CONSTRAINTS) == 3
