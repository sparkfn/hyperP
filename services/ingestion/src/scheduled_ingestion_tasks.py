"""Idempotent weekly API-ingestion chain dispatch."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TypedDict

import redis
from celery import Task, chain
from celery.canvas import Signature

from src.celery_app import INGESTION_QUEUE, celery_app
from src.config import get_settings
from src.ingestion_config import get_ingestion_config
from src.scheduled_ingestion_groups import scheduled_ingestion_group

logger = logging.getLogger(__name__)

_MARKER_PREFIX = "profile_unifier:scheduled-ingestion"
_MARKER_TTL_SECONDS = 60 * 60 * 24 * 8


class ScheduledGroupDispatchSummary(TypedDict):
    """A group-chain publication result."""

    status: str
    group_key: str
    incremental: bool
    workflow_task_id: str


def _utc_occurrence_date() -> str:
    return datetime.now(UTC).date().isoformat()


def _marker_key(group_key: str, incremental: bool, task_id: str) -> str:
    """Return a stable idempotency key for cron or manual dispatch."""
    if incremental:
        occurrence = _utc_occurrence_date()
    else:
        occurrence = task_id
    policy = "incremental" if incremental else "full"
    return f"{_MARKER_PREFIX}:{group_key}:{policy}:{occurrence}"


def _claim_dispatch(marker_key: str, task_id: str) -> tuple[bool, str | None]:
    """Claim a dispatch key or resume a redelivery owned by this task."""
    with redis.Redis.from_url(get_settings().celery_broker_url, decode_responses=True) as client:
        while True:
            if client.set(marker_key, task_id, nx=True, ex=_MARKER_TTL_SECONDS):
                return True, None
            prior = client.get(marker_key)
            if prior is not None:
                break
    if prior == task_id:
        # The worker may have exited after reserving but before recording the
        # workflow ID. Re-publishing is safe because every chain step has its
        # own logical-run completion marker.
        return True, None
    return False, prior


def _release_claim(marker_key: str, task_id: str) -> None:
    """Release a failed publication only when this task owns the claim."""
    script = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""
    with redis.Redis.from_url(get_settings().celery_broker_url, decode_responses=True) as client:
        client.eval(script, 1, marker_key, task_id)


def _signature(
    source_key: str,
    entity_key: str | None,
    incremental: bool,
    idempotency_key: str,
) -> Signature:
    """Build an immutable chain step that cannot consume a prior result."""
    return celery_app.signature(
        "src.tasks.run_ingestion_task",
        args=(source_key, "api"),
        kwargs={
            "entity_key": entity_key,
            "incremental": incremental,
            "wait_for_source": True,
            "require_clean_completion": True,
            "idempotency_key": idempotency_key,
        },
        immutable=True,
        queue=INGESTION_QUEUE,
    )


@celery_app.task(  # type: ignore[untyped-decorator]
    name="src.scheduled_ingestion_tasks.dispatch_ingestion_group_task",
    bind=True,
    max_retries=0,
)
def dispatch_ingestion_group_task(
    self: Task,
    group_key: str,
    incremental: bool = False,
) -> ScheduledGroupDispatchSummary:
    """Publish one ordered API chain, with cron runs deduplicated by UTC day."""
    if not get_ingestion_config().scheduled_ingestion.enabled:
        logger.info(
            "Skipped scheduled ingestion group=%s incremental=%s because scheduling is disabled",
            group_key,
            incremental,
        )
        return {
            "status": "disabled",
            "group_key": group_key,
            "incremental": incremental,
            "workflow_task_id": "",
        }
    group = scheduled_ingestion_group(group_key)
    task_id = str(self.request.id or "manual")
    marker_key = _marker_key(group.key, incremental, task_id)
    claimed, existing = _claim_dispatch(marker_key, task_id)
    if not claimed:
        return {
            "status": "already_queued",
            "group_key": group.key,
            "incremental": incremental,
            "workflow_task_id": existing or task_id,
        }
    try:
        workflow = chain(
            *(
                _signature(
                    spec.source_key,
                    spec.entity_key,
                    incremental,
                    f"{marker_key}:step:{index}",
                )
                for index, spec in enumerate(group.tasks)
            )
        )
        result = workflow.apply_async(queue=INGESTION_QUEUE)
    except Exception:
        _release_claim(marker_key, task_id)
        raise
    workflow_task_id = str(result.id)
    # Replace the reservation with the workflow ID while retaining the same TTL.
    with redis.Redis.from_url(get_settings().celery_broker_url, decode_responses=True) as client:
        client.set(marker_key, workflow_task_id, xx=True, ex=_MARKER_TTL_SECONDS)
    logger.info(
        "Queued scheduled ingestion group=%s incremental=%s workflow=%s",
        group.key,
        incremental,
        workflow_task_id,
    )
    return {
        "status": "queued",
        "group_key": group.key,
        "incremental": incremental,
        "workflow_task_id": workflow_task_id,
    }
