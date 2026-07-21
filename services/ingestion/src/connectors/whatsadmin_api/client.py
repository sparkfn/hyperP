"""Authenticated client for WhatsAdmin's HyperP extraction endpoints."""

from __future__ import annotations

from collections.abc import Iterator

import httpx

from src.connectors.whatsadmin_api.credentials import WhatsAdminCredential, WhatsAdminEntity
from src.connectors.whatsadmin_api.models import ChatPage, SessionPage, SessionRow


class WhatsAdminApiClient:
    def __init__(
        self,
        *,
        credential: WhatsAdminCredential,
        page_size: int,
        timeout_seconds: float = 30.0,
        http: httpx.Client | None = None,
    ) -> None:
        self._credential = credential
        self._base_url = credential.base_url.rstrip("/")
        self._page_size = page_size
        self._http = http or httpx.Client(timeout=timeout_seconds)
        self._closed = False

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

    def iter_chat_pages(self, session_id: str, changed_since: str | None) -> Iterator[ChatPage]:
        cursor: str | None = None
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

    def _post(self, resource: str, payload: dict[str, str | int]) -> object:
        response = self._http.post(
            f"{self._base_url}/api/integrations/hyperp/{resource}",
            headers={"X-API-Key": self._credential.api_key.get_secret_value()},
            json=payload,
        )
        response.raise_for_status()
        return response.json()
