"""Durable queueing and recovery controls for stage-history review commands."""

from __future__ import annotations

import argparse
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from pydantic.types import JsonValue

from src.config import Settings, get_settings
from src.graph.bitrix_source_instances import admit_configured_bitrix_control
from src.graph.client import Neo4jClient
from src.graph.ingestion_control import BitrixStreamControl, LogicalRunControl
from src.graph.stage_history_review import StageHistoryReviewRepository
from src.ingestion_config import StageHistoryIngestionConfig
from src.resumable import CheckpointDescriptor
from src.source_instances import LEGACY_DEFAULT_CONTROL_INSTANCE_ID
from src.stage_history_ingestion_models import (
    StageHistoryAuthorityState,
    StageHistoryReviewCommand,
    StageHistoryReviewKind,
    stage_history_review_configuration_fingerprint,
)
from src.stage_history_task_lock import StageHistoryTaskLock, stage_history_task_lock
from src.stage_history_tasks import execute_stage_history_review_task


@dataclass(frozen=True, slots=True)
class _PreparedReview:
    task_id: str
    logical_run_id: str
    command_id: str
    review_kind: StageHistoryReviewKind
    authorization_reference: str
    fence: object | None
    control_instance_id: str = LEGACY_DEFAULT_CONTROL_INSTANCE_ID


def _queue_review(
    args: argparse.Namespace,
    config: StageHistoryIngestionConfig,
) -> dict[str, JsonValue]:
    if args.authorization_reference != config.authorization_reference:
        raise PermissionError("stage-history review authorization changed")
    if args.reviewer != config.authorized_actor:
        raise PermissionError("stage-history review actor changed")
    kind_by_command: dict[str, StageHistoryReviewKind] = {
        "resolve-parents": "resolve_parent",
        "reject-parent": "reject_parent",
        "resolve-conflict": "resolve_conflict",
        "apply-correction": "apply_correction",
    }
    kind = kind_by_command[cast(str, args.command)]
    command_id = cast(str | None, args.command_id) or uuid.uuid4().hex
    requested_state = cast(str | None, args.expected_authority_state)
    expected_state: StageHistoryAuthorityState
    if kind in {"resolve_parent", "reject_parent"}:
        if requested_state not in {None, "withheld_parent", "withheld_conflict"}:
            raise ValueError("review command expected authority state is incompatible")
        expected_state = cast(
            StageHistoryAuthorityState,
            requested_state or "withheld_parent",
        )
    elif kind == "resolve_conflict":
        if requested_state is not None and requested_state != "withheld_conflict":
            raise ValueError("review command expected authority state is incompatible")
        expected_state = "withheld_conflict"
    else:
        if requested_state is None:
            raise ValueError("correction review requires the current authority state")
        expected_state = cast(StageHistoryAuthorityState, requested_state)
    command = StageHistoryReviewCommand(
        command_id=command_id,
        kind=kind,
        status="pending",
        event_identity=cast(str, args.event_identity),
        reviewer_id=cast(str, args.reviewer),
        available_at=datetime.now(UTC),
        expected_head_version=cast(int, args.expected_head_version),
        expected_authority_token=cast(int, args.expected_authority_token),
        expected_authority_state=expected_state,
        expected_variant_set_digest=cast(str, args.expected_variant_set_digest),
        retry_sequence=cast(int | None, args.retry_sequence),
        selected_variant_hash=cast(str | None, args.selected_variant_hash),
        selected_association_decision_id=cast(str | None, args.selected_association_decision_id),
        correction_of_decision_id=cast(str | None, args.correction_of_decision_id),
    )
    task_id = uuid.uuid4().hex
    run_type = {
        "resolve_parent": "parent_reconcile",
        "reject_parent": "parent_reconcile",
        "resolve_conflict": "conflict_review",
        "apply_correction": "correction_review",
    }[kind]
    auth_digest = (
        "sha256:" + hashlib.sha256(config.authorization_reference.encode("utf-8")).hexdigest()
    )
    checkpoint = _review_checkpoint(command_id, auth_digest)
    configuration_fingerprint = stage_history_review_configuration_fingerprint(
        command_id,
        kind,
        config.authorization_reference,
        review_lease_seconds=config.review_lease_seconds,
        retry_backoff_seconds=config.retry_backoff_seconds,
    )
    settings = get_settings()
    admit_configured_bitrix_control(settings, LEGACY_DEFAULT_CONTROL_INSTANCE_ID)
    with stage_history_task_lock(
        settings.celery_broker_url,
        owner=f"review-queue:{task_id}",
        control_instance_id=LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
    ) as lock:
        lock.assert_owned()
        prepared = _queue_review_locked(
            command,
            occurrence_id=cast(str, args.occurrence_id),
            authorization_reference=config.authorization_reference,
            run_type=run_type,
            configuration_fingerprint=configuration_fingerprint,
            checkpoint=checkpoint,
            task_id=task_id,
            settings=settings,
            lock=lock,
        )
    return _publish_review(prepared, settings=settings)


