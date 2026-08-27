"""Read-only operator status and reconciliation for CRM stage-history runs."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import cast

from neo4j import ManagedTransaction, Record

from src.connectors.bitrix_stage_history.artifact_connector import (
    VerifiedStageIngestionArtifact,
)
from src.graph.client import Neo4jClient
from src.graph.queries.stage_history_ingestion import (
    GET_STAGE_HISTORY_RECONCILIATION,
    GET_STAGE_HISTORY_STATUS,
)
from src.source_instances import LEGACY_DEFAULT_CONTROL_INSTANCE_ID, effective_control_instance_id
from src.stage_history_ingestion_models import (
    StageHistoryAccounting,
    StageHistoryAssociationAccounting,
    StageHistoryAuthorityAccounting,
    StageHistoryCheckpointSnapshot,
    StageHistoryIdentityAccounting,
    StageHistoryRetryAccounting,
    StageHistoryTerminalAccounting,
)
from src.stage_history_pipeline import initial_failure_checkpoint, initial_replay_checkpoint


@dataclass(frozen=True, slots=True)
class StageHistoryRunStatus:
    logical_run_id: str
    run_type: str
    logical_status: str
    ingest_run_id: str | None
    attempt_status: str | None
    stream_status: str | None
    stream_generation: int | None
    phase: str | None
    checkpoint_revision: int
    checkpoint_last_page_sequence: int | None
    committed_unit_count: int
    fetched_count: int
    artifact_id: str | None
    artifact_manifest_hmac: str | None
    accounting: StageHistoryAccounting | None
    variant_count: int
    source_record_count: int
    invalidation_intent_count: int
    review_command_count: int
    graph_error_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StageHistoryReconciliationReport:
    logical_run_id: str
    artifact_id: str
    artifact_manifest_hmac: str
    complete: bool
    smoke_ready: bool
    error_codes: tuple[str, ...]
    committed_unit_count: int
    fetched_count: int
    variant_count: int
    source_record_count: int
    invalidation_intent_count: int
    review_command_count: int
    accounting: StageHistoryAccounting
    nonterminal_unit_count: int
    invalid_authority_head_count: int
    invalid_invalidation_transition_count: int
    expired_review_claim_count: int


class StageHistoryStatusRepository:
    """Expose aggregate-only status and hostile reconciliation evidence."""

    def __init__(
        self,
        client: Neo4jClient,
        control_instance_id: str = LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
    ) -> None:
        self._client = client
        self._control_instance_id = effective_control_instance_id(control_instance_id)

    def status(self, logical_run_id: str) -> StageHistoryRunStatus | None:
        _require_text(logical_run_id, "logical_run_id")

        def _read(tx: ManagedTransaction) -> Record | None:
            return tx.run(
                GET_STAGE_HISTORY_STATUS,
                logical_run_id=logical_run_id,
                control_instance_id=self._control_instance_id,
            ).single()

        row = self._client.execute_read(_read)
        if row is None:
            return None
        run_type = _string(row, "run_type")
        reconciliation = None
        if run_type in {
            "bounded_smoke_replay",
            "authoritative_backfill_replay",
            "authoritative_catch_up_replay",
            "capture_failure_accounting",
        }:

            def _reconcile_read(tx: ManagedTransaction) -> Record | None:
                return tx.run(
                    GET_STAGE_HISTORY_RECONCILIATION,
                    logical_run_id=logical_run_id,
                    control_instance_id=self._control_instance_id,
                ).single()

            reconciliation = self._client.execute_read(_reconcile_read)
        graph_errors = _graph_errors(reconciliation) if reconciliation is not None else []
        try:
            artifact_id, manifest_hmac = _artifact_reference(reconciliation)
        except (TypeError, ValueError, json.JSONDecodeError):
            artifact_id, manifest_hmac = None, None
            graph_errors.append("checkpoint_source_window_invalid")
        return StageHistoryRunStatus(
            logical_run_id=_string(row, "logical_run_id"),
            run_type=run_type,
            logical_status=_string(row, "logical_status"),
            ingest_run_id=_optional_string(row.get("ingest_run_id")),
            attempt_status=_optional_string(row.get("attempt_status")),
            stream_status=_optional_string(row.get("stream_status")),
            stream_generation=_optional_non_negative(row.get("stream_generation")),
            phase=_optional_string(row.get("phase")),
            checkpoint_revision=_non_negative(row, "checkpoint_revision"),
            checkpoint_last_page_sequence=_optional_non_negative(
                row.get("checkpoint_last_page_sequence")
            ),
            committed_unit_count=_non_negative(row, "committed_unit_count"),
            fetched_count=_non_negative(row, "fetched_count"),
            artifact_id=artifact_id,
            artifact_manifest_hmac=manifest_hmac,
            accounting=_accounting(reconciliation) if reconciliation is not None else None,
            variant_count=(
                _non_negative(reconciliation, "variant_count") if reconciliation is not None else 0
            ),
            source_record_count=(
                _non_negative(reconciliation, "source_record_count")
                if reconciliation is not None
                else 0
            ),
            invalidation_intent_count=(
                _non_negative(reconciliation, "invalidation_intent_count")
                if reconciliation is not None
                else 0
            ),
            review_command_count=(
                _non_negative(reconciliation, "review_command_count")
                if reconciliation is not None
                else 0
            ),
            graph_error_codes=tuple(sorted(set(graph_errors))),
        )

    def reconcile(
        self,
        logical_run_id: str,
        *,
        artifact: VerifiedStageIngestionArtifact,
        now: datetime | None = None,
    ) -> StageHistoryReconciliationReport:
        _require_text(logical_run_id, "logical_run_id")

        def _read(tx: ManagedTransaction) -> Record | None:
            return tx.run(
                GET_STAGE_HISTORY_RECONCILIATION,
                logical_run_id=logical_run_id,
                control_instance_id=self._control_instance_id,
            ).single()

        row = self._client.execute_read(_read)
        if row is None:
            raise ValueError("stage-history logical run was not found")
        errors = _reconciliation_errors(row, artifact, now=now or datetime.now(UTC))
        status = self.status(logical_run_id)
        expected_checkpoint = _expected_checkpoint(artifact)
        expected_status = (
            "completed_with_errors"
            if artifact.manifest.artifact_kind == "stage-ingestion-failed"
            else "completed"
        )
        if status is None:
            errors.append("logical_status_unavailable")
            logical_status = "unknown"
        else:
            logical_status = status.logical_status
            if status.logical_status != expected_status:
                errors.append("logical_status_mismatch")
            if status.attempt_status != expected_status:
                errors.append("attempt_status_mismatch")
            if status.stream_status != "completed":
                errors.append("stream_status_mismatch")
            if status.phase != expected_checkpoint.phase:
                errors.append("checkpoint_phase_mismatch")
        errors = sorted(set(errors))
        complete = not errors
        return StageHistoryReconciliationReport(
            logical_run_id=_string(row, "logical_run_id"),
            artifact_id=artifact.manifest.artifact_id,
            artifact_manifest_hmac=artifact.manifest.manifest_hmac,
            complete=complete,
            smoke_ready=complete and logical_status in {"completed", "completed_with_errors"},
            error_codes=tuple(errors),
            committed_unit_count=len(_int_list(row, "committed_page_sequences")),
            fetched_count=_non_negative(row, "total_fetched_count"),
            variant_count=_non_negative(row, "variant_count"),
            source_record_count=_non_negative(row, "source_record_count"),
            invalidation_intent_count=_non_negative(row, "invalidation_intent_count"),
            review_command_count=_non_negative(row, "review_command_count"),
            accounting=_accounting(row),
            nonterminal_unit_count=_non_negative(row, "nonterminal_unit_count"),
            invalid_authority_head_count=_non_negative(row, "invalid_authority_head_count"),
            invalid_invalidation_transition_count=_non_negative(
                row, "invalid_invalidation_transition_count"
            ),
            expired_review_claim_count=_non_negative(row, "expired_review_claim_count"),
        )

    def checkpoint(
        self,
        logical_run_id: str,
        *,
        artifact: VerifiedStageIngestionArtifact,
    ) -> StageHistoryCheckpointSnapshot:
        """Rebuild the exact immutable checkpoint snapshot for worker resume."""

        def _read(tx: ManagedTransaction) -> Record | None:
            return tx.run(
                GET_STAGE_HISTORY_RECONCILIATION,
                logical_run_id=logical_run_id,
                control_instance_id=self._control_instance_id,
            ).single()

        row = self._client.execute_read(_read)
        if row is None:
            raise ValueError("stage-history logical run was not found")
        initial = _expected_checkpoint(artifact)
        if _string(row, "run_type") != initial.run_type:
            raise RuntimeError("stage-history checkpoint run type changed")
        source_window_json = _string(row, "checkpoint_source_window_json")
        decoded = json.loads(source_window_json)
        if decoded != asdict(initial.source_window):
            raise RuntimeError("stage-history checkpoint source window changed")
        revision = _non_negative(row, "checkpoint_revision")
        if revision == 0:
            return initial
        unit_ids = _str_list(row, "committed_unit_ids")
        digests = _str_list(row, "committed_unit_digests")
        pages = _int_list(row, "committed_page_sequences")
        if len(unit_ids) != revision or len(digests) != revision or len(pages) != revision:
            raise RuntimeError("stage-history checkpoint ledger length changed")
        return StageHistoryCheckpointSnapshot(
            run_type=initial.run_type,
            source_window=initial.source_window,
            last_page_sequence=pages[-1],
            revision=revision,
            committed_unit_count=revision,
            last_unit_id=unit_ids[-1],
            last_unit_digest=digests[-1],
            accounting=_accounting(row),
        )


def _reconciliation_errors(
    row: Record,
    artifact: VerifiedStageIngestionArtifact,
    *,
    now: datetime,
) -> list[str]:
    errors = _graph_errors(row)
    expected_checkpoint = _expected_checkpoint(artifact)
    if _string(row, "run_type") != expected_checkpoint.run_type:
        errors.append("artifact_run_type_mismatch")
    try:
        source_window = _decode_source_window(row)
    except (TypeError, ValueError, json.JSONDecodeError):
        errors.append("checkpoint_source_window_invalid")
    else:
        if source_window != asdict(expected_checkpoint.source_window):
            errors.append("artifact_source_window_mismatch")
    expected_pages = [page.page_sequence for page in artifact.pages]
    expected_digests = [page.page_digest for page in artifact.pages]
    if _int_list(row, "committed_page_sequences") != expected_pages:
        errors.append("artifact_page_mismatch")
    if _str_list(row, "committed_unit_digests") != expected_digests:
        errors.append("artifact_digest_mismatch")
    artifact_rows = sum(len(page.rows) for page in artifact.pages)
    if _non_negative(row, "total_fetched_count") != artifact_rows:
        errors.append("artifact_row_count_mismatch")
    if artifact.manifest.is_expired(now=now):
        errors.append("artifact_expired")
    return sorted(set(errors))


def _graph_errors(row: Record) -> list[str]:
    checks = {
        "unit_accounting_imbalance": "units_balanced",
        "variant_evidence_imbalance": "variant_source_records_balanced",
        "parent_association_imbalance": "parent_associations_balanced",
        "checkpoint_page_gap": "committed_pages_contiguous",
        "checkpoint_revision_mismatch": "checkpoint_revision_balanced",
        "checkpoint_cursor_mismatch": "checkpoint_cursor_page_balanced",
        "checkpoint_cursor_json_mismatch": "checkpoint_cursor_json_balanced",
        "checkpoint_boundary_mismatch": "replay_boundary_valid",
        "checkpoint_tail_mismatch": "checkpoint_last_unit_balanced",
        "committed_counter_imbalance": "committed_counter_balanced",
        "duplicate_counter_imbalance": "duplicate_counter_balanced",
        "excluded_counter_imbalance": "excluded_counter_balanced",
        "retry_counter_imbalance": "retry_counter_balanced",
        "current_association_partition_imbalance": ("current_association_partition_balanced"),
        "current_authority_partition_imbalance": "current_authority_partition_balanced",
        "current_retry_partition_imbalance": "current_retry_partition_balanced",
    }
    errors = [code for code, key in checks.items() if row.get(key) is not True]
    integer_failures = {
        "nonterminal_unit": "nonterminal_unit_count",
        "invalid_variant_evidence": "invalid_variant_evidence_count",
        "shared_variant_evidence": "shared_variant_evidence_count",
        "invalid_occurrence_variant_link": "invalid_occurrence_variant_link_count",
        "unexpected_occurrence_variant_link": ("invalid_empty_occurrence_variant_link_count"),
        "invalid_parent_association": "invalid_parent_association_count",
        "invalid_authority_head": "invalid_authority_head_count",
        "invalid_effective_authority_head": "invalid_effective_head_count",
        "incomplete_invalidation_targets": "invalid_invalidation_transition_count",
        "expired_review_claim": "expired_review_claim_count",
        "stale_association_projection": "invalid_current_association_projection_count",
        "unexpected_association_projection": "invalid_empty_association_projection_count",
        "stale_authority_projection": "invalid_current_authority_projection_count",
        "unexpected_authority_projection": "invalid_empty_authority_projection_count",
        "stale_retry_projection": "invalid_current_retry_projection_count",
        "unexpected_retry_projection": "invalid_empty_retry_projection_count",
    }
    errors.extend(code for code, key in integer_failures.items() if _non_negative(row, key) != 0)
    return sorted(set(errors))


def _artifact_reference(row: Record | None) -> tuple[str | None, str | None]:
    if row is None:
        return None, None
    source_window = _decode_source_window(row)
    artifact_id = source_window.get("stage_ingestion_artifact_id")
    if artifact_id is None:
        artifact_id = source_window.get("failed_artifact_id")
    manifest_hmac = source_window.get("artifact_manifest_hmac")
    if manifest_hmac is None:
        manifest_hmac = source_window.get("manifest_hmac")
    return _optional_string(artifact_id), _optional_string(manifest_hmac)


def _decode_source_window(row: Record) -> dict[str, object]:
    decoded = json.loads(_string(row, "checkpoint_source_window_json"))
    if not isinstance(decoded, dict):
        raise ValueError("stage-history checkpoint source window is invalid")
    if not all(isinstance(key, str) for key in decoded):
        raise ValueError("stage-history checkpoint source window keys are invalid")
    return cast(dict[str, object], decoded)


def _expected_checkpoint(
    artifact: VerifiedStageIngestionArtifact,
) -> StageHistoryCheckpointSnapshot:
    return (
        initial_replay_checkpoint(artifact)
        if artifact.manifest.artifact_kind == "stage-ingestion"
        else initial_failure_checkpoint(artifact)
    )


def _accounting(row: Record) -> StageHistoryAccounting:
    return StageHistoryAccounting(
        terminal=StageHistoryTerminalAccounting(
            malformed_excluded=_non_negative(row, "total_malformed_excluded_count"),
            capture_rejected_valid=_non_negative(row, "total_capture_rejected_valid_count"),
            excluded_out_of_scope=_non_negative(row, "total_excluded_out_of_scope_count"),
            canonical_effective=_non_negative(row, "total_canonical_effective_count"),
            canonical_pending_parent=_non_negative(row, "total_canonical_pending_parent_count"),
            parent_waiting=_non_negative(row, "total_parent_waiting_count"),
            parent_ambiguous=_non_negative(row, "total_parent_ambiguous_count"),
            same_hash_replay=_non_negative(row, "total_same_hash_replay_count"),
            differing_hash_conflict=_non_negative(row, "total_differing_hash_conflict_count"),
        ),
        identity=StageHistoryIdentityAccounting(
            new_variant=_non_negative(row, "total_new_variant_count"),
            existing_same_hash=_non_negative(row, "total_existing_same_hash_count"),
            new_conflict_variant=_non_negative(row, "total_new_conflict_variant_count"),
        ),
        association=StageHistoryAssociationAccounting(
            selected_active=_non_negative(row, "total_selected_active_count"),
            selected_pending_review=_non_negative(row, "total_selected_pending_review_count"),
            waiting=_non_negative(row, "total_waiting_count"),
            ambiguous=_non_negative(row, "total_ambiguous_count"),
            rejected=_non_negative(row, "total_association_rejected_count"),
        ),
        authority=StageHistoryAuthorityAccounting(
            effective=_non_negative(row, "total_effective_count"),
            withheld_parent=_non_negative(row, "total_withheld_parent_count"),
            withheld_conflict=_non_negative(row, "total_withheld_conflict_count"),
            rejected=_non_negative(row, "total_authority_rejected_count"),
            corrected=_non_negative(row, "total_corrected_count"),
        ),
        retry=StageHistoryRetryAccounting(
            none=_non_negative(row, "total_retry_none_count"),
            pending=_non_negative(row, "total_retry_pending_count"),
            claimed=_non_negative(row, "total_retry_claimed_count"),
            resolved=_non_negative(row, "total_retry_resolved_count"),
            rejected=_non_negative(row, "total_retry_rejected_count"),
            quarantined=_non_negative(row, "total_retry_quarantined_count"),
        ),
    )


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _string(row: Record, key: str) -> str:
    value: object = row[key]
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"stage-history status returned invalid {key}")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RuntimeError("stage-history status returned an invalid optional string")
    return value


def _non_negative(row: Record, key: str) -> int:
    value: object = row.get(key, 0)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"stage-history status returned invalid {key}")
    return value


def _optional_non_negative(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError("stage-history status returned an invalid optional integer")
    return value


def _int_list(row: Record, key: str) -> list[int]:
    value: object = row.get(key, [])
    if not isinstance(value, list):
        raise RuntimeError(f"stage-history status returned invalid {key}")
    result: list[int] = []
    for item in cast(list[object], value):
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise RuntimeError(f"stage-history status returned invalid {key}")
        result.append(item)
    return result


def _str_list(row: Record, key: str) -> list[str]:
    value: object = row.get(key, [])
    if not isinstance(value, list):
        raise RuntimeError(f"stage-history status returned invalid {key}")
    result: list[str] = []
    for item in cast(list[object], value):
        if not isinstance(item, str) or not item:
            raise RuntimeError(f"stage-history status returned invalid {key}")
        result.append(item)
    return result
