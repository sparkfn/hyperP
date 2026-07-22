"""Authenticated client for WhatsAdmin's HyperP extraction endpoints."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator

import httpx

from src.connectors.whatsadmin_api.credentials import WhatsAdminCredential, WhatsAdminEntity
from src.connectors.whatsadmin_api.models import ChatPage, SessionPage, SessionRow
from src.models import JsonValue

logger = logging.getLogger(__name__)


class WhatsAdminApiClient:
    def __init__(
        self,
        *,
        credential: WhatsAdminCredential,
        page_size: int,
        timeout_seconds: float = 30.0,
        max_attempts: int = 3,
        retry_base_delay_seconds: float = 1.0,
        http: httpx.Client | None = None,
    ) -> None:
        self._credential = credential
        self._base_url = credential.base_url.rstrip("/")
        self._page_size = page_size
        self._max_attempts = max(max_attempts, 1)
        self._retry_base_delay_seconds = max(retry_base_delay_seconds, 0.0)
        self._http = http or httpx.Client(timeout=timeout_seconds)
        self._closed = False
        self._failure_context: dict[str, JsonValue] = {}

    @property
    def entity_key(self) -> WhatsAdminEntity:
        """Return the HyperP entity bound to this authenticated client."""
        return self._credential.entity_key

    def iter_sessions(self) -> Iterator[SessionRow]:
        cursor: str | None = None
        while True:
            payload: dict[str, str | int] = {"limit": self._page_size}
            if cursor is not None:
                payload["cursor"] = cursor
            page = SessionPage.model_validate(self._post("sessions/query", payload))
            yield from page.data
            if not page.meta.pagination.has_more:
                return
            cursor = page.meta.pagination.next_cursor
            if cursor is None:
                raise RuntimeError("WhatsAdmin session page omitted nextCursor")

    def iter_chat_pages(
        self,
        session_id: str,
        changed_since: str | None,
        cursor: str | None = None,
    ) -> Iterator[ChatPage]:
        while True:
            payload: dict[str, str | int] = {
                "sessionId": session_id,
                "limit": self._page_size,
            }
            if changed_since is not None:
                payload["changedSince"] = changed_since
            if cursor is not None:
                payload["cursor"] = cursor
            page = ChatPage.model_validate(self._post("chats/query", payload))
            yield page
            if not page.meta.pagination.has_more:
                return
            cursor = page.meta.pagination.next_cursor
            if cursor is None:
                raise RuntimeError("WhatsAdmin chat page omitted nextCursor")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._http.close()

    def failure_context(self) -> dict[str, JsonValue]:
        """Return safe context for the most recent terminal upstream failure."""
        return dict(self._failure_context)

    def _post(self, resource: str, payload: dict[str, str | int]) -> object:
        self._failure_context.clear()
        for attempt in range(1, self._max_attempts + 1):
            started_at = time.monotonic()
            try:
                response = self._http.post(
                    f"{self._base_url}/api/integrations/hyperp/{resource}",
                    headers={"X-API-Key": self._credential.api_key.get_secret_value()},
                    json=payload,
                )
                response.raise_for_status()
                logger.info(
                    "WhatsAdmin request completed entity=%s resource=%s session=%s "
                    "cursor=%s latency_seconds=%.3f attempt=%d",
                    self.entity_key,
                    resource,
                    payload.get("sessionId", ""),
                    payload.get("cursor", "first"),
                    time.monotonic() - started_at,
                    attempt,
                )
                self._failure_context.clear()
                return response.json()
            except httpx.HTTPStatusError as exc:
                retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
                self._capture_failure(resource, payload, attempt, started_at, type(exc).__name__)
                if not retryable or attempt == self._max_attempts:
                    raise
            except (httpx.ReadTimeout, httpx.TransportError) as exc:
                self._capture_failure(resource, payload, attempt, started_at, type(exc).__name__)
                if attempt == self._max_attempts:
                    raise
            time.sleep(self._retry_base_delay_seconds * (2 ** (attempt - 1)))
        raise AssertionError("unreachable WhatsAdmin retry state")

    def _capture_failure(
        self,
        resource: str,
        payload: dict[str, str | int],
        attempt: int,
        started_at: float,
        exception_class: str,
    ) -> None:
        latency = time.monotonic() - started_at
        self._failure_context = {
            "upstream_resource": resource,
            "upstream_session_id": str(payload.get("sessionId", "")),
            "upstream_cursor": str(payload.get("cursor", "first")),
            "upstream_attempt": attempt,
            "upstream_latency_seconds": latency,
        }
        logger.warning(
            "WhatsAdmin transient request failure entity=%s resource=%s session=%s "
            "cursor=%s latency_seconds=%.3f attempt=%d/%d exception=%s",
            self.entity_key,
            resource,
            payload.get("sessionId", ""),
            payload.get("cursor", "first"),
            latency,
            attempt,
            self._max_attempts,
            exception_class,
        )
