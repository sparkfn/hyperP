"""Default-off Celery entry points for bounded CRM stage-history execution."""

from __future__ import annotations

from celery import Task

from src.celery_app import celery_app
from src.stage_history_review_task_runtime import (
    StageHistoryReviewTaskSummary,
    execute_review_task,
)
from src.stage_history_task_runtime import StageHistoryTaskSummary, execute_artifact_task


@celery_app.task(
    bind=True,
    name="src.stage_history_tasks.replay_stage_history_artifact_task",
)
def replay_stage_history_artifact_task(
    self: Task,
    artifact_id: str,
    authorization_reference: str,
    control_instance_id: str = "legacy-default",
) -> StageHistoryTaskSummary:
    return execute_artifact_task(
        self,
        artifact_id=artifact_id,
        authorization_reference=authorization_reference,
        failed_capture=False,
        control_instance_id=control_instance_id,
    )


@celery_app.task(
    bind=True,
    name="src.stage_history_tasks.record_stage_history_capture_failure_task",
)
def record_stage_history_capture_failure_task(
    self: Task,
    artifact_id: str,
    authorization_reference: str,
    control_instance_id: str = "legacy-default",
) -> StageHistoryTaskSummary:
    return execute_artifact_task(
        self,
        artifact_id=artifact_id,
        authorization_reference=authorization_reference,
        failed_capture=True,
        control_instance_id=control_instance_id,
    )


@celery_app.task(
    bind=True,
    name="src.stage_history_tasks.execute_stage_history_review_task",
)
def execute_stage_history_review_task(
    self: Task,
    command_id: str,
    authorization_reference: str,
    control_instance_id: str = "legacy-default",
) -> StageHistoryReviewTaskSummary:
    return execute_review_task(
        self,
        command_id=command_id,
        authorization_reference=authorization_reference,
        control_instance_id=control_instance_id,
    )
