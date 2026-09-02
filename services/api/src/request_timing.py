"""Request-scoped safe timing context for HTTP and repository observability."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from contextvars import Context, ContextVar, Token
from dataclasses import dataclass
from time import monotonic


@dataclass
class RequestTiming:
    """Mutable timing state shared by tasks created for one HTTP request."""

    request_id: str
    repository_duration_ms: float = 0.0


_request_timing: ContextVar[RequestTiming | None] = ContextVar("request_timing", default=None)


def begin_request(request_id: str) -> Token[RequestTiming | None]:
    """Initialize request-local values and return reset tokens."""
    return _request_timing.set(RequestTiming(request_id=request_id))


def end_request(token: Token[RequestTiming | None]) -> None:
    """Reset request-local values after the response is complete."""
    _request_timing.reset(token)


def current_request_id() -> str | None:
    """Return the trusted request ID assigned by middleware, if any."""
    timing = _request_timing.get()
    return timing.request_id if timing is not None else None


def record_repository_duration(started_at: float) -> None:
    """Accumulate a completed repository query duration without query data."""
    elapsed_ms = (monotonic() - started_at) * 1000
    timing = _request_timing.get()
    if timing is not None:
        timing.repository_duration_ms += elapsed_ms


def repository_duration_ms() -> float:
    """Return aggregate repository execution time for the current request."""
    timing = _request_timing.get()
    return timing.repository_duration_ms if timing is not None else 0.0


def create_detached_task[T](coro: Coroutine[object, object, T]) -> asyncio.Task[T]:
    """Create a background task without inheriting completed request context."""
    clean_context = Context()
    return clean_context.run(asyncio.create_task, coro)