def _review_checkpoint(command_id: str, auth_digest: str) -> CheckpointDescriptor:
    return CheckpointDescriptor(
        phase="crm_stage_history_review_v1",
        cursor={"revision": 0},
        source_window={
            "review_command_id": command_id,
            "authorization_reference_digest": auth_digest,
        },
        last_committed_record_id=None,
        connector_version="bitrix-crm-stagehistory-review-v1",
        schema_version=1,
        replay_boundary="single_review_command",
    )


def _fail_claimed_attempt(
    client: Neo4jClient,
    claimed_attempt: tuple[str, str, int],
    *,
    error: Exception,
) -> None:
    logical_run_id, ingest_run_id, generation = claimed_attempt
    LogicalRunControl(client, LEGACY_DEFAULT_CONTROL_INSTANCE_ID).fail(
        logical_run_id=logical_run_id,
        ingest_run_id=ingest_run_id,
        generation=generation,
        failure_category="stage_history_review_publication_failed",
        safe_failure_message=type(error).__name__,
    )


def _queue_review_locked(
    command: StageHistoryReviewCommand,
    *,
    occurrence_id: str,
    authorization_reference: str,
    run_type: str,
    configuration_fingerprint: str,
    checkpoint: CheckpointDescriptor,
    task_id: str,
    settings: Settings,
    lock: StageHistoryTaskLock,
) -> _PreparedReview:
    client = Neo4jClient(settings)
    fence = None
    claimed_attempt: tuple[str, str, int] | None = None
    try:
        logical = LogicalRunControl(client, LEGACY_DEFAULT_CONTROL_INSTANCE_ID)
        lock.assert_owned()
        attempt = logical.create_or_reuse(
            source_key="bitrix_chat",
            control_instance_id=LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
            mode=run_type,
            dump_path=None,
            entity_key=None,
            idempotency_key=f"bitrix-stage-history:review:{command.command_id}",
            worker_task_id=task_id,
            configuration_fingerprint=configuration_fingerprint,
            connector_version=checkpoint.connector_version,
            checkpoint_schema_version=checkpoint.schema_version,
            initial_checkpoint=checkpoint,
        )
        if attempt.worker_task_id != task_id:
            raise RuntimeError("stage-history review command already exists or is running")
        if not logical.claim(
            logical_run_id=attempt.logical_run_id,
            ingest_run_id=attempt.ingest_run_id,
            generation=attempt.generation,
            worker_task_id=task_id,
        ):
            raise RuntimeError("stage-history review command already exists or is running")
        claimed_attempt = (
            attempt.logical_run_id,
            attempt.ingest_run_id,
            attempt.generation,
        )
        lock.assert_owned()
        admission = BitrixStreamControl(client).admit_or_coalesce(
            stream_key="crm_stage_history",
            logical_run_id=attempt.logical_run_id,
            ingest_run_id=attempt.ingest_run_id,
            attempt_generation=attempt.generation,
            worker_task_id=task_id,
            control_instance_id=LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
        )
        fence = admission.fence_context
        lock.assert_owned()
        StageHistoryReviewRepository(
            client,
            control_instance_id=LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
        ).record_command(
            command,
            occurrence_id=occurrence_id,
            authorization_reference=authorization_reference,
            fence=fence,
        )
        return _PreparedReview(
            task_id=task_id,
            logical_run_id=attempt.logical_run_id,
            command_id=command.command_id,
            review_kind=command.kind,
            authorization_reference=authorization_reference,
            fence=fence,
        )
    except Exception as exc:
        if fence is not None:
            lock.assert_owned()
            LogicalRunControl(client, fence.control_instance_id).fail_fenced(
                context=fence,
                failure_category="stage_history_review_publication_failed",
                safe_failure_message=type(exc).__name__,
            )
        elif claimed_attempt is not None:
            lock.assert_owned()
            _fail_claimed_attempt(client, claimed_attempt, error=exc)
        raise
    finally:
        client.close()


def _publish_review(
    prepared: _PreparedReview,
    *,
    settings: Settings,
) -> dict[str, JsonValue]:
    try:
        admit_configured_bitrix_control(settings, prepared.control_instance_id)
        kwargs: dict[str, str] = {}
        if prepared.control_instance_id != LEGACY_DEFAULT_CONTROL_INSTANCE_ID:
            kwargs["control_instance_id"] = prepared.control_instance_id
        execute_stage_history_review_task.apply_async(
            args=(prepared.command_id, prepared.authorization_reference),
            kwargs=kwargs,
            task_id=prepared.task_id,
            queue="ingestion",
        )
    except Exception as exc:
        if prepared.fence is not None:
            from src.bitrix_ingestion_models import FenceContext

            if not isinstance(prepared.fence, FenceContext):
                raise TypeError("stage-history review fence is invalid") from exc
            _fail_review_publication(
                settings,
                fence=prepared.fence,
                task_id=prepared.task_id,
                error=exc,
            )
        raise
    return {
        "status": "queued",
        "task_id": prepared.task_id,
        "logical_run_id": prepared.logical_run_id,
        "command_id": prepared.command_id,
        "review_kind": prepared.review_kind,
    }


