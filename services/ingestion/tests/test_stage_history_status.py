"""Aggregate-only status and reconciliation coverage for stage history."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar, cast

import pytest
from neo4j import ManagedTransaction, Record
from src.connectors.bitrix_stage_history.artifact_connector import (
    StageArtifactReplayPage,
    StageArtifactReplayRow,
    VerifiedStageIngestionArtifact,
)
from src.connectors.bitrix_stage_history.artifact_manifest import (
    ArtifactManifest,
    canonical_metadata_json,
)
from src.connectors.bitrix_stage_history.artifact_provenance import ArtifactProvenance
from src.graph.client import Neo4jClient
from src.graph.queries.stage_history_ingestion import (
    GET_STAGE_HISTORY_RECONCILIATION,
    GET_STAGE_HISTORY_STATUS,
)
from src.graph.stage_history_status import StageHistoryStatusRepository
from src.stage_history_ingestion_models import StageHistoryMalformedObservation
from src.stage_history_pipeline import initial_replay_checkpoint

T = TypeVar("T")
_NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


class _Result:
    def __init__(self, row: Record | None) -> None:
        self._row = row

    def single(self) -> Record | None:
        return self._row


class _Tx:
    def __init__(self, rows: dict[str, Record | None]) -> None:
        self._rows = rows

    def run(self, query: str, **_parameters: object) -> _Result:
        if query not in self._rows:
            raise AssertionError("unexpected status query")
        return _Result(self._rows[query])


class _Client:
    def __init__(self, rows: dict[str, Record | None]) -> None:
        self._tx = _Tx(rows)

    def execute_read(self, work: Callable[[ManagedTransaction], T]) -> T:
        return work(cast(ManagedTransaction, self._tx))


def _record(**values: object) -> Record:
    return cast(Record, values)


def _artifact(tmp_path: Path, *, expired: bool = False) -> VerifiedStageIngestionArtifact:
    expiry = "2026-08-13T12:00:00Z" if expired else "2026-09-14T12:00:00Z"
    manifest = _manifest(tmp_path, "ingestion", "stage-ingestion", expiry, "f" * 64)
    owner = _manifest(tmp_path, "owner", "owner-export", expiry, "a" * 64)
    stage = _manifest(tmp_path, "stage", "stage-capability", expiry, "b" * 64)
    observation = StageHistoryMalformedObservation(
        occurrence_id="occurrence-1",
        artifact_id=manifest.artifact_id,
        page_sequence=1,
        row_sequence=1,
        canonical_raw_row_digest=f"sha256:{'c' * 64}",
        safe_error_code="test-only",
        source_observed_at=_NOW,
    )
    page = StageArtifactReplayPage(
        page_sequence=1,
        page_digest=f"sha256:{'d' * 64}",
        rows=(StageArtifactReplayRow(observation=observation, in_scope=False),),
    )
    return VerifiedStageIngestionArtifact(manifest, owner, stage, (page,))


def _manifest(
    tmp_path: Path,
    artifact_id: str,
    kind: str,
    expiry: str,
    manifest_hmac: str,
) -> ArtifactManifest:
    root = tmp_path / artifact_id
    return ArtifactManifest(
        schema_version=1,
        artifact_id=artifact_id,
        artifact_kind=kind,
        created_at="2026-08-01T12:00:00Z",
        retention_expires_at=expiry,
        metadata_json=canonical_metadata_json(
            {
                "entity_type_id": "2",
                "owner_artifact_id": "owner",
                "owner_manifest_digest": "hmac-sha256:owner",
                "stage_artifact_id": "stage",
                "qualification_evidence_digest": f"sha256:{'e' * 64}",
                "canonical_hash_version": "bitrix-stage-history-v1",
                "traversal_contract": "bounded_spool_reconcile",
                "configuration_digest": f"sha256:{'1' * 64}",
                "limits_digest": f"sha256:{'2' * 64}",
            }
        ),
        files=(),
        provenance=ArtifactProvenance(
            artifact_path=str(root),
            primary_device=1,
            primary_inode=1,
            backup_device=1,
            backup_inode=2,
            owner_uid=1,
            group_gid=1,
            directory_mode=0o500,
            source_contract_uuid="12345678-1234-5678-9234-567812345678",
            repository_sha="a" * 40,
            image_digest=f"sha256:{'3' * 64}",
            configuration_digest=f"sha256:{'1' * 64}",
            restricted_boundaries_json='{"upper_history_id":"redacted"}',
            counts_json='{"rows":1}',
            total_bytes=0,
        ),
        backup_path=str(tmp_path / "backup" / artifact_id),
        backup_verified=True,
        signing_key_id="key-1",
        manifest_hmac=manifest_hmac,
    )


def _balanced_row(artifact: VerifiedStageIngestionArtifact) -> Record:
    initial = initial_replay_checkpoint(artifact)
    return _record(
        logical_run_id="logical-1",
        run_type="bounded_smoke_replay",
        units_balanced=True,
        variant_source_records_balanced=True,
        parent_associations_balanced=True,
        committed_pages_contiguous=True,
        checkpoint_revision_balanced=True,
        checkpoint_cursor_page_balanced=True,
        checkpoint_cursor_json_balanced=True,
        replay_boundary_valid=True,
        checkpoint_last_unit_balanced=True,
        committed_counter_balanced=True,
        duplicate_counter_balanced=True,
        excluded_counter_balanced=True,
        retry_counter_balanced=True,
        current_association_partition_balanced=True,
        current_authority_partition_balanced=True,
        current_retry_partition_balanced=True,
        nonterminal_unit_count=0,
        invalid_variant_evidence_count=0,
        shared_variant_evidence_count=0,
        occurrence_variant_identity_count=1,
        invalid_occurrence_variant_link_count=0,
        invalid_empty_occurrence_variant_link_count=0,
        invalid_parent_association_count=0,
        invalid_authority_head_count=0,
        invalid_effective_head_count=0,
        invalid_invalidation_transition_count=0,
        expired_review_claim_count=0,
        invalid_current_association_projection_count=0,
        invalid_empty_association_projection_count=0,
        invalid_current_authority_projection_count=0,
        invalid_empty_authority_projection_count=0,
        invalid_current_retry_projection_count=0,
        invalid_empty_retry_projection_count=0,
        committed_page_sequences=[1],
        committed_unit_ids=["unit-1"],
        committed_unit_digests=[artifact.pages[0].page_digest],
        total_fetched_count=1,
        checkpoint_revision=1,
        checkpoint_source_window_json=json.dumps(
            asdict(initial.source_window), sort_keys=True, separators=(",", ":")
        ),
        total_canonical_effective_count=1,
        total_new_variant_count=1,
        total_selected_active_count=1,
        total_effective_count=1,
        total_retry_none_count=1,
        variant_count=1,
        source_record_count=1,
        invalidation_intent_count=1,
        review_command_count=0,
    )


def _status_row(status: str = "completed") -> Record:
    return _record(
        logical_run_id="logical-1",
        run_type="bounded_smoke_replay",
        logical_status=status,
        ingest_run_id="attempt-1",
        attempt_status=status,
        stream_status="completed",
        stream_generation=1,
        phase="crm_stage_history_artifact_replay_v1",
        checkpoint_revision=1,
        checkpoint_last_page_sequence=1,
        committed_unit_count=1,
        fetched_count=1,
    )


def _repository(
    reconciliation: Record,
    *,
    status: Record | None = None,
) -> StageHistoryStatusRepository:
    client = _Client(
        {
            GET_STAGE_HISTORY_RECONCILIATION: reconciliation,
            GET_STAGE_HISTORY_STATUS: status or _status_row(),
        }
    )
    return StageHistoryStatusRepository(cast(Neo4jClient, client))


def test_balanced_completed_run_is_smoke_ready(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    report = _repository(_balanced_row(artifact)).reconcile(
        "logical-1", artifact=artifact, now=_NOW
    )

    assert report.complete is True
    assert report.smoke_ready is True
    assert report.error_codes == ()
    assert report.committed_unit_count == 1
    assert report.fetched_count == 1
    assert report.accounting.terminal.canonical_effective == 1


def test_status_exposes_only_opaque_progress_and_aggregate_accounting(
    tmp_path: Path,
) -> None:
    artifact = _artifact(tmp_path)
    status = _repository(_balanced_row(artifact)).status("logical-1")

    assert status is not None
    assert status.ingest_run_id == "attempt-1"
    assert status.stream_generation == 1
    assert status.checkpoint_last_page_sequence == 1
    assert status.artifact_id == "ingestion"
    assert status.artifact_manifest_hmac == "f" * 64
    assert status.accounting is not None
    assert status.accounting.terminal.canonical_effective == 1
    assert status.graph_error_codes == ()


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"committed_page_sequences": [2]}, "artifact_page_mismatch"),
        ({"committed_unit_digests": [f"sha256:{'9' * 64}"]}, "artifact_digest_mismatch"),
        ({"total_fetched_count": 2}, "artifact_row_count_mismatch"),
        ({"nonterminal_unit_count": 1}, "nonterminal_unit"),
        ({"invalid_authority_head_count": 1}, "invalid_authority_head"),
        ({"invalid_invalidation_transition_count": 1}, "incomplete_invalidation_targets"),
        ({"checkpoint_revision_balanced": False}, "checkpoint_revision_mismatch"),
        ({"checkpoint_cursor_json_balanced": False}, "checkpoint_cursor_json_mismatch"),
        (
            {"invalid_current_authority_projection_count": 1},
            "stale_authority_projection",
        ),
        (
            {"current_association_partition_balanced": False},
            "current_association_partition_imbalance",
        ),
        ({"run_type": "capture_failure_accounting"}, "artifact_run_type_mismatch"),
    ],
)
def test_reconciliation_reports_hostile_mismatches(
    tmp_path: Path,
    updates: dict[str, object],
    expected: str,
) -> None:
    artifact = _artifact(tmp_path)
    row = dict(_balanced_row(artifact))
    row.update(updates)

    report = _repository(cast(Record, row)).reconcile("logical-1", artifact=artifact, now=_NOW)

    assert report.complete is False
    assert report.smoke_ready is False
    assert expected in report.error_codes


def test_expired_artifact_is_not_smoke_ready(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path, expired=True)
    report = _repository(_balanced_row(artifact)).reconcile(
        "logical-1", artifact=artifact, now=_NOW
    )

    assert report.error_codes == ("artifact_expired",)
    assert report.smoke_ready is False


def test_reconciliation_requires_matching_terminal_control_state(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    report = _repository(
        _balanced_row(artifact),
        status=_status_row("failed"),
    ).reconcile("logical-1", artifact=artifact, now=_NOW)

    assert report.complete is False
    assert report.smoke_ready is False
    assert "logical_status_mismatch" in report.error_codes
    assert "attempt_status_mismatch" in report.error_codes


def test_reconciliation_rejects_a_content_equivalent_artifact_substitution(
    tmp_path: Path,
) -> None:
    artifact = _artifact(tmp_path)
    row = dict(_balanced_row(artifact))
    source_window = json.loads(cast(str, row["checkpoint_source_window_json"]))
    source_window["stage_ingestion_artifact_id"] = "substitute-artifact"
    row["checkpoint_source_window_json"] = json.dumps(
        source_window, sort_keys=True, separators=(",", ":")
    )

    report = _repository(cast(Record, row)).reconcile("logical-1", artifact=artifact, now=_NOW)

    assert "artifact_source_window_mismatch" in report.error_codes
    assert report.smoke_ready is False


def test_status_reports_invalid_checkpoint_source_window_without_exposing_it(
    tmp_path: Path,
) -> None:
    artifact = _artifact(tmp_path)
    row = dict(_balanced_row(artifact))
    row["checkpoint_source_window_json"] = "not-json"

    status = _repository(cast(Record, row)).status("logical-1")

    assert status is not None
    assert status.artifact_id is None
    assert status.artifact_manifest_hmac is None
    assert "checkpoint_source_window_invalid" in status.graph_error_codes


def test_checkpoint_reconstructs_the_committed_ledger(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    checkpoint = _repository(_balanced_row(artifact)).checkpoint("logical-1", artifact=artifact)

    assert checkpoint.last_page_sequence == 1
    assert checkpoint.revision == 1
    assert checkpoint.last_unit_id == "unit-1"
    assert checkpoint.last_unit_digest == artifact.pages[0].page_digest
    assert checkpoint.accounting.terminal.canonical_effective == 1


def test_checkpoint_keeps_immutable_disposition_with_latest_current_projections(
    tmp_path: Path,
) -> None:
    artifact = _artifact(tmp_path)
    row = dict(_balanced_row(artifact))
    row.update(
        {
            "total_canonical_effective_count": 0,
            "total_parent_waiting_count": 1,
            "total_retry_none_count": 0,
            "total_retry_resolved_count": 1,
        }
    )

    checkpoint = _repository(cast(Record, row)).checkpoint("logical-1", artifact=artifact)

    assert checkpoint.accounting.terminal.parent_waiting == 1
    assert checkpoint.accounting.terminal.canonical_effective == 0
    assert checkpoint.accounting.association.selected_active == 1
    assert checkpoint.accounting.authority.effective == 1
    assert checkpoint.accounting.retry.resolved == 1
