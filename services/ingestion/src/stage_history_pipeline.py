"""Source-free replay pipelines for sealed stage-history capture artifacts."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from src.bitrix_ingestion_models import FenceContext
from src.connectors.bitrix_stage_history.artifact_connector import (
    StageArtifactReplayRow,
    VerifiedStageIngestionArtifact,
)
from src.stage_history_ingestion_models import (
    StageHistoryAccounting,
    StageHistoryAssociationAccounting,
    StageHistoryAuthorityAccounting,
    StageHistoryCheckpointSnapshot,
    StageHistoryFailureSourceWindow,
    StageHistoryIdentityAccounting,
    StageHistoryMalformedObservation,
    StageHistoryOccurrence,
    StageHistoryReplayRunType,
    StageHistoryReplaySourceWindow,
    StageHistoryReplayUnit,
    StageHistoryRetryAccounting,
    StageHistoryTerminalAccounting,
    StageHistoryUnitResult,
    StageHistoryValidObservation,
)
from src.stage_history_parent_lifecycle import (
    StageHistoryLifecycleReader,
    build_lifecycle_occurrence,
)


class StageHistoryUnitRepository(Protocol):
    def persist_unit(
        self,
        unit: StageHistoryReplayUnit,
        expected_checkpoint: StageHistoryCheckpointSnapshot,
        fence: FenceContext,
    ) -> StageHistoryUnitResult: ...


@dataclass(frozen=True, slots=True)
class StageHistoryPipelineResult:
    checkpoint: StageHistoryCheckpointSnapshot
    units: tuple[StageHistoryUnitResult, ...]
    stopped: bool = False


def initial_replay_checkpoint(
    artifact: VerifiedStageIngestionArtifact,
) -> StageHistoryCheckpointSnapshot:
    manifest = artifact.manifest
    return StageHistoryCheckpointSnapshot(
        run_type="bounded_smoke_replay",
        source_window=StageHistoryReplaySourceWindow(
            stage_ingestion_artifact_id=manifest.artifact_id,
            artifact_manifest_hmac=manifest.manifest_hmac,
            source_contract_uuid=manifest.provenance.source_contract_uuid,
            entity_type_id=_metadata(manifest, "entity_type_id"),
            owner_artifact_id=artifact.owner_manifest.artifact_id,
            owner_manifest_digest=_metadata(manifest, "owner_manifest_digest"),
            stage_artifact_id=artifact.stage_manifest.artifact_id,
            qualification_evidence_digest=_metadata(manifest, "qualification_evidence_digest"),
            canonical_hash_version=_metadata(manifest, "canonical_hash_version"),
            traversal_contract=_metadata(manifest, "traversal_contract"),
            configuration_digest=_metadata(manifest, "configuration_digest"),
            limits_digest=_metadata(manifest, "limits_digest"),
        ),
        last_page_sequence=None,
        revision=0,
        committed_unit_count=0,
        last_unit_id=None,
        last_unit_digest=None,
        accounting=_empty_accounting(),
    )


def initial_failure_checkpoint(
    artifact: VerifiedStageIngestionArtifact,
) -> StageHistoryCheckpointSnapshot:
    manifest = artifact.manifest
    return StageHistoryCheckpointSnapshot(
        run_type="capture_failure_accounting",
        source_window=StageHistoryFailureSourceWindow(
            failed_artifact_id=manifest.artifact_id,
            manifest_hmac=manifest.manifest_hmac,
            source_contract_uuid=manifest.provenance.source_contract_uuid,
            stage_artifact_id=artifact.stage_manifest.artifact_id,
            qualification_evidence_digest=_metadata(manifest, "qualification_evidence_digest"),
            configuration_digest=_metadata(manifest, "configuration_digest"),
            limits_digest=_metadata(manifest, "limits_digest"),
        ),
        last_page_sequence=None,
        revision=0,
        committed_unit_count=0,
        last_unit_id=None,
        last_unit_digest=None,
        accounting=_empty_accounting(),
    )


def replay_stage_history_artifact(
    artifact: VerifiedStageIngestionArtifact,
    *,
    lifecycle: StageHistoryLifecycleReader,
    repository: StageHistoryUnitRepository,
    checkpoint: StageHistoryCheckpointSnapshot,
    fence: FenceContext,
    stop_requested: Callable[[], bool] | None = None,
) -> StageHistoryPipelineResult:
    """Replay every immutable successful page, one fenced transaction per page."""
    if artifact.manifest.artifact_kind != "stage-ingestion":
        raise ValueError("successful replay requires a stage-ingestion artifact")
    _validate_checkpoint_artifact(checkpoint, artifact, "bounded_smoke_replay")
    current = checkpoint
    results: list[StageHistoryUnitResult] = []
    for page in artifact.pages:
        if stop_requested is not None and stop_requested():
            return StageHistoryPipelineResult(current, tuple(results), stopped=True)
        if page.page_sequence <= current.committed_unit_count:
            continue
        occurrences: list[StageHistoryOccurrence] = []
        for row in page.rows:
            observation = row.observation
            if not isinstance(observation, StageHistoryValidObservation):
                raise RuntimeError("qualified stage ingestion contains a malformed row")
            if row.in_scope:
                occurrences.append(
                    build_lifecycle_occurrence(
                        observation,
                        lifecycle.classify(observation),
                    )
                )
            else:
                occurrences.append(
                    StageHistoryOccurrence(
                        observation=observation,
                        disposition="excluded_out_of_scope",
                        parse_scope="out_of_scope",
                    )
                )
        unit = _unit(
            run_type="bounded_smoke_replay",
            artifact_id=artifact.manifest.artifact_id,
            page_sequence=page.page_sequence,
            page_digest=page.page_digest,
            occurrences=tuple(occurrences),
        )
        result = repository.persist_unit(unit, current, fence)
        current = result.checkpoint_after
        results.append(result)
    return StageHistoryPipelineResult(current, tuple(results))


def record_stage_history_capture_failure(
    artifact: VerifiedStageIngestionArtifact,
    *,
    repository: StageHistoryUnitRepository,
    checkpoint: StageHistoryCheckpointSnapshot,
    fence: FenceContext,
    stop_requested: Callable[[], bool] | None = None,
) -> StageHistoryPipelineResult:
    """Persist failed-capture accounting without canonical/domain mutations."""
    if artifact.manifest.artifact_kind != "stage-ingestion-failed":
        raise ValueError("failure accounting requires a stage-ingestion-failed artifact")
    _validate_checkpoint_artifact(checkpoint, artifact, "capture_failure_accounting")
    current = checkpoint
    results: list[StageHistoryUnitResult] = []
    for page in artifact.pages:
        if stop_requested is not None and stop_requested():
            return StageHistoryPipelineResult(current, tuple(results), stopped=True)
        if page.page_sequence <= current.committed_unit_count:
            continue
        occurrences = tuple(_failure_occurrence(row) for row in page.rows)
        unit = _unit(
            run_type="capture_failure_accounting",
            artifact_id=artifact.manifest.artifact_id,
            page_sequence=page.page_sequence,
            page_digest=page.page_digest,
            occurrences=occurrences,
        )
        result = repository.persist_unit(unit, current, fence)
        current = result.checkpoint_after
        results.append(result)
    return StageHistoryPipelineResult(current, tuple(results))


def _failure_occurrence(
    row: StageArtifactReplayRow,
) -> StageHistoryOccurrence:
    observation = row.observation
    if isinstance(observation, StageHistoryMalformedObservation):
        return StageHistoryOccurrence(
            observation=observation,
            disposition="malformed_excluded",
            parse_scope="malformed",
        )
    return StageHistoryOccurrence(
        observation=observation,
        disposition="capture_rejected_valid",
        parse_scope="in_scope" if row.in_scope else "out_of_scope",
    )


def _unit(
    *,
    run_type: StageHistoryReplayRunType,
    artifact_id: str,
    page_sequence: int,
    page_digest: str,
    occurrences: tuple[StageHistoryOccurrence, ...],
) -> StageHistoryReplayUnit:
    unit_id = _unit_id(run_type, artifact_id, page_sequence, page_digest)
    accounting = StageHistoryAccounting.from_occurrences(occurrences)
    return StageHistoryReplayUnit(
        run_type=run_type,
        unit_id=unit_id,
        artifact_id=artifact_id,
        page_sequence=page_sequence,
        page_digest=page_digest,
        occurrences=occurrences,
        accounting=accounting,
    )


def _unit_id(run_type: str, artifact_id: str, page_sequence: int, page_digest: str) -> str:
    digest = hashlib.sha256()
    for value in (
        "bitrix-stage-history-replay-unit-v1",
        run_type,
        artifact_id,
        str(page_sequence),
        page_digest,
    ):
        digest.update(value.encode("utf-8"))
        digest.update(b"\x00")
    return "sha256:" + digest.hexdigest()


def _validate_checkpoint_artifact(
    checkpoint: StageHistoryCheckpointSnapshot,
    artifact: VerifiedStageIngestionArtifact,
    expected_run_type: str,
) -> None:
    if checkpoint.run_type != expected_run_type:
        raise ValueError("stage-history checkpoint run type changed")
    source_window = checkpoint.source_window
    artifact_id = (
        source_window.stage_ingestion_artifact_id
        if isinstance(source_window, StageHistoryReplaySourceWindow)
        else source_window.failed_artifact_id
    )
    if artifact_id != artifact.manifest.artifact_id:
        raise ValueError("stage-history checkpoint artifact changed")
    expected_window = (
        initial_replay_checkpoint(artifact).source_window
        if expected_run_type == "bounded_smoke_replay"
        else initial_failure_checkpoint(artifact).source_window
    )
    if source_window != expected_window:
        raise ValueError("stage-history checkpoint source window changed")
    if checkpoint.committed_unit_count > len(artifact.pages):
        raise ValueError("stage-history checkpoint exceeds the sealed page inventory")
    if checkpoint.committed_unit_count:
        page = artifact.pages[checkpoint.committed_unit_count - 1]
        if (
            checkpoint.last_page_sequence != page.page_sequence
            or checkpoint.last_unit_digest != page.page_digest
            or checkpoint.last_unit_id
            != _unit_id(
                checkpoint.run_type,
                artifact.manifest.artifact_id,
                page.page_sequence,
                page.page_digest,
            )
        ):
            raise ValueError("stage-history checkpoint tail differs from the sealed artifact")


def _metadata(manifest: object, key: str) -> str:
    from src.connectors.bitrix_stage_history.artifact_manifest import ArtifactManifest

    if not isinstance(manifest, ArtifactManifest):
        raise TypeError("stage-history artifact manifest is invalid")
    value = manifest.metadata.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"sealed stage ingestion metadata omitted {key}")
    return value


def _empty_accounting() -> StageHistoryAccounting:
    return StageHistoryAccounting(
        terminal=StageHistoryTerminalAccounting(),
        identity=StageHistoryIdentityAccounting(),
        association=StageHistoryAssociationAccounting(),
        authority=StageHistoryAuthorityAccounting(),
        retry=StageHistoryRetryAccounting(),
    )
