"""Client for the proxy-claude-v2 Anthropic Messages API (/v1/messages)."""

from __future__ import annotations

from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from src.config import config


class TextBlock(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, object]


class ToolResultBlock(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str | list[TextBlock]


ContentBlock = Annotated[TextBlock | ToolUseBlock | ToolResultBlock, Field(discriminator="type")]


class MessageParam(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    role: Literal["user", "assistant"]
    content: str | list[ContentBlock]


class MessagesUsage(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    input_tokens: int
    output_tokens: int


class MessagesRequest(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    model: str
    messages: list[MessageParam]
    max_tokens: int
    system: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    stop_sequences: list[str] | None = None
    tools: list[dict[str, object]] | None = None
    tool_choice: dict[str, object] | None = None
    metadata: dict[str, str] | None = None


class MessagesResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    type: Literal["message"]
    role: Literal["assistant"]
    content: list[ContentBlock]
    model: str
    stop_reason: str | None = None
    stop_sequence: str | None = None
    usage: MessagesUsage


class ErrorDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    type: str
    code: str
    message: str
    retryable: bool
    request_id: str
    details: dict[str, object]
    hint: str | None = None
    example: str | None = None
    param: str | None = None
    retry_after: float | None = None


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    error: ErrorDetail


class ProclaudeAPIError(Exception):
    """Raised when /v1/messages returns a non-2xx response.

    `detail` carries the proxy's canonical ErrorDetail when the response body
    is a valid ErrorEnvelope; it is `None` if the body could not be parsed as
    one, in which case this wraps the underlying `httpx.HTTPStatusError`.
    """

    def __init__(
        self,
        message: str,
        detail: ErrorDetail | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.detail = detail
        self.status_code = status_code


class ProclaudeService:
    """Thread-safe client for the proxy-claude-v2 `/v1/messages` endpoint.

    Authenticates with a service credential (`Authorization: Bearer svc_...`).
    The client is lazily initialised on first use and reused across calls.
    Call :meth:`close` when shutting down the application.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._base = (base_url or config.proclaude_api_base_url or "").rstrip("/")
        key = api_key or config.proclaude_api_key or ""
        self._headers: dict[str, str] = {"Authorization": f"Bearer {key}"} if key else {}
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=self._base, timeout=self._timeout)
        return self._client

    async def create_message(
        self,
        messages: list[MessageParam],
        model: str | None = None,
        max_tokens: int = 1024,
        system: str | None = None,
        temperature: float = 0.0,
        top_p: float | None = None,
        top_k: int | None = None,
        stop_sequences: list[str] | None = None,
        tools: list[dict[str, object]] | None = None,
        tool_choice: dict[str, object] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> MessagesResponse:
        """Send a non-streaming Messages request and return the parsed response."""
        client = await self._ensure_client()
        model_alias = model or config.proclaude_default_model or "claude-sonnet-4"
        req = MessagesRequest(
            model=model_alias,
            messages=messages,
            max_tokens=max_tokens,
            system=system,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            stop_sequences=stop_sequences,
            tools=tools,
            tool_choice=tool_choice,
            metadata=metadata,
        )
        headers = {**self._headers, "X-Model-Alias": model_alias}
        response = await client.post(
            "/v1/messages",
            json=req.model_dump(exclude_none=True),
            headers=headers,
        )
        if response.status_code >= 400:
            self._raise_for_error(response)
        return MessagesResponse.model_validate(response.json())

    async def create_message_text(
        self,
        messages: list[MessageParam],
        model: str | None = None,
        max_tokens: int = 1024,
        system: str | None = None,
        temperature: float = 0.0,
    ) -> str:
        """Shortcut — return the concatenated text content of the response."""
        resp = await self.create_message(
            messages,
            model=model,
            max_tokens=max_tokens,
            system=system,
            temperature=temperature,
        )
        return "".join(block.text for block in resp.content if isinstance(block, TextBlock))

    def _raise_for_error(self, response: httpx.Response) -> None:
        try:
            envelope = ErrorEnvelope.model_validate(response.json())
        except (ValueError, ValidationError):
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ProclaudeAPIError(str(exc), status_code=response.status_code) from exc
            return
        raise ProclaudeAPIError(
            envelope.error.message,
            detail=envelope.error,
            status_code=response.status_code,
        )


# Module-level singleton — configure via constructor or environment.
_proclaude_service: ProclaudeService | None = None


def get_proclaude_service() -> ProclaudeService:
    """Return the module-level ProclaudeService singleton."""
    global _proclaude_service
    if _proclaude_service is None:
        _proclaude_service = ProclaudeService()
    return _proclaude_service


async def close_proclaude_service() -> None:
    global _proclaude_service
    if _proclaude_service is not None:
        await _proclaude_service.close()
        _proclaude_service = None
