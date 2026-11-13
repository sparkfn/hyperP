"""Coverage-ledger identity and conflict behavior."""

from __future__ import annotations

from inspect import getsource
from typing import cast

from neo4j import ManagedTransaction
from src.bitrix_backfill_models import (
    CoverageEntry,
    CoverageReconciliation,
    GenerationRunContext,
)
from src.bitrix_backfill_runtime import (
    KNOWN_OWNER_REFRESH_PHASE,
    record_terminal_unit,
    refresh_attempt_key,
    source_lineage_text,
)
from src.bitrix_ingestion_models import ExecutionContext, FenceContext
from src.graph.queries.bitrix_backfill import (
    GET_BITRIX_COVERAGE_RECONCILIATION,
    UPSERT_BITRIX_BACKFILL_COVERAGE,
)
from src.graph.queries.ingest_skips import RECORD_INGEST_REFRESH_UNIT
from src.models import IngestResult, SourceRecordEnvelope
from src.resumable import CheckpointDescriptor


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


def test_source_lineage_prefers_hash_stable_canonical_fields() -> None:
    payload = {
        "category_id": "2",
        "stage_id": "C2:NEW",
        "CATEGORY_ID": "legacy-category",
    }

    assert source_lineage_text(payload, "category_id", "CATEGORY_ID") == "2"
    assert source_lineage_text(payload, "stage_id", "STAGE_ID") == "C2:NEW"
    assert source_lineage_text({"CATEGORY_ID": "7"}, "category_id", "CATEGORY_ID") == "7"


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


class _RecordingResult:
    def __init__(self, record: dict[str, object]) -> None:
        self._record = record

    def single(self) -> dict[str, object]:
        return self._record


class _RecordingTx:
    """Minimal transaction double that records every statement it is handed."""

    def __init__(self) -> None:
        self.runs: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **kwargs: object) -> _RecordingResult:
        self.runs.append((query, kwargs))
        return _RecordingResult({"stop_requested": False})

    def accounting(self) -> list[dict[str, object]]:
        return [params for query, params in self.runs if query == RECORD_INGEST_REFRESH_UNIT]


def _fence_context() -> FenceContext:
    return FenceContext(
        logical_run_id="logical-1",
        ingest_run_id="ingest-1",
        source_key="bitrix_chat",
        stream_key="crm_deals",
        stream_generation=1,
        fencing_token=1,
        attempt_generation=1,
    )


def _execution_context(phase: str) -> ExecutionContext:
    return ExecutionContext(
        worker_task_id="task-1",
        fence_context=_fence_context(),
        checkpoint=CheckpointDescriptor(
            phase=phase,
            cursor={"last_deal_id": None, "last_known_deal_id": None, "census_epoch": 1},
            source_window={},
            last_committed_record_id=None,
            connector_version="bitrix-crm-deals-keyset-v1",
            schema_version=1,
            replay_boundary="exclusive_sorted_deal_id",
        ),
        generation_context=GenerationRunContext(
            generation_id="generation-1",
            boundary_digest="sha256:boundary",
            configuration_digest="sha256:config",
        ),
    )


def _deal_envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope.model_validate(
        {
            "source_system": "bitrix_chat",
            "source_record_id": "bitrix-crm-deal-7",
            "record_type": "crm_deal",
            "ingest_type": "api_incremental",
            "observed_at": "2026-08-13T00:00:00+00:00",
            "record_hash": "sha256:7",
            "raw_payload": {"ID": "7", "CATEGORY_ID": "2", "STAGE_ID": "C2:NEW"},
        }
    )


def test_known_owner_refresh_accounts_each_terminal_unit_in_its_own_transaction() -> None:
    tx = _RecordingTx()

    record_terminal_unit(
        cast(ManagedTransaction, tx),
        context=_execution_context(KNOWN_OWNER_REFRESH_PHASE),
        envelope=_deal_envelope(),
        result=IngestResult(source_record_id="bitrix-crm-deal-7"),
    )

    [accounting] = tx.accounting()
    assert accounting["ingest_run_id"] == "ingest-1"
    assert accounting["attempt_key"] == "known_owner_refresh_v1"
    assert accounting["generation_id"] == "generation-1"
    assert accounting["stream_key"] == "crm_deals"
    assert accounting["source_identity"] == "bitrix-crm-deal-7"
    assert accounting["source_boundary"] == "sha256:boundary:known_owner_refresh_v1"
    assert accounting["persisted_count"] == 1
    assert accounting["duplicate_count"] == 0
    assert accounting["pipeline_skip_count"] == 0
    assert accounting["record_error_count"] == 0


def test_known_owner_refresh_accounts_excluded_and_duplicate_buckets() -> None:
    excluded_tx = _RecordingTx()
    record_terminal_unit(
        cast(ManagedTransaction, excluded_tx),
        context=_execution_context(KNOWN_OWNER_REFRESH_PHASE),
        envelope=_deal_envelope(),
        result=IngestResult(
            source_record_id="bitrix-crm-deal-7",
            excluded=True,
            skip_reason="excluded_out_of_scope",
        ),
    )
    [excluded] = excluded_tx.accounting()
    assert excluded["persisted_count"] == 0
    assert excluded["pipeline_skip_count"] == 1

    duplicate_tx = _RecordingTx()
    record_terminal_unit(
        cast(ManagedTransaction, duplicate_tx),
        context=_execution_context(KNOWN_OWNER_REFRESH_PHASE),
        envelope=_deal_envelope(),
        result=IngestResult(source_record_id="bitrix-crm-deal-7", skipped_duplicate=True),
    )
    [duplicate] = duplicate_tx.accounting()
    assert duplicate["persisted_count"] == 0
    assert duplicate["duplicate_count"] == 1


def test_deal_census_does_not_write_refresh_accounting() -> None:
    tx = _RecordingTx()

    record_terminal_unit(
        cast(ManagedTransaction, tx),
        context=_execution_context("scoped_deal_census_v1"),
        envelope=_deal_envelope(),
        result=IngestResult(source_record_id="bitrix-crm-deal-7"),
    )

    assert tx.accounting() == []


def test_refresh_attempt_key_is_one_stable_attempt_per_run() -> None:
    context = _execution_context(KNOWN_OWNER_REFRESH_PHASE)

    assert refresh_attempt_key(context) == "known_owner_refresh_v1"
    legacy = ExecutionContext(
        worker_task_id="task-1",
        fence_context=_fence_context(),
        checkpoint=context.checkpoint,
    )
    assert refresh_attempt_key(legacy) == ""
