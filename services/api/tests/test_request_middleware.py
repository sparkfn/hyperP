"""Integration coverage for request timing headers and disconnect cancellation."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from neo4j import AsyncSession
from src.graph.client import TimedAsyncSession
from src.request_middleware import RequestTimingMiddleware
from starlette.types import Message, Receive, Scope, Send


class _DelayedSession:
    async def __aenter__(self) -> _DelayedSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        await asyncio.sleep(0.01)

    async def run(self, *_args: object, **_kwargs: object) -> _DelayedResult:
        return _DelayedResult()


class _DelayedResult:
    async def consume(self) -> None:
        await asyncio.sleep(0.01)


def _scope(headers: list[tuple[bytes, bytes]], method: str = "GET") -> Scope:
    return cast(
        Scope,
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("test", 1),
            "server": ("test", 80),
        },
    )


@pytest.mark.anyio
async def test_timing_headers_include_session_lifetime_and_request_id() -> None:
    app = FastAPI()
    app.add_middleware(RequestTimingMiddleware)

    @app.get("/timed")
    async def timed() -> dict[str, bool]:
        async with TimedAsyncSession(cast(AsyncSession, _DelayedSession()), write=False) as session:
            result = await session.run("RETURN 1")
            await cast(_DelayedResult, result).consume()
        return {"ok": True}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/timed", headers={"x-request-id": "request-timing-test"})

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "request-timing-test"
    assert float(response.headers["x-api-duration-ms"]) >= 10
    assert float(response.headers["x-repository-duration-ms"]) >= 10


@pytest.mark.anyio
async def test_unhandled_route_error_returns_safe_envelope_and_timing_headers() -> None:
    app = FastAPI()
    app.add_middleware(RequestTimingMiddleware)

    @app.get("/raises")
    async def raises() -> None:
        raise RuntimeError("unexpected database detail")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/raises", headers={"x-request-id": "request-error-test"})

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "An internal error occurred.",
            "details": None,
        },
        "meta": {"request_id": "request-error-test", "next_cursor": None, "total_count": None},
    }
    assert response.headers["x-request-id"] == "request-error-test"
    assert float(response.headers["x-api-duration-ms"]) >= 0
    assert float(response.headers["x-repository-duration-ms"]) >= 0


@pytest.mark.parametrize(
    "value",
    [b"bad\x00id", b"caf\xc3\xa9", b"x" * 129],
)
def test_request_id_replaces_unsafe_header_values(value: bytes) -> None:
    request_id = RequestTimingMiddleware._request_id(_scope([(b"x-request-id", value)]))

    assert request_id != value.decode("latin-1")
    assert len(request_id) == 36


def test_request_id_preserves_valid_printable_ascii_token() -> None:
    assert (
        RequestTimingMiddleware._request_id(_scope([(b"x-request-id", b"gateway-req_123:abc")]))
        == "gateway-req_123:abc"
    )


@pytest.mark.anyio
async def test_disconnect_cancels_downstream_handler_promptly() -> None:
    cancelled = asyncio.Event()
    started = asyncio.Event()
    initial_request_consumed = asyncio.Event()

    async def downstream(_scope: Scope, _receive: Receive, _send: Send) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    middleware = RequestTimingMiddleware(downstream)
    messages: asyncio.Queue[Message] = asyncio.Queue()
    await messages.put({"type": "http.request", "body": b"", "more_body": False})

    async def receive() -> Message:
        message = await messages.get()
        if message["type"] == "http.request":
            initial_request_consumed.set()
        return message

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    task = asyncio.create_task(
        middleware(
            _scope([]),
            receive,
            send,
        )
    )
    await started.wait()
    await asyncio.wait_for(initial_request_consumed.wait(), timeout=1)
    await messages.put({"type": "http.disconnect"})
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert sent == []


@pytest.mark.anyio
async def test_body_bearing_request_is_not_read_ahead() -> None:
    calls = 0
    allow_application_read = asyncio.Event()
    first_read = asyncio.Event()
    release = asyncio.Event()

    async def receive() -> Message:
        nonlocal calls
        calls += 1
        first_read.set()
        await release.wait()
        return {"type": "http.request", "body": b"payload", "more_body": False}

    async def downstream(_scope: Scope, receive_fn: Receive, send: Send) -> None:
        await allow_application_read.wait()
        message = await receive_fn()
        assert message["type"] == "http.request"
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RequestTimingMiddleware(downstream)
    task = asyncio.create_task(
        middleware(_scope([], "POST"), receive, lambda _message: asyncio.sleep(0))
    )
    await asyncio.sleep(0)
    assert calls == 0
    allow_application_read.set()
    await asyncio.wait_for(first_read.wait(), timeout=1)
    assert calls == 1
    release.set()
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers",
    [[(b"content-length", b"1")], [(b"transfer-encoding", b"chunked")]],
)
async def test_declared_body_get_is_not_read_ahead(headers: list[tuple[bytes, bytes]]) -> None:
    calls = 0
    allow_application_read = asyncio.Event()
    first_read = asyncio.Event()
    release = asyncio.Event()

    async def receive() -> Message:
        nonlocal calls
        calls += 1
        first_read.set()
        await release.wait()
        return {"type": "http.request", "body": b"payload", "more_body": False}

    async def downstream(_scope: Scope, receive_fn: Receive, send: Send) -> None:
        await allow_application_read.wait()
        message = await receive_fn()
        assert message["type"] == "http.request"
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RequestTimingMiddleware(downstream)
    task = asyncio.create_task(
        middleware(_scope(headers), receive, lambda _message: asyncio.sleep(0))
    )
    await asyncio.sleep(0)
    assert calls == 0
    allow_application_read.set()
    await asyncio.wait_for(first_read.wait(), timeout=1)
    assert calls == 1
    release.set()
    await asyncio.wait_for(task, timeout=1)
