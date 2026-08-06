"""Per-phase single-flight coordination for deferred KNOWS materialization."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TypeGuard, cast

import redis
from celery import Task
from celery.result import AsyncResult

from src.config import get_settings
from src.pipeline_knows import KnowsMaterializationPhase

logger = logging.getLogger(__name__)

_GATE_PREFIX = "profile_unifier:knows-materialization"
_RETRY_PUBLICATION: ContextVar[bool] = ContextVar("knows_retry_publication", default=False)
_RELEASE_SCRIPT = """
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
local now = tonumber(redis.call('time')[1])
local current = redis.call('get', KEYS[1])
if current then
    local state, timestamp, owner = string.match(current, '^([^|]+)|([^|]+)|(.+)$')
    return {0, owner or current}
end
local marker = 'publishing|' .. now .. '|' .. ARGV[1]
redis.call('set', KEYS[1], marker)
return {1, ARGV[1], marker}
"""
_MARK_QUEUED_SCRIPT = """
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
_CLAIM_SCRIPT = """
local current = redis.call('get', KEYS[1])
if not current then
    return 0
end
local state, timestamp, owner = string.match(current, '^([^|]+)|([^|]+)|(.+)$')
if current == ARGV[1] or owner == ARGV[1] then
    redis.call('set', KEYS[1], ARGV[1])
    return 1
end
if ARGV[2] ~= '' and current == ARGV[2] then
    redis.call('set', KEYS[1], ARGV[1])
    return 1
end
return 0
"""
_TRANSFER_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    local now = tonumber(redis.call('time')[1])
    redis.call('set', KEYS[1], 'publishing|' .. now .. '|' .. ARGV[2])
    return 1
end
return 0
"""


def _redis_client() -> redis.Redis:
    return redis.Redis.from_url(get_settings().celery_broker_url)


def _gate_key(phase: KnowsMaterializationPhase) -> str:
    return f"{_GATE_PREFIX}:{phase}:queued"


def _decode(value: object) -> str | None:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value if isinstance(value, str) else None


def _owner_from_value(value: str | None) -> str | None:
    if value is None:
        return None
    parts = value.split("|", 2)
    return parts[2] if len(parts) == 3 and parts[0] == "publishing" else value


def _is_phase(value: object) -> TypeGuard[KnowsMaterializationPhase]:
    return value in {"contacts", "chat_relationships"}


def _phase_from_call(
    args: tuple[object, ...] | None,
    kwargs: dict[str, object] | None,
) -> KnowsMaterializationPhase | None:
    value = args[0] if args else kwargs.get("phase") if kwargs is not None else None
    return value if _is_phase(value) else None


def gate_owner(phase: KnowsMaterializationPhase) -> str | None:
    """Return the current phase-gate owner, if any."""
    return _owner_from_value(_decode(_redis_client().get(_gate_key(phase))))


def release_knows_materialization_queue_gate(
    phase: KnowsMaterializationPhase, task_id: str
) -> bool:
    """Strictly release a phase gate only if it still belongs to ``task_id``."""
    return (
        cast(
            int,
            _redis_client().eval(_RELEASE_SCRIPT, 1, _gate_key(phase), task_id),
        )
        == 1
    )


def claim_knows_materialization_gate(
    phase: KnowsMaterializationPhase,
    task_id: str,
    predecessor_task_id: str | None,
) -> bool:
    """Validate ownership or atomically claim a published continuation."""
    return (
        cast(
            int,
            _redis_client().eval(
                _CLAIM_SCRIPT,
                1,
                _gate_key(phase),
                task_id,
                predecessor_task_id or "",
            ),
        )
        == 1
    )


def transfer_knows_materialization_gate(
    phase: KnowsMaterializationPhase,
    current_task_id: str,
    next_task_id: str,
) -> bool:
    """Reserve publication ownership for a continuation before broker publish."""
    return (
        cast(
            int,
            _redis_client().eval(
                _TRANSFER_SCRIPT,
                1,
                _gate_key(phase),
                current_task_id,
                next_task_id,
            ),
        )
        == 1
    )


def mark_knows_materialization_queued(
    phase: KnowsMaterializationPhase,
    task_id: str,
) -> bool:
    """Mark an accepted publication as queued without overwriting another owner."""
    return (
        cast(
            int,
            _redis_client().eval(
                _MARK_QUEUED_SCRIPT,
                1,
                _gate_key(phase),
                task_id,
            ),
        )
        == 1
    )


@contextmanager
def allow_knows_retry_publication() -> Iterator[None]:
    """Allow only an in-process Celery retry to republish its existing task ID."""
    token = _RETRY_PUBLICATION.set(True)
    try:
        yield
    finally:
        _RETRY_PUBLICATION.reset(token)


class KnowsMaterializationTask(Task):  # type: ignore[misc]
    """Publish at most one pending or running cursor chain for each phase."""

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
        phase = _phase_from_call(args, kwargs)
        if phase is None:
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
        queued_task_id = task_id or uuid.uuid4().hex
        if _RETRY_PUBLICATION.get():
            if task_id is None:
                raise RuntimeError("KNOWS retry publication requires the existing task ID")
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
            _gate_key(phase),
            queued_task_id,
        )
        if not isinstance(claimed_raw, (list, tuple)) or len(claimed_raw) not in {2, 3}:
            raise RuntimeError("Invalid Redis response while claiming KNOWS publication gate")
        claimed = int(claimed_raw[0]) == 1
        owner = _decode(claimed_raw[1])
        if not claimed:
            if owner is None:
                raise RuntimeError("KNOWS publication gate returned no owner")
            logger.info("KNOWS phase=%s already pending or running", phase)
            return self.AsyncResult(owner)
        publishing_marker = _decode(claimed_raw[2]) if len(claimed_raw) == 3 else None
        if publishing_marker is None:
            raise RuntimeError("KNOWS publication gate returned no publishing marker")
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
                "KNOWS publication outcome is ambiguous; retaining publishing marker phase=%s",
                phase,
            )
            raise
        if not mark_knows_materialization_queued(phase, queued_task_id):
            logger.warning(
                "KNOWS publication accepted after gate ownership changed phase=%s", phase
            )
        return result
