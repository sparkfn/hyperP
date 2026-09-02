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
        app_task: asyncio.Future[None] | None = None
        response_started = False
        initial_request: Message | None = None
        initial_is_terminal = False
        initial_ready = asyncio.Event()
        unexpected_message: Message | None = None
        unexpected_ready = asyncio.Event()
        initial_delivered = False
        unexpected_delivered = False

        async def queued_receive() -> Message:
            nonlocal initial_delivered, unexpected_delivered
            await initial_ready.wait()
            if not initial_delivered:
                initial_delivered = True
                if initial_request is None:
                    raise RuntimeError("disconnect monitor did not retain an initial request")
                return initial_request
            if not initial_is_terminal:
                return await receive()
            await unexpected_ready.wait()
            if not unexpected_delivered:
                unexpected_delivered = True
                if unexpected_message is None:
                    raise RuntimeError("disconnect monitor did not retain an unexpected request")
                return unexpected_message
            return await receive()

        async def send_with_timing(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(self._timing_headers(request_id, started_at))
                message = {**message, "headers": headers}
                response_started = True
            await send(message)

        async def pump_disconnect() -> None:
            nonlocal initial_request, initial_is_terminal, unexpected_message
            message = await receive()
            if message["type"] == "http.disconnect":
                if app_task is not None:
                    app_task.cancel()
                return
            initial_request = message
            initial_is_terminal = self._is_terminal_empty_request(message)
            initial_ready.set()
            if not initial_is_terminal:
                return
            while True:
                message = await receive()
                if message["type"] == "http.disconnect" and app_task is not None:
                    app_task.cancel()
                    return
                unexpected_message = message
                unexpected_ready.set()
                return

        try:
            monitor_disconnect = self._is_bodyless_read(scope)
            app_receive = queued_receive if monitor_disconnect else receive
            app_task = asyncio.ensure_future(self.app(scope, app_receive, send_with_timing))
            pump_task = asyncio.create_task(pump_disconnect()) if monitor_disconnect else None
            try:
                await app_task
            except Exception:
                logger.exception("Unhandled error")
                if response_started:
                    raise
                await self._send_internal_error(send_with_timing, request_id)
            finally:
                if pump_task is not None:
                    pump_task.cancel()
                    await asyncio.gather(pump_task, return_exceptions=True)
        finally:
            end_request(token)

    @staticmethod
    def _is_bodyless_read(scope: Scope) -> bool:
        """Return whether this request can be monitored without consuming its body.

        GET/HEAD requests without a declared body are the expensive read paths for
        which we can safely keep one ASGI message of read-ahead in order to notice
        ``http.disconnect``.  A body-bearing request must leave ``receive`` under
        application control so middleware cannot violate its backpressure contract.
        """
        if scope.get("method") not in {"GET", "HEAD"}:
            return False
        headers = cast(list[tuple[bytes, bytes]], scope.get("headers", []))
        content_lengths = [
            value.strip() for name, value in headers if name.lower() == b"content-length"
        ]
        if any(name.lower() == b"transfer-encoding" for name, _value in headers):
            return False
        if not content_lengths:
            return True
        if len(content_lengths) != 1:
            return False
        try:
            return int(content_lengths[0]) == 0
        except ValueError:
            return False

    @staticmethod
    def _is_terminal_empty_request(message: Message) -> bool:
        return (
            message["type"] == "http.request"
            and message.get("body", b"") == b""
            and message.get("more_body", False) is False
        )

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
