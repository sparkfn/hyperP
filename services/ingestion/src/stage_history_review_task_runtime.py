"""Worker execution for durable stage-history review commands."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TypedDict

from celery import Task

from src.config import Settings, get_settings
from src.graph.client import Neo4jClient
from src.graph.ingestion_control import LogicalRunControl
from src.graph.stage_history_review import StageHistoryReviewRepository
from src.ingestion_config import StageHistoryIngestionConfig, get_ingestion_config
from src.stage_history_ingestion_models import (
    stage_history_review_configuration_fingerprint,
)
from src.stage_history_task_lock import StageHistoryTaskLock, stage_history_task_lock


class StageHistoryReviewTaskSummary(TypedDict):
    status: str
    logical_run_id: str
    command_id: str
    authority_state: str
    invalidation_count: int


def execute_review_task(
    task: Task,
    *,
    command_id: str,
    authorization_reference: str,
) -> StageHistoryReviewTaskSummary:
    config = get_ingestion_config().stage_history_ingestion
    config.assert_dispatch_enabled(now=datetime.now(UTC))
    if not command_id.strip() or authorization_reference != config.authorization_reference:
        raise PermissionError("stage-history review authorization changed")
    task_id = str(task.request.id or "").strip()
    if not task_id:
        raise RuntimeError("stage-history review requires a stable Celery task ID")
    settings = get_settings()
    with stage_history_task_lock(
        settings.celery_broker_url,
        owner=f"review:{task_id}",
    ) as lock:
        lock.assert_owned()
        return _run_review_task(
            settings=settings,
            lock=lock,
            task_id=task_id,
            command_id=command_id,
            authorization_reference=authorization_reference,
            config=config,
        )


def _run_review_task(
    *,
    settings: Settings,
    lock: StageHistoryTaskLock,
    task_id: str,
    command_id: str,
    authorization_reference: str,
    config: StageHistoryIngestionConfig,
) -> StageHistoryReviewTaskSummary:
    client = Neo4jClient(settings)
    execution = None
    domain_committed = False
    try:
        repository = StageHistoryReviewRepository(client)
        loaded_execution = repository.load_execution(command_id)
        if loaded_execution is None:
            raise ValueError("stage-history review command was not found")
        expected_configuration_fingerprint = stage_history_review_configuration_fingerprint(
            command_id,
            loaded_execution.command.kind,
            authorization_reference,
            review_lease_seconds=config.review_lease_seconds,
            retry_backoff_seconds=config.retry_backoff_seconds,
        )
        if (
            loaded_execution.worker_task_id != task_id
            or loaded_execution.authorization_reference != authorization_reference
            or loaded_execution.command.reviewer_id != config.authorized_actor
            or loaded_execution.configuration_fingerprint != expected_configuration_fingerprint
        ):
            raise PermissionError("stage-history review execution identity changed")
        execution = loaded_execution
        lock.assert_owned()
        result = repository.execute_command(
            execution.command,
            occurrence_id=execution.occurrence_id,
            authorization_reference=execution.authorization_reference,
            lease_owner=task_id,
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=config.review_lease_seconds),
            retry_backoff_seconds=config.retry_backoff_seconds,
            fence=execution.fence,
        )
        domain_committed = True
        lock.assert_owned()
        LogicalRunControl(client).finalize_fenced(
            context=execution.fence,
            phase="crm_stage_history_review_v1",
            status="completed",
            committed_count=0,
            duplicate_count=0,
            excluded_count=0,
            retry_count=0,
            record_count=0,
            rejected_count=0,
        )
        return {
            "status": "completed",
            "logical_run_id": execution.fence.logical_run_id,
            "command_id": command_id,
            "authority_state": result.authority_state,
            "invalidation_count": result.invalidation_count,
        }
    except Exception as exc:
        if execution is not None and not domain_committed:
            lock.assert_owned()
            LogicalRunControl(client).fail_fenced(
                context=execution.fence,
                failure_category="stage_history_review_failed",
                safe_failure_message=type(exc).__name__,
            )
        raise
    finally:
        client.close()
