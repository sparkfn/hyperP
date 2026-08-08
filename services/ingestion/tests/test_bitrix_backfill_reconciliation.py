"""Freeze and reconciliation queries fail closed on incomplete evidence."""

from src.graph.queries.bitrix_backfill import (
    COMPLETE_BITRIX_BACKFILL_FREEZE,
    FREEZE_BITRIX_BACKFILL_GENERATION,
    GET_BITRIX_COVERAGE_RECONCILIATION,
    GET_OWNER_COVERAGE_FOR_FREEZE,
    RECORD_BITRIX_BACKFILL_RECONCILIATION,
)


def test_freeze_archives_checkpoints_and_supersedes_every_corrective_fence() -> None:
    assert "generation.status = 'reconciling'" in FREEZE_BITRIX_BACKFILL_GENERATION
    assert "all(logical IN logicals WHERE logical.status IN" in (FREEZE_BITRIX_BACKFILL_GENERATION)
    assert "stream.status = 'superseded'" in FREEZE_BITRIX_BACKFILL_GENERATION
    assert "stream.fence_lock_version" in FREEZE_BITRIX_BACKFILL_GENERATION
    assert "checkpoint.status = 'archived'" in FREEZE_BITRIX_BACKFILL_GENERATION
    assert "generation.status = 'frozen'" in COMPLETE_BITRIX_BACKFILL_FREEZE


def test_reconciliation_and_owner_export_read_only_non_stage_coverage() -> None:
    assert "coverage.disposition = 'failed'" in GET_BITRIX_COVERAGE_RECONCILIATION
    assert "stream_key: 'crm_deals'" in GET_OWNER_COVERAGE_FOR_FREEZE
    assert "Stage" not in GET_OWNER_COVERAGE_FOR_FREEZE
    assert "generation.status IN ['backfilling', 'reconciling']" in (
        RECORD_BITRIX_BACKFILL_RECONCILIATION
    )
    assert "conflict_count = 0" in RECORD_BITRIX_BACKFILL_RECONCILIATION
    assert "generation.reconciliation_digest = $reconciliation_digest" in (
        RECORD_BITRIX_BACKFILL_RECONCILIATION
    )
