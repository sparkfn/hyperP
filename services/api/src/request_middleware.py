"""Pure ASGI request timing and disconnect cancellation middleware."""

from __future__ import annotations

import asyncio
import logging
from time import monotonic
from typing import cast
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.request_timing import begin_request, end_request, repository_duration_ms
from src.types import ApiError, ApiErrorBody, ResponseMeta

logger = logging.getLogger("profile_unifier_api")


class RequestTimingMiddleware:
    """Add safe timing headers and cancel request work on client disconnect."""

    # Request IDs are safe correlation tokens, never user input echoed verbatim.
    # Accept a compact visible-ASCII token only; 128 bytes prevents header abuse.
    _MAX_REQUEST_ID_BYTES = 128

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = self._request_id(scope)
        token = begin_request(request_id)
        started_at = monotonic()
        messages: asyncio.Queue[Message] = asyncio.Queue()
        app_task: asyncio.Future[None] | None = None
        response_started = False

        async def queued_receive() -> Message:
            return await messages.get()

        async def send_with_timing(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(self._timing_headers(request_id, started_at))
                message = {**message, "headers": headers}
                response_started = True
            await send(message)

        async def pump_disconnect() -> None:
            while True:
                message = await receive()
                await messages.put(message)
                if message["type"] == "http.disconnect" and app_task is not None:
                    app_task.cancel()
                    return

        try:
            app_task = asyncio.ensure_future(self.app(scope, queued_receive, send_with_timing))
            pump_task = asyncio.create_task(pump_disconnect())
            try:
                await app_task
            except Exception:
                logger.exception("Unhandled error")
                if response_started:
                    raise
                await self._send_internal_error(send_with_timing, request_id)
            finally:
                pump_task.cancel()
                await asyncio.gather(pump_task, return_exceptions=True)
        finally:
            end_request(token)

    @staticmethod
    def _request_id(scope: Scope) -> str:
        headers = cast(list[tuple[bytes, bytes]], scope.get("headers", []))
        for name, value in headers:
            if name.lower() == b"x-request-id":
                if RequestTimingMiddleware._is_safe_request_id(value):
                    return value.decode("ascii")
                return str(uuid4())
        return str(uuid4())

    @classmethod
    def _is_safe_request_id(cls, value: bytes) -> bool:
        """Accept only bounded printable-ASCII request correlation tokens."""
        return 0 < len(value) <= cls._MAX_REQUEST_ID_BYTES and all(
            0x21 <= byte <= 0x7E for byte in value
        )

    @staticmethod
    def _timing_headers(request_id: str, started_at: float) -> list[tuple[bytes, bytes]]:
        api_ms = (monotonic() - started_at) * 1000
        return [
            (b"x-request-id", request_id.encode()),
            (b"x-api-duration-ms", f"{api_ms:.1f}".encode()),
            (b"x-repository-duration-ms", f"{repository_duration_ms():.1f}".encode()),
        ]

    @staticmethod
    async def _send_internal_error(send: Send, request_id: str) -> None:
        """Return the standard safe envelope when no application response began."""
        body = (
            ApiError(
                error=ApiErrorBody(code="internal_error", message="An internal error occurred."),
                meta=ResponseMeta(request_id=request_id),
            )
            .model_dump_json()
            .encode()
        )
        await send(
            cast(
                Message,
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [(b"content-type", b"application/json")],
                },
            )
        )
        await send(cast(Message, {"type": "http.response.body", "body": body}))
