"""Client for the multi-tenant WhatsApp Web REST API.

Targets the chrishubert/whatsapp-api server (e.g. ``https://whatsapi.ada.asia``).
The upstream API exposes session-scoped endpoints under four families:

- ``/session/*``  — lifecycle (start, status, qr, restart, terminate)
- ``/client/*``   — client operations (chats, contacts, send message, ...)
- ``/chat/*``     — chat-level methods (fetch messages, clear, delete)
- ``/message/*``  — message-level methods (delete, forward, reactions)

All authenticated routes require an ``x-api-key`` header. Both the base URL
and the API key are sourced from environment variables via
:class:`src.config.Settings` (``WHATSAPP_API_BASE_URL`` / ``WHATSAPP_API_KEY``).
"""

from __future__ import annotations

from types import TracebackType
from typing import Self, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field

from src.config import Settings, get_settings

ResponseT = TypeVar("ResponseT", bound=BaseModel)


# --------------------------------------------------------------------- errors


class WhatsAppApiError(RuntimeError):
    """Raised when the WhatsApp API returns a non-2xx response."""

    def __init__(self, status_code: int, message: str, raw_body: str) -> None:
        super().__init__(f"WhatsApp API {status_code}: {message}")
        self.status_code: int = status_code
        self.raw_body: str = raw_body


# ----------------------------------------------------------- shared sub-models


class WhatsAppId(BaseModel):
    """WhatsApp internal identifier (``user@server``)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    server: str | None = None
    user: str | None = None
    serialized: str | None = Field(default=None, alias="_serialized")


# ----------------------------------------------------------- response models


class SimpleResult(BaseModel):
    """Generic ``{"success": bool, "message": str}`` response."""

    model_config = ConfigDict(extra="allow")

    success: bool
    message: str | None = None


class PingResponse(BaseModel):
    """``GET /ping`` response."""

    model_config = ConfigDict(extra="allow")

    success: bool = True
    message: str | None = None


class SessionStatus(BaseModel):
    """Response shape for ``GET /session/status/{sessionId}``."""

    model_config = ConfigDict(extra="allow")

    success: bool
    state: str | None = None
    message: str | None = None


class QrCode(BaseModel):
    """Response shape for ``GET /session/qr/{sessionId}``."""

    model_config = ConfigDict(extra="allow")

    success: bool
    qr: str | None = None


class Chat(BaseModel):
    """Minimal chat shape returned by ``/client/getChats``."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: WhatsAppId = Field(default_factory=WhatsAppId)
    name: str | None = None
    is_group: bool | None = Field(default=None, alias="isGroup")
    timestamp: int | None = None
    unread_count: int | None = Field(default=None, alias="unreadCount")


class Contact(BaseModel):
    """Minimal contact shape returned by ``/client/getContacts``."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: WhatsAppId = Field(default_factory=WhatsAppId)
    name: str | None = None
    pushname: str | None = None
    number: str | None = None
    is_my_contact: bool | None = Field(default=None, alias="isMyContact")
    is_business: bool | None = Field(default=None, alias="isBusiness")


class ChatsResponse(BaseModel):
    """``GET /client/getChats/{sessionId}`` envelope."""

    model_config = ConfigDict(extra="allow")

    success: bool
    chats: list[Chat] = Field(default_factory=list)


class ContactsResponse(BaseModel):
    """``GET /client/getContacts/{sessionId}`` envelope."""

    model_config = ConfigDict(extra="allow")

    success: bool
    contacts: list[Contact] = Field(default_factory=list)


class MessageRef(BaseModel):
    """Minimal message identifier returned by send/fetch endpoints."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: WhatsAppId | None = None
    body: str | None = None
    timestamp: int | None = None
    from_id: str | None = Field(default=None, alias="from")
    to_id: str | None = Field(default=None, alias="to")


class SendMessageResponse(BaseModel):
    """``POST /client/sendMessage/{sessionId}`` envelope."""

    model_config = ConfigDict(extra="allow")

    success: bool
    message: MessageRef | None = None


class FetchMessagesResponse(BaseModel):
    """``POST /chat/fetchMessages/{sessionId}`` envelope."""

    model_config = ConfigDict(extra="allow")

    success: bool
    messages: list[MessageRef] = Field(default_factory=list)


# ------------------------------------------------------------- request bodies


class SendMessageBody(BaseModel):
    """Request body for ``POST /client/sendMessage/{sessionId}``."""

    model_config = ConfigDict(populate_by_name=True)

    chat_id: str = Field(serialization_alias="chatId")
    content: str
    content_type: str = Field(default="string", serialization_alias="contentType")


class FetchMessagesSearchOptions(BaseModel):
    """Search-options sub-object for ``fetchMessages``."""

    limit: int = 50


class FetchMessagesBody(BaseModel):
    """Request body for ``POST /chat/fetchMessages/{sessionId}``."""

    model_config = ConfigDict(populate_by_name=True)

    chat_id: str = Field(serialization_alias="chatId")
    search_options: FetchMessagesSearchOptions = Field(
        default_factory=FetchMessagesSearchOptions,
        serialization_alias="searchOptions",
    )


