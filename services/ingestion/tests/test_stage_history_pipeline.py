"""Focused unit tests for source-free stage-history replay pipelines."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import src.stage_history_pipeline as stage_history_pipeline
from src.bitrix_ingestion_models import FenceContext
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
from src.connectors.bitrix_stage_history.models import StageHistoryItem
from src.models import JsonValue
from src.stage_history_ingestion_models import (
    StageHistoryCheckpointSnapshot,
    StageHistoryMalformedObservation,
    StageHistoryOccurrence,
    StageHistoryReplaySourceWindow,
    StageHistoryReplayUnit,
    StageHistoryUnitResult,
    StageHistoryValidObservation,
    advance_stage_history_checkpoint,
)
from src.stage_history_parent_lifecycle import StageHistoryLifecycleSnapshot
from src.stage_history_pipeline import (
    initial_failure_checkpoint,
    initial_replay_checkpoint,
    record_stage_history_capture_failure,
    replay_stage_history_artifact,
)

_SOURCE_CONTRACT = "12345678-1234-5678-9234-567812345678"
_OBSERVED_AT = datetime(2026, 8, 14, 4, 5, 6, tzinfo=UTC)


class _Lifecycle:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def classify(
        self,
        observation: StageHistoryValidObservation,
    ) -> StageHistoryLifecycleSnapshot:
        self.calls.append(observation.occurrence_id)
        return StageHistoryLifecycleSnapshot(
            identity_hash_state="new_variant",
            association_state="selected_active",
            authority_state="effective",
        )


class _Repository:
    def __init__(self) -> None:
        self.calls: list[
            tuple[StageHistoryReplayUnit, StageHistoryCheckpointSnapshot, FenceContext]
        ] = []

    def persist_unit(
        self,
        unit: StageHistoryReplayUnit,
        expected_checkpoint: StageHistoryCheckpointSnapshot,
        fence: FenceContext,
    ) -> StageHistoryUnitResult:
        self.calls.append((unit, expected_checkpoint, fence))
        checkpoint_after = advance_stage_history_checkpoint(expected_checkpoint, unit)
        return StageHistoryUnitResult(
            outcome="committed",
            unit=unit,
            checkpoint_before=expected_checkpoint,
            checkpoint_after=checkpoint_after,
        )


def test_successful_replay_persists_once_per_page_and_classifies_only_in_scope(
    tmp_path: Path,
) -> None:
    artifact = _successful_artifact(tmp_path)
    lifecycle = _Lifecycle()
    repository = _Repository()
    checkpoint = initial_replay_checkpoint(artifact)
    fence = _fence()

    result = replay_stage_history_artifact(
        artifact,
        lifecycle=lifecycle,
        repository=repository,
        checkpoint=checkpoint,
        fence=fence,
    )

    assert len(repository.calls) == len(artifact.pages) == 2
    assert [call[0].page_sequence for call in repository.calls] == [1, 2]
    assert all(call[2] == fence for call in repository.calls)
    assert lifecycle.calls == ["occurrence-in-scope-1", "occurrence-in-scope-2"]
    first_occurrences = repository.calls[0][0].occurrences
    assert [item.disposition for item in first_occurrences] == [
        "canonical_effective",
        "excluded_out_of_scope",
    ]
    assert first_occurrences[1].identity_hash_state is None
    assert result.checkpoint.committed_unit_count == 2
    assert result.checkpoint.last_page_sequence == 2
    assert len(result.units) == 2


def test_legacy_artifact_without_capture_mode_defaults_to_bounded_smoke(
    tmp_path: Path,
) -> None:
    artifact = _successful_artifact(tmp_path)

    checkpoint = initial_replay_checkpoint(artifact)

    assert checkpoint.run_type == "bounded_smoke_replay"


@pytest.mark.parametrize(
    ("mode", "expected_run_type"),
    [
        ("collect-smoke", "bounded_smoke_replay"),
        ("collect-backfill", "authoritative_backfill_replay"),
        ("collect-catch-up", "authoritative_catch_up_replay"),
    ],
)
def test_capture_mode_selects_replay_run_type(
    tmp_path: Path,
    mode: str,
    expected_run_type: str,
) -> None:
    artifact = _with_capture_mode(_successful_artifact(tmp_path), mode)

    checkpoint = initial_replay_checkpoint(artifact)

    assert checkpoint.run_type == expected_run_type


def test_nonempty_unknown_capture_mode_fails_closed(tmp_path: Path) -> None:
    artifact = _with_capture_mode(_successful_artifact(tmp_path), "collect-unknown")

    with pytest.raises(RuntimeError, match="unknown capture mode"):
        initial_replay_checkpoint(artifact)


def test_failure_accounting_has_only_failure_dispositions_and_no_lifecycle_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _failure_artifact(tmp_path)
    repository = _Repository()

    def unexpected_lifecycle(
        observation: StageHistoryValidObservation,
        snapshot: StageHistoryLifecycleSnapshot,
    ) -> StageHistoryOccurrence:
        _ = observation, snapshot
        pytest.fail("failure accounting attempted lifecycle materialization")

    monkeypatch.setattr(
        stage_history_pipeline,
        "build_lifecycle_occurrence",
        unexpected_lifecycle,
    )
    result = record_stage_history_capture_failure(
        artifact,
        repository=repository,
        checkpoint=initial_failure_checkpoint(artifact),
        fence=_fence(),
    )

    assert len(repository.calls) == 1
    unit = repository.calls[0][0]
    assert [item.disposition for item in unit.occurrences] == [
        "malformed_excluded",
        "capture_rejected_valid",
        "capture_rejected_valid",
    ]
    assert [item.parse_scope for item in unit.occurrences] == [
        "malformed",
        "in_scope",
        "out_of_scope",
    ]
    assert all(item.identity_hash_state is None for item in unit.occurrences)
    assert all(item.association_state is None for item in unit.occurrences)
    assert all(item.authority_state is None for item in unit.occurrences)
    assert result.units[0].association_decisions == ()
    assert result.units[0].authority_transitions == ()
    assert result.units[0].retries == ()
    assert result.units[0].invalidation_intents == ()


def test_resume_skips_pages_already_committed_in_checkpoint(tmp_path: Path) -> None:
    full_artifact = _successful_artifact(tmp_path)
    first_page_artifact = replace(full_artifact, pages=(full_artifact.pages[0],))
    first_repository = _Repository()
    first_result = replay_stage_history_artifact(
        first_page_artifact,
        lifecycle=_Lifecycle(),
        repository=first_repository,
        checkpoint=initial_replay_checkpoint(first_page_artifact),
        fence=_fence(),
    )
    resumed_lifecycle = _Lifecycle()
    resumed_repository = _Repository()

    resumed = replay_stage_history_artifact(
        full_artifact,
        lifecycle=resumed_lifecycle,
        repository=resumed_repository,
        checkpoint=first_result.checkpoint,
        fence=_fence(),
    )

    assert [call[0].page_sequence for call in resumed_repository.calls] == [2]
    assert resumed_lifecycle.calls == ["occurrence-in-scope-2"]
    assert resumed.checkpoint.committed_unit_count == 2
    assert len(resumed.units) == 1


def test_replay_rejects_source_window_artifact_mismatch_before_calls(tmp_path: Path) -> None:
    artifact = _successful_artifact(tmp_path)
    checkpoint = initial_replay_checkpoint(artifact)
    assert isinstance(checkpoint.source_window, StageHistoryReplaySourceWindow)
    mismatched = replace(
        checkpoint,
        source_window=replace(
            checkpoint.source_window,
            stage_ingestion_artifact_id="different-artifact",
        ),
    )
    lifecycle = _Lifecycle()
    repository = _Repository()

    with pytest.raises(ValueError, match="checkpoint artifact changed"):
        replay_stage_history_artifact(
            artifact,
            lifecycle=lifecycle,
            repository=repository,
            checkpoint=mismatched,
            fence=_fence(),
        )

    assert lifecycle.calls == []
    assert repository.calls == []


def _successful_artifact(tmp_path: Path) -> VerifiedStageIngestionArtifact:
    manifest, owner, stage = _manifests(tmp_path, kind="stage-ingestion")
    first = StageArtifactReplayPage(
        page_sequence=1,
        page_digest=f"sha256:{'1' * 64}",
        rows=(
            StageArtifactReplayRow(_valid_observation("in-scope-1", "501", 1, 1), True),
            StageArtifactReplayRow(_valid_observation("out-of-scope", "999", 1, 2), False),
        ),
    )
    second = StageArtifactReplayPage(
        page_sequence=2,
        page_digest=f"sha256:{'2' * 64}",
        rows=(StageArtifactReplayRow(_valid_observation("in-scope-2", "501", 2, 3), True),),
    )
    return VerifiedStageIngestionArtifact(manifest, owner, stage, (first, second))


def _with_capture_mode(
    artifact: VerifiedStageIngestionArtifact,
    mode: str,
) -> VerifiedStageIngestionArtifact:
    metadata = dict(artifact.manifest.metadata)
    metadata["mode"] = mode
    manifest = replace(
        artifact.manifest,
        metadata_json=canonical_metadata_json(metadata),
    )
    return replace(artifact, manifest=manifest)


def _failure_artifact(tmp_path: Path) -> VerifiedStageIngestionArtifact:
    manifest, owner, stage = _manifests(tmp_path, kind="stage-ingestion-failed")
    page = StageArtifactReplayPage(
        page_sequence=1,
        page_digest=f"sha256:{'3' * 64}",
        rows=(
            StageArtifactReplayRow(
                StageHistoryMalformedObservation(
                    occurrence_id="malformed-occurrence",
                    artifact_id=manifest.artifact_id,
                    page_sequence=1,
                    row_sequence=1,
                    canonical_raw_row_digest=f"sha256:{'4' * 64}",
                    safe_error_code="missing_history_id",
                    source_observed_at=_OBSERVED_AT,
                ),
                False,
            ),
            StageArtifactReplayRow(_valid_observation("rejected-valid", "501", 1, 2), True),
            StageArtifactReplayRow(
                _valid_observation("rejected-out-of-scope", "999", 1, 3),
                False,
            ),
        ),
    )
    return VerifiedStageIngestionArtifact(manifest, owner, stage, (page,))


def _valid_observation(
    suffix: str,
    owner_id: str,
    page_sequence: int,
    row_sequence: int,
) -> StageHistoryValidObservation:
    item = StageHistoryItem(
        history_id=str(row_sequence),
        entity_type_id="2",
        owner_id=owner_id,
        type_id="1",
        created_time=datetime(2026, 8, 14, 2, 30, tzinfo=UTC),
        created_time_source="2026-08-14T02:30:00+00:00",
        category_id="2",
        stage_semantic_id="P",
        stage_id="C2:NEW",
        raw_payload={"ID": str(row_sequence), "OWNER_ID": owner_id},
    )
    return StageHistoryValidObservation(
        occurrence_id=f"occurrence-{suffix}",
        artifact_id="ingestion-artifact",
        page_sequence=page_sequence,
        row_sequence=row_sequence,
        event_identity=f"event-{suffix}",
        canonical_hash=f"sha256:{row_sequence:064x}",
        item=item,
        logical_parent_source_system="bitrix_chat",
        logical_parent_source_record_id=f"bitrix-crm-deal-{owner_id}",
        source_observed_at=_OBSERVED_AT,
    )


def _manifests(
    tmp_path: Path,
    *,
    kind: str,
) -> tuple[ArtifactManifest, ArtifactManifest, ArtifactManifest]:
    owner = _manifest(
        "owner-artifact",
        "owner-export",
        tmp_path / "owner",
        {"owner_manifest_digest": "hmac-sha256:owner"},
        "a" * 64,
    )
    stage = _manifest(
        "stage-artifact",
        "stage-capability",
        tmp_path / "stage",
        {"owner_artifact_id": owner.artifact_id},
        "b" * 64,
    )
    ingestion = _manifest(
        "ingestion-artifact",
        kind,
        tmp_path / "ingestion",
        {
            "entity_type_id": "2",
            "owner_manifest_digest": "hmac-sha256:owner",
            "qualification_evidence_digest": f"sha256:{'d' * 64}",
            "canonical_hash_version": "bitrix-stage-history-v1",
            "traversal_contract": "bounded_spool_reconcile",
            "configuration_digest": f"sha256:{'c' * 64}",
            "limits_digest": f"sha256:{'e' * 64}",
        },
        "f" * 64,
    )
    return ingestion, owner, stage


def _manifest(
    artifact_id: str,
    artifact_kind: str,
    root: Path,
    metadata: dict[str, JsonValue],
    manifest_hmac: str,
) -> ArtifactManifest:
    return ArtifactManifest(
        schema_version=1,
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        created_at="2026-08-14T03:00:00Z",
        retention_expires_at="2026-09-14T03:00:00Z",
        metadata_json=canonical_metadata_json(metadata),
        files=(),
        provenance=ArtifactProvenance(
            artifact_path=str(root.absolute()),
            primary_device=1,
            primary_inode=1,
            backup_device=1,
            backup_inode=2,
            owner_uid=1,
            group_gid=1,
            directory_mode=0o500,
            source_contract_uuid=_SOURCE_CONTRACT,
            repository_sha="a" * 40,
            image_digest=f"sha256:{'b' * 64}",
            configuration_digest=f"sha256:{'c' * 64}",
            restricted_boundaries_json='{"upper_history_id":"200"}',
            counts_json='{"rows":1}',
            total_bytes=0,
        ),
        backup_path=str((root.parent / "backup" / artifact_id).absolute()),
        backup_verified=True,
        signing_key_id="key-1",
        manifest_hmac=manifest_hmac,
    )


def _fence() -> FenceContext:
    return FenceContext(
        logical_run_id="logical-run",
        ingest_run_id="ingest-run",
        source_key="bitrix_chat",
        stream_key="crm_stage_history",
        stream_generation=1,
        fencing_token=1,
        attempt_generation=1,
    )
