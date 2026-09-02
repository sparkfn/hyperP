"""Request-scoped safe timing context for HTTP and repository observability."""

from __future__ import annotations

from contextvars import ContextVar, Token
from time import monotonic

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_repository_duration_ms: ContextVar[float] = ContextVar("repository_duration_ms", default=0.0)


def begin_request(request_id: str) -> tuple[Token[str | None], Token[float]]:
    """Initialize request-local values and return reset tokens."""
    return _request_id.set(request_id), _repository_duration_ms.set(0.0)


def end_request(tokens: tuple[Token[str | None], Token[float]]) -> None:
    """Reset request-local values after the response is complete."""
    request_token, repository_token = tokens
    _request_id.reset(request_token)
    _repository_duration_ms.reset(repository_token)


def current_request_id() -> str | None:
    """Return the trusted request ID assigned by middleware, if any."""
    return _request_id.get()


def record_repository_duration(started_at: float) -> None:
    """Accumulate a completed repository query duration without query data."""
    elapsed_ms = (monotonic() - started_at) * 1000
    _repository_duration_ms.set(_repository_duration_ms.get() + elapsed_ms)


def repository_duration_ms() -> float:
    """Return aggregate repository execution time for the current request."""
    return _repository_duration_ms.get()
