"""Coverage-ledger identity and conflict behavior."""

from inspect import getsource

from src.bitrix_backfill_models import CoverageEntry, CoverageReconciliation
from src.bitrix_backfill_runtime import record_terminal_unit
from src.graph.queries.bitrix_backfill import (
    GET_BITRIX_COVERAGE_RECONCILIATION,
    UPSERT_BITRIX_BACKFILL_COVERAGE,
)


def test_coverage_entry_requires_a_terminal_source_identity() -> None:
    entry = CoverageEntry(
        source_identity="bitrix-crm-deal-7",
        source_boundary="upper-deal-900",
        disposition="existing_same_hash",
        source_observation_hash="sha256:observation",
        deal_id="7",
        scope_state="in_scope",
        category_id="2",
        stage_id="C2:NEW",
        census_epoch=1,
    )

    assert entry.terminal is True
    assert entry.disposition == "existing_same_hash"


def test_coverage_query_fails_closed_on_conflicting_replay() -> None:
    assert "source_identity: $source_identity" in UPSERT_BITRIX_BACKFILL_COVERAGE
    assert "source_boundary: $source_boundary" in UPSERT_BITRIX_BACKFILL_COVERAGE
    assert "coverage.outcome_digest = $outcome_digest" in UPSERT_BITRIX_BACKFILL_COVERAGE
    assert (
        "REMOVE coverage.creation_token\nWITH generation, coverage, created\nWHERE"
        in UPSERT_BITRIX_BACKFILL_COVERAGE
    )
    assert "CASE WHEN created THEN [1] ELSE []" in UPSERT_BITRIX_BACKFILL_COVERAGE
    assert "generation.status IN ['backfilling', 'reconciling', 'activating', 'active']" in (
        UPSERT_BITRIX_BACKFILL_COVERAGE
    )
    assert "(generation)-[:HAS_LOGICAL_RUN]" in UPSERT_BITRIX_BACKFILL_COVERAGE
    assert "(generation)-[:HAS_STREAM]" in UPSERT_BITRIX_BACKFILL_COVERAGE


def test_coverage_completion_equation_reconciles_checkpoint_counters() -> None:
    reconciliation = CoverageReconciliation(
        stream_key="crm_deals",
        coverage_count=5,
        terminal_count=5,
        created_count=2,
        duplicate_count=1,
        projection_count=0,
        unchanged_count=1,
        excluded_count=1,
        quarantine_count=0,
        conflict_count=0,
        failed_count=0,
        checkpoint_committed_count=2,
        checkpoint_duplicate_count=2,
        checkpoint_excluded_count=1,
        checkpoint_retry_count=0,
    )

    assert reconciliation.complete is True
    assert "checkpoint.committed_count" in GET_BITRIX_COVERAGE_RECONCILIATION
    assert "coverage.disposition = 'failed'" in GET_BITRIX_COVERAGE_RECONCILIATION


def test_coverage_identity_separates_deal_census_from_known_owner_refresh() -> None:
    source = getsource(record_terminal_unit)

    assert "generation.boundary_digest" in source
    assert "context.checkpoint.phase" in source