# ------------------------------------------------------------------- client


class WhatsAppApiClient:
    """Thin async wrapper around the chrishubert/whatsapp-api HTTP surface.

    Use as a context manager so the underlying ``httpx.AsyncClient`` is closed
    deterministically::

        async with WhatsAppApiClient.from_settings() as wa:
            chats = await wa.get_chats()
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        default_session: str = "default",
        timeout_seconds: float = 30.0,
    ) -> None:
        if not base_url:
            raise ValueError("WhatsApp API base URL is required")
        self._base_url: str = base_url.rstrip("/")
        self._api_key: str = api_key
        self._default_session: str = default_session
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout_seconds,
            headers={"x-api-key": api_key} if api_key else {},
        )

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> Self:
        """Build a client from :class:`Settings` (env-driven)."""
        s = settings or get_settings()
        return cls(
            base_url=s.whatsapp_api_base_url,
            api_key=s.whatsapp_api_key,
            default_session=s.whatsapp_api_default_session,
            timeout_seconds=s.whatsapp_api_timeout_seconds,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    # ------------------------------------------------------------------ core

    def _session(self, session_id: str | None) -> str:
        return session_id or self._default_session

    async def _request(
        self,
        method: str,
        path: str,
        response_model: type[ResponseT],
        *,
        body: BaseModel | None = None,
    ) -> ResponseT:
        json_body: str | None = (
            body.model_dump_json(by_alias=True, exclude_none=True)
            if body is not None
            else None
        )
        headers: dict[str, str] = (
            {"content-type": "application/json"} if json_body is not None else {}
        )
        response = await self._client.request(
            method, path, content=json_body, headers=headers
        )
        if response.status_code >= 400:
            raise WhatsAppApiError(
                response.status_code,
                response.reason_phrase or "request failed",
                response.text,
            )
        return response_model.model_validate_json(response.content)

    # ----------------------------------------------------------------- health

    async def ping(self) -> PingResponse:
        """``GET /ping`` — unauthenticated health check."""
        return await self._request("GET", "/ping", PingResponse)

    # ---------------------------------------------------------------- session

    async def start_session(self, session_id: str | None = None) -> SessionStatus:
        """``GET /session/start/{sessionId}``."""
        return await self._request(
            "GET", f"/session/start/{self._session(session_id)}", SessionStatus
        )

    async def get_session_status(self, session_id: str | None = None) -> SessionStatus:
        """``GET /session/status/{sessionId}``."""
        return await self._request(
            "GET", f"/session/status/{self._session(session_id)}", SessionStatus
        )

    async def get_session_qr(self, session_id: str | None = None) -> QrCode:
        """``GET /session/qr/{sessionId}`` — returns the pairing QR string."""
        return await self._request(
            "GET", f"/session/qr/{self._session(session_id)}", QrCode
        )

    async def restart_session(self, session_id: str | None = None) -> SimpleResult:
        """``GET /session/restart/{sessionId}``."""
        return await self._request(
            "GET", f"/session/restart/{self._session(session_id)}", SimpleResult
        )

    async def terminate_session(self, session_id: str | None = None) -> SimpleResult:
        """``GET /session/terminate/{sessionId}``."""
        return await self._request(
            "GET", f"/session/terminate/{self._session(session_id)}", SimpleResult
        )

    # ----------------------------------------------------------------- client

    async def get_chats(self, session_id: str | None = None) -> list[Chat]:
        """``GET /client/getChats/{sessionId}``."""
        envelope = await self._request(
            "GET", f"/client/getChats/{self._session(session_id)}", ChatsResponse
        )
        return envelope.chats

    async def get_contacts(self, session_id: str | None = None) -> list[Contact]:
        """``GET /client/getContacts/{sessionId}``."""
        envelope = await self._request(
            "GET",
            f"/client/getContacts/{self._session(session_id)}",
            ContactsResponse,
        )
        return envelope.contacts

    async def send_message(
        self,
        chat_id: str,
        content: str,
        *,
        content_type: str = "string",
        session_id: str | None = None,
    ) -> SendMessageResponse:
        """``POST /client/sendMessage/{sessionId}``."""
        body = SendMessageBody(
            chat_id=chat_id, content=content, content_type=content_type
        )
        return await self._request(
            "POST",
            f"/client/sendMessage/{self._session(session_id)}",
            SendMessageResponse,
            body=body,
        )

    # ------------------------------------------------------------------- chat

    async def fetch_messages(
        self,
        chat_id: str,
        *,
        limit: int = 50,
        session_id: str | None = None,
    ) -> FetchMessagesResponse:
        """``POST /chat/fetchMessages/{sessionId}`` (capped at 100 by upstream)."""
        body = FetchMessagesBody(
            chat_id=chat_id,
            search_options=FetchMessagesSearchOptions(limit=limit),
        )
        return await self._request(
            "POST",
            f"/chat/fetchMessages/{self._session(session_id)}",
            FetchMessagesResponse,
            body=body,
        )
