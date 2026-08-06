"""Queue-level single-flight coordination for lifecycle reconciliation."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import cast

import redis
from celery import Task
from celery.result import AsyncResult

from src.config import get_settings

logger = logging.getLogger(__name__)

_LIFECYCLE_RECONCILIATION_QUEUE_KEY = "profile_unifier:lifecycle-reconciliation:queued"
_RETRY_PUBLICATION: ContextVar[bool] = ContextVar("lifecycle_retry_publication", default=False)
_RELEASE_QUEUE_GATE_SCRIPT = """
local current = redis.call('get', KEYS[1])
if not current then
    return 0
end
local state, timestamp, owner = string.match(current, '^([^|]+)|([^|]+)|(.+)$')
if current == ARGV[1] or owner == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""
_ROOT_CLAIM_SCRIPT = """
local current = redis.call('get', KEYS[1])
if current then
    local state, timestamp, owner = string.match(current, '^([^|]+)|([^|]+)|(.+)$')
    return {0, owner or current}
end
local now = tonumber(redis.call('time')[1])
local marker = 'publishing|' .. now .. '|' .. ARGV[1]
redis.call('set', KEYS[1], marker)
return {1, ARGV[1], marker}
"""
_CLAIM_QUEUED_SCRIPT = """
local current = redis.call('get', KEYS[1])
if not current then
    return 0
end
local state, timestamp, owner = string.match(current, '^([^|]+)|([^|]+)|(.+)$')
if current == ARGV[1] or owner == ARGV[1] then
    redis.call('set', KEYS[1], ARGV[1])
    return 1
end
return 0
"""


def _redis_client() -> redis.Redis:
    return redis.Redis.from_url(get_settings().celery_broker_url)


def _decode(value: object) -> str | None:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value if isinstance(value, str) else None


def _owner_from_value(value: str | None) -> str | None:
    if value is None:
        return None
    parts = value.split("|", 2)
    return parts[2] if len(parts) == 3 and parts[0] == "publishing" else value


def release_lifecycle_reconciliation_queue_gate(task_id: str) -> bool:
    """Strictly release the queue gate only when it still belongs to this task."""
    return (
        cast(
            int,
            _redis_client().eval(
                _RELEASE_QUEUE_GATE_SCRIPT,
                1,
                _LIFECYCLE_RECONCILIATION_QUEUE_KEY,
                task_id,
            ),
        )
        == 1
    )


def claim_lifecycle_reconciliation_queue_gate(task_id: str) -> bool:
    """Claim an accepted publishing marker or validate queued ownership."""
    return (
        cast(
            int,
            _redis_client().eval(
                _CLAIM_QUEUED_SCRIPT,
                1,
                _LIFECYCLE_RECONCILIATION_QUEUE_KEY,
                task_id,
            ),
        )
        == 1
    )


def _queued_task_id(client: redis.Redis) -> str | None:
    return _owner_from_value(_decode(client.get(_LIFECYCLE_RECONCILIATION_QUEUE_KEY)))


@contextmanager
def allow_lifecycle_retry_publication() -> Iterator[None]:
    """Allow only an in-process Celery retry to republish its existing task ID."""
    token = _RETRY_PUBLICATION.set(True)
    try:
        yield
    finally:
        _RETRY_PUBLICATION.reset(token)


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
        if _RETRY_PUBLICATION.get():
            if task_id is None:
                raise RuntimeError("Lifecycle retry publication requires the existing task ID")
            return cast(
                AsyncResult,
                super().apply_async(
                    args=args,
                    kwargs=kwargs,
                    task_id=task_id,
                    producer=producer,
                    link=link,
                    link_error=link_error,
                    shadow=shadow,
                    **options,
                ),
            )

        client = _redis_client()
        claimed_raw = client.eval(
            _ROOT_CLAIM_SCRIPT,
            1,
            _LIFECYCLE_RECONCILIATION_QUEUE_KEY,
            queued_task_id,
        )
        if not isinstance(claimed_raw, (list, tuple)) or len(claimed_raw) not in {2, 3}:
            raise RuntimeError("Invalid Redis response while claiming reconciliation gate")
        claimed = int(claimed_raw[0]) == 1
        owner = _decode(claimed_raw[1])
        if not claimed:
            if owner is None:
                raise RuntimeError("Lifecycle reconciliation gate returned no owner")
            logger.info(
                "Lifecycle reconciliation task %s is already pending or running; "
                "skipping duplicate publish",
                owner,
            )
            return self.AsyncResult(owner)
        publishing_marker = _decode(claimed_raw[2]) if len(claimed_raw) == 3 else None
        if publishing_marker is None:
            raise RuntimeError("Lifecycle reconciliation gate returned no publishing marker")
        try:
            result = cast(
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
            logger.exception(
                "Reconciliation publication outcome is ambiguous; retaining publishing marker"
            )
            raise
        if not claim_lifecycle_reconciliation_queue_gate(queued_task_id):
            logger.warning("Reconciliation publication accepted after gate ownership changed")
        return result
