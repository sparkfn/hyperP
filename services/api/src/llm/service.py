"""LLM client backed by an OpenAI-compatible API endpoint."""

from __future__ import annotations

from typing import Literal, TypedDict

import httpx
from pydantic import BaseModel, ConfigDict
from src.config import config


class Usage(TypedDict, total=False):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ToolFunction(TypedDict, total=False):
    name: str
    description: str
    parameters: dict[str, object]


class ChatMessage(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    role: Literal["system", "user", "assistant"]
    content: str


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    model: str
    messages: list[ChatMessage]
    temperature: float = 0.0
    max_tokens: int | None = None
    tools: list[dict[str, object]] | None = None
    tool_choice: str | None = None
    response_format: dict[str, str] | None = None


class ChatCompletionChoice(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    index: int
    message: ChatMessage
    finish_reason: str | None = None


class ChatCompletionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: Usage | None = None


class LLMService:
    """Thread-safe LLM client for OpenAI-compatible endpoints.

    Supports any server that speaks the `/v1/chat/completions` protocol,
    including Azure AI Foundry, LM Studio, Ollama proxies, and custom
    gateways such as the ada.asia LLM gateway.

    The client is lazily initialised on first use and reused across calls.
    Call :meth:`close` when shutting down the application.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._base = (base_url or config.llm_api_base_url).rstrip("/")
        key = api_key or config.llm_api_key or ""
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

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        tools: list[dict[str, object]] | None = None,
        tool_choice: str | None = None,
        response_format: dict[str, str] | None = None,
    ) -> ChatCompletionResponse:
        """Send a chat completion request and return the parsed response."""
        client = await self._ensure_client()
        req = ChatCompletionRequest(
            model=model or config.llm_default_model or "gpt-4o",
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
        )
        response = await client.post(
            "/v1/chat/completions",
            json=req.model_dump(),
            headers=self._headers,
        )
        response.raise_for_status()
        return ChatCompletionResponse.model_validate(response.json())

    async def chat_text(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        """Shortcut — return the text content of the first assistant message."""
        resp = await self.chat(
            messages, model=model, temperature=temperature, max_tokens=max_tokens
        )
        if not resp.choices:
            return ""
        return resp.choices[0].message.content or ""


# Module-level singleton — configure via constructor or environment.
_llm_service: LLMService | None = None


def get_llm_service() -> LLMService:
    """Return the module-level LLMService singleton."""
    global _llm_service
    if _llm_service is None:
        _llm_service = LLMService()
    return _llm_service


async def close_llm_service() -> None:
    global _llm_service
    if _llm_service is not None:
        await _llm_service.close()
        _llm_service = None
