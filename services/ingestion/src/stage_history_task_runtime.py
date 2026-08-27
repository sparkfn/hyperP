"""Execution runtime for manual CRM stage-history Celery tasks."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TypedDict, cast

from celery import Task
from pydantic.types import JsonValue

from src.bitrix_ingestion_models import FenceContext
from src.config import get_settings
from src.connectors.bitrix_stage_history.artifact_connector import (
    StageArtifactReplayAuthorization,
    VerifiedStageIngestionArtifact,
    read_stage_ingestion_artifact,
)
from src.connectors.bitrix_stage_history.artifact_runtime import (
    stage_history_store_from_settings,
)
from src.connectors.bitrix_stage_history.connector import (
    StageCaptureLimits,
    stage_capture_limits_digest,
)
from src.graph.bitrix_source_instances import admit_configured_bitrix_control
from src.graph.client import Neo4jClient
from src.graph.ingestion_control import BitrixStreamControl, LogicalRunControl
from src.graph.stage_history_ingestion import StageHistoryIngestionRepository
from src.graph.stage_history_status import StageHistoryStatusRepository
from src.ingestion_config import StageHistoryIngestionConfig, get_ingestion_config
from src.resumable import CheckpointDescriptor
from src.source_instances import LEGACY_DEFAULT_CONTROL_INSTANCE_ID, effective_control_instance_id
from src.stage_history_parent_lifecycle import Neo4jStageHistoryLifecycleReader
from src.stage_history_pipeline import (
    initial_failure_checkpoint,
    initial_replay_checkpoint,
    record_stage_history_capture_failure,
    replay_stage_history_artifact,
)
from src.stage_history_task_lock import StageHistoryTaskLock, stage_history_task_lock


class StageHistoryTaskSummary(TypedDict):
    status: str
    logical_run_id: str
    artifact_id: str
    committed_units: int
    fetched: int


def execute_artifact_task(
    task: Task,
    *,
    artifact_id: str,
    authorization_reference: str,
    failed_capture: bool,
    control_instance_id: str = LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
) -> StageHistoryTaskSummary:
    control_instance_id = effective_control_instance_id(control_instance_id)
    config = get_ingestion_config().stage_history_ingestion
    config.assert_dispatch_enabled(now=datetime.now(UTC))
    _validate_public_arguments(artifact_id, authorization_reference, config)
    task_id = str(task.request.id or "").strip()
    if not task_id:
        raise RuntimeError("stage-history task requires a stable Celery task ID")
    settings = get_settings()
    admit_configured_bitrix_control(settings, control_instance_id)
    with stage_history_task_lock(
        settings.celery_broker_url,
        owner=f"artifact:{task_id}",
        control_instance_id=control_instance_id,
    ) as lock:
        lock.assert_owned()
        store = stage_history_store_from_settings(settings)
        try:
            manifest = store.verify(artifact_id)
            expected_kind = "stage-ingestion-failed" if failed_capture else "stage-ingestion"
            if manifest.artifact_kind != expected_kind:
                raise ValueError("stage-history artifact kind does not match the requested task")
            artifact = read_stage_ingestion_artifact(
                store,
                artifact_id=artifact_id,
                authorization=_replay_authorization(
                    artifact_id,
                    authorization_reference,
                    manifest,
                    config,
                    repository_sha=settings.stage_history_repository_sha,
                    image_digest=settings.stage_history_image_digest,
                ),
            )
        finally:
            store.close()
        lock.assert_owned()
        client = Neo4jClient(settings)
        try:
            return _run_source_free_replay(
                client,
                artifact=artifact,
                worker_task_id=task_id,
                authorization_reference=authorization_reference,
                config=config,
                failed_capture=failed_capture,
                lock=lock,
                control_instance_id=control_instance_id,
            )
        finally:
            client.close()


def _run_source_free_replay(
    client: Neo4jClient,
    *,
    artifact: VerifiedStageIngestionArtifact,
    worker_task_id: str,
    authorization_reference: str,
    config: StageHistoryIngestionConfig,
    failed_capture: bool,
    lock: StageHistoryTaskLock,
    control_instance_id: str = LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
) -> StageHistoryTaskSummary:
    initial = (
        initial_failure_checkpoint(artifact)
        if failed_capture
        else initial_replay_checkpoint(artifact)
    )
    descriptor = _descriptor(initial)
    logical = LogicalRunControl(client, control_instance_id)
    lock.assert_owned()
    attempt = logical.create_or_reuse(
        source_key="bitrix_chat",
        control_instance_id=control_instance_id,
        mode=initial.run_type,
        dump_path=None,
        entity_key=None,
        idempotency_key=_idempotency_key(artifact),
        worker_task_id=worker_task_id,
        configuration_fingerprint=_configuration_fingerprint(
            artifact,
            authorization_reference,
            retry_max_attempts=config.retry_max_attempts,
        ),
        connector_version=initial.connector_version,
        checkpoint_schema_version=initial.schema_version,
        initial_checkpoint=descriptor,
    )
    if attempt.worker_task_id != worker_task_id and attempt.logical_status in {
        "paused_with_checkpoint",
        "failed",
    }:
        resumed = logical.resume(
            logical_run_id=attempt.logical_run_id,
            worker_task_id=worker_task_id,
            configuration_fingerprint=_configuration_fingerprint(
                artifact,
                authorization_reference,
                retry_max_attempts=config.retry_max_attempts,
            ),
            logical_connector_version=initial.connector_version,
            checkpoint_connector_version=initial.connector_version,
            checkpoint_schema_version=initial.schema_version,
        )
        if resumed is None:
            raise RuntimeError("stage-history run could not resume from durable state")
        attempt = resumed
    if attempt.worker_task_id != worker_task_id:
        return _existing_summary(
            client,
            artifact,
            attempt.logical_run_id,
            attempt.logical_status,
            control_instance_id,
        )
    if not logical.claim(
        logical_run_id=attempt.logical_run_id,
        ingest_run_id=attempt.ingest_run_id,
        generation=attempt.generation,
        worker_task_id=worker_task_id,
    ):
        state = logical.get(attempt.logical_run_id)
        status = state.status if state is not None else "already_running"
        return _existing_summary(
            client,
            artifact,
            attempt.logical_run_id,
            status,
            control_instance_id,
        )
    fence: FenceContext | None = None
    try:
        lock.assert_owned()
        admission = BitrixStreamControl(client).admit_or_coalesce(
            stream_key="crm_stage_history",
            logical_run_id=attempt.logical_run_id,
            ingest_run_id=attempt.ingest_run_id,
            attempt_generation=attempt.generation,
            worker_task_id=worker_task_id,
            control_instance_id=control_instance_id,
            replace_active=attempt.generation > 1,
        )
        fence = admission.fence_context
        checkpoint = StageHistoryStatusRepository(client, control_instance_id).checkpoint(
            attempt.logical_run_id,
            artifact=artifact,
        )
        repository = StageHistoryIngestionRepository(
            client,
            retry_max_attempts=config.retry_max_attempts,
        )

        def stop_requested() -> bool:
            lock.assert_owned()
            state = logical.get(attempt.logical_run_id)
            return state is not None and state.stop_requested

        result = (
            record_stage_history_capture_failure(
                artifact,
                repository=repository,
                checkpoint=checkpoint,
                fence=fence,
                stop_requested=stop_requested,
            )
            if failed_capture
            else replay_stage_history_artifact(
                artifact,
                lifecycle=Neo4jStageHistoryLifecycleReader(client),
                repository=repository,
                checkpoint=checkpoint,
                fence=fence,
                stop_requested=stop_requested,
            )
        )
        if result.stopped:
            lock.assert_owned()
            logical.pause_fenced(context=fence, phase=checkpoint.phase)
            return _summary("paused_with_checkpoint", attempt.logical_run_id, artifact, result)
        counts = result.checkpoint.accounting
        terminal = counts.terminal
        lock.assert_owned()
        logical.finalize_fenced(
            context=fence,
            phase=result.checkpoint.phase,
            status="completed_with_errors" if failed_capture else "completed",
            committed_count=(
                terminal.canonical_effective
                + terminal.canonical_pending_parent
                + terminal.parent_waiting
                + terminal.parent_ambiguous
                + terminal.differing_hash_conflict
            ),
            duplicate_count=terminal.same_hash_replay,
            excluded_count=(
                terminal.malformed_excluded
                + terminal.capture_rejected_valid
                + terminal.excluded_out_of_scope
            ),
            retry_count=(
                terminal.canonical_pending_parent
                + terminal.parent_waiting
                + terminal.parent_ambiguous
            ),
            record_count=terminal.fetched,
            rejected_count=terminal.fetched if failed_capture else 0,
        )
        status = "completed_with_errors" if failed_capture else "completed"
        return _summary(status, attempt.logical_run_id, artifact, result)
    except Exception as exc:
        lock.assert_owned()
        if fence is None:
            logical.fail(
                logical_run_id=attempt.logical_run_id,
                ingest_run_id=attempt.ingest_run_id,
                generation=attempt.generation,
                failure_category="stage_history_execution_failed",
                safe_failure_message=type(exc).__name__,
            )
        else:
            logical.fail_fenced(
                context=fence,
                failure_category="stage_history_execution_failed",
                safe_failure_message=type(exc).__name__,
            )
        raise


def _descriptor(snapshot: object) -> CheckpointDescriptor:
    from src.stage_history_ingestion_models import StageHistoryCheckpointSnapshot

    if not isinstance(snapshot, StageHistoryCheckpointSnapshot):
        raise TypeError("stage-history checkpoint snapshot is invalid")
    return CheckpointDescriptor(
        phase=snapshot.phase,
        cursor={
            "last_page_sequence": snapshot.last_page_sequence,
            "revision": snapshot.revision,
        },
        source_window=cast(dict[str, JsonValue], asdict(snapshot.source_window)),
        last_committed_record_id=snapshot.last_unit_id,
        connector_version=snapshot.connector_version,
        schema_version=snapshot.schema_version,
        replay_boundary=snapshot.replay_boundary,
    )


def _replay_authorization(
    artifact_id: str,
    reference: str,
    manifest: object,
    config: StageHistoryIngestionConfig,
    *,
    repository_sha: str,
    image_digest: str,
) -> StageArtifactReplayAuthorization:
    from src.connectors.bitrix_stage_history.artifact_manifest import ArtifactManifest

    if not isinstance(manifest, ArtifactManifest):
        raise TypeError("stage-history artifact manifest is invalid")
    if reference != config.authorization_reference:
        raise PermissionError("stage-history authorization reference changed")
    limits_digest = stage_capture_limits_digest(
        StageCaptureLimits(
            max_calls=config.max_calls,
            max_rows=config.max_rows,
            max_spool_bytes=config.max_spool_bytes,
            max_runtime_seconds=config.max_runtime_seconds,
        )
    )
    return StageArtifactReplayAuthorization(
        reference=reference,
        actor=config.authorized_actor,
        artifact_id=artifact_id,
        manifest_hmac=manifest.manifest_hmac,
        artifact_kind=manifest.artifact_kind,
        manifest_schema_version=manifest.schema_version,
        repository_sha=repository_sha,
        image_digest=image_digest,
        source_contract_uuid=config.source_contract_uuid,
        entity_type_id=str(config.entity_type_id),
        owner_artifact_id=config.owner_artifact_id,
        owner_manifest_hmac=config.owner_manifest_hmac,
        stage_artifact_id=config.stage_artifact_id,
        stage_manifest_hmac=config.stage_manifest_hmac,
        qualification_evidence_digest=config.qualification_evidence_digest,
        configuration_digest=config.accepted_configuration_digest,
        limits_digest=limits_digest,
        canonical_hash_version="bitrix-stage-history-v1",
        traversal_contract="bounded_spool_reconcile",
    )


def _validate_public_arguments(
    artifact_id: str,
    authorization_reference: str,
    config: StageHistoryIngestionConfig,
) -> None:
    if not artifact_id.strip() or not authorization_reference.strip():
        raise ValueError("stage-history task arguments must be non-empty")
    if authorization_reference != config.authorization_reference:
        raise PermissionError("stage-history authorization reference changed")


def _idempotency_key(artifact: VerifiedStageIngestionArtifact) -> str:
    prefix = (
        "bitrix-stage-history:capture-failure"
        if artifact.manifest.artifact_kind == "stage-ingestion-failed"
        else "bitrix-stage-history:artifact-replay"
    )
    return f"{prefix}:{artifact.manifest.artifact_id}:{artifact.manifest.manifest_hmac}"


def _configuration_fingerprint(
    artifact: VerifiedStageIngestionArtifact,
    authorization_reference: str,
    *,
    retry_max_attempts: int,
) -> str:
    payload = json.dumps(
        {
            "artifact_id": artifact.manifest.artifact_id,
            "manifest_hmac": artifact.manifest.manifest_hmac,
            "artifact_kind": artifact.manifest.artifact_kind,
            "authorization_reference": authorization_reference,
            "retry_max_attempts": retry_max_attempts,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _existing_summary(
    client: Neo4jClient,
    artifact: VerifiedStageIngestionArtifact,
    logical_run_id: str,
    status: str,
    control_instance_id: str = LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
) -> StageHistoryTaskSummary:
    current = StageHistoryStatusRepository(client, control_instance_id).status(logical_run_id)
    return {
        "status": status,
        "logical_run_id": logical_run_id,
        "artifact_id": artifact.manifest.artifact_id,
        "committed_units": current.committed_unit_count if current is not None else 0,
        "fetched": current.fetched_count if current is not None else 0,
    }


def _summary(
    status: str,
    logical_run_id: str,
    artifact: VerifiedStageIngestionArtifact,
    result: object,
) -> StageHistoryTaskSummary:
    from src.stage_history_pipeline import StageHistoryPipelineResult

    if not isinstance(result, StageHistoryPipelineResult):
        raise TypeError("stage-history pipeline result is invalid")
    return {
        "status": status,
        "logical_run_id": logical_run_id,
        "artifact_id": artifact.manifest.artifact_id,
        "committed_units": result.checkpoint.committed_unit_count,
        "fetched": result.checkpoint.accounting.terminal.fetched,
    }