def _fail_review_publication(
    settings: Settings,
    *,
    fence: object,
    task_id: str,
    error: Exception,
) -> None:
    from src.bitrix_ingestion_models import FenceContext

    if not isinstance(fence, FenceContext):
        raise TypeError("stage-history review fence is invalid")
    with stage_history_task_lock(
        settings.celery_broker_url,
        owner=f"review-publish-failure:{task_id}",
        control_instance_id=fence.control_instance_id,
    ) as lock:
        lock.assert_owned()
        client = Neo4jClient(settings)
        try:
            LogicalRunControl(client, fence.control_instance_id).fail_fenced(
                context=fence,
                failure_category="stage_history_review_publication_failed",
                safe_failure_message=type(error).__name__,
            )
        finally:
            client.close()


def _resume_review(
    command_id: str,
    authorization_reference: str,
    config: StageHistoryIngestionConfig,
) -> dict[str, JsonValue]:
    if authorization_reference != config.authorization_reference:
        raise PermissionError("stage-history review authorization changed")
    task_id = uuid.uuid4().hex
    settings = get_settings()
    admit_configured_bitrix_control(settings, LEGACY_DEFAULT_CONTROL_INSTANCE_ID)
    with stage_history_task_lock(
        settings.celery_broker_url,
        owner=f"review-resume:{task_id}",
        control_instance_id=LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
    ) as lock:
        client = Neo4jClient(settings)
        fence = None
        claimed_attempt: tuple[str, str, int] | None = None
        try:
            repository = StageHistoryReviewRepository(
                client,
                control_instance_id=LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
            )
            context = repository.load_resume_context(command_id)
            if context is None:
                raise ValueError("stage-history review command was not found")
            if context.authorization_reference != authorization_reference:
                raise PermissionError("stage-history review authorization changed")
            if context.command.reviewer_id != config.authorized_actor:
                raise PermissionError("stage-history review actor changed")
            configuration_fingerprint = stage_history_review_configuration_fingerprint(
                command_id,
                context.command.kind,
                authorization_reference,
                review_lease_seconds=config.review_lease_seconds,
                retry_backoff_seconds=config.retry_backoff_seconds,
            )
            if context.configuration_fingerprint != configuration_fingerprint:
                raise PermissionError("stage-history review configuration changed")
            if context.logical_status in {"completed", "completed_with_errors"}:
                return {
                    "status": context.logical_status,
                    "logical_run_id": context.logical_run_id,
                    "command_id": command_id,
                }
            logical = LogicalRunControl(client, LEGACY_DEFAULT_CONTROL_INSTANCE_ID)
            if context.logical_status in {"failed", "paused_with_checkpoint"}:
                attempt = logical.resume(
                    logical_run_id=context.logical_run_id,
                    worker_task_id=task_id,
                    configuration_fingerprint=configuration_fingerprint,
                    logical_connector_version="bitrix-crm-stagehistory-review-v1",
                    checkpoint_connector_version="bitrix-crm-stagehistory-review-v1",
                    checkpoint_schema_version=1,
                )
                if attempt is None or not logical.claim(
                    logical_run_id=context.logical_run_id,
                    ingest_run_id=attempt.ingest_run_id,
                    generation=attempt.generation,
                    worker_task_id=task_id,
                ):
                    raise RuntimeError("stage-history review could not resume")
                claimed_attempt = (
                    context.logical_run_id,
                    attempt.ingest_run_id,
                    attempt.generation,
                )
                lock.assert_owned()
                admission = BitrixStreamControl(client).admit_or_coalesce(
                    stream_key="crm_stage_history",
                    logical_run_id=context.logical_run_id,
                    ingest_run_id=attempt.ingest_run_id,
                    attempt_generation=attempt.generation,
                    worker_task_id=task_id,
                    control_instance_id=LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
                    replace_active=True,
                )
                fence = admission.fence_context
            elif context.logical_status in {"queued", "running"}:
                if context.worker_task_id is None:
                    raise RuntimeError("active stage-history review lost its worker identity")
                task_id = context.worker_task_id
            else:
                raise RuntimeError("stage-history review is not resumable")
            prepared = _PreparedReview(
                task_id=task_id,
                logical_run_id=context.logical_run_id,
                command_id=command_id,
                review_kind=context.command.kind,
                authorization_reference=authorization_reference,
                fence=fence,
            )
        except Exception as exc:
            if fence is not None:
                lock.assert_owned()
                LogicalRunControl(client, LEGACY_DEFAULT_CONTROL_INSTANCE_ID).fail_fenced(
                    context=fence,
                    failure_category="stage_history_review_publication_failed",
                    safe_failure_message=type(exc).__name__,
                )
            elif claimed_attempt is not None:
                lock.assert_owned()
                _fail_claimed_attempt(client, claimed_attempt, error=exc)
            raise
        finally:
            client.close()
    return _publish_review(prepared, settings=settings)
