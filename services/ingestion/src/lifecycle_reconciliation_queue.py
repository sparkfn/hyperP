"""Queue-level single-flight coordination for lifecycle reconciliation."""

from __future__ import annotations

import logging
import uuid
from typing import cast

import redis
from celery import Task
from celery.result import AsyncResult

from src.config import get_settings

logger = logging.getLogger(__name__)

_LIFECYCLE_RECONCILIATION_QUEUE_KEY = "profile_unifier:lifecycle-reconciliation:queued"
_RELEASE_QUEUE_GATE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def _redis_client() -> redis.Redis:
    return redis.Redis.from_url(get_settings().celery_broker_url)


def release_lifecycle_reconciliation_queue_gate(task_id: str) -> bool:
    """Release the queue gate only when it still belongs to this task."""
    client = _redis_client()
    try:
        return (
            cast(
                int,
                client.eval(
                    _RELEASE_QUEUE_GATE_SCRIPT,
                    1,
                    _LIFECYCLE_RECONCILIATION_QUEUE_KEY,
                    task_id,
                ),
            )
            == 1
        )
    except Exception:
        logger.exception(
            "Failed to release lifecycle reconciliation queue gate for task %s",
            task_id,
        )
        return False


def _queued_task_id(client: redis.Redis) -> str | None:
    task_id = client.get(_LIFECYCLE_RECONCILIATION_QUEUE_KEY)
    if isinstance(task_id, bytes):
        return task_id.decode("utf-8")
    return task_id if isinstance(task_id, str) else None


class LifecycleReconciliationTask(Task):  # type: ignore[misc]
    """Publish at most one pending or running lifecycle reconciliation task."""

    def apply_async(
        self,
        args: tuple[object, ...] | None = None,
        kwargs: dict[str, object] | None = None,
        task_id: str | None = None,
        producer: object | None = None,
        link: object | None = None,
        link_error: object | None = None,
        shadow: str | None = None,
        **options: object,
    ) -> AsyncResult:
        queued_task_id = task_id or uuid.uuid4().hex
        client = _redis_client()

        # Do not expire this marker: the task may remain pending behind a
        # long-running ingestion for longer than the broker visibility timeout.
        # Publish failures and task completion both release it by owner ID.
        while not client.set(
            _LIFECYCLE_RECONCILIATION_QUEUE_KEY,
            queued_task_id,
            nx=True,
        ):
            existing_task_id = _queued_task_id(client)
            if existing_task_id is not None:
                logger.info(
                    "Lifecycle reconciliation task %s is already pending or running; "
                    "skipping duplicate publish",
                    existing_task_id,
                )
                return self.AsyncResult(existing_task_id)

        try:
            return cast(
                AsyncResult,
                super().apply_async(
                    args=args,
                    kwargs=kwargs,
                    task_id=queued_task_id,
                    producer=producer,
                    link=link,
                    link_error=link_error,
                    shadow=shadow,
                    **options,
                ),
            )
        except Exception:
            release_lifecycle_reconciliation_queue_gate(queued_task_id)
            raise
