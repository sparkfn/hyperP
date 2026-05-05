"""LLM client for ingestion service — OpenAI-compatible endpoints.

Copied from the API service so the ingestion worker can call the LLM
without routing through FastAPI. Shares the same configuration (env vars)
as the API service.
"""

from __future__ import annotations

from typing import Literal, TypedDict

import httpx
from pydantic import BaseModel, ConfigDict


class Usage(TypedDict, total=False):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


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

    Used by WhatsApp / Bitrix connectors to extract structured identity and
    transaction data from raw message text.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
        default_model: str = "Qwen/Qwen2.5-72B-Instruct",
    ) -> None:
        # Read same env vars as API service.
        import os as _os

        # Normalise base URL: ensure exactly /v1 suffix, no double-prefix.
        # Handles both "https://host/api/v1" and "https://host/api/v1/" cleanly.
        raw = base_url or _os.environ.get("LLM_API_BASE_URL", "https://gpt.ada.asia/api/v1")
        stripped = raw.rstrip("/")
        self._base = stripped if stripped.endswith("/v1") else stripped + "/v1"
        key = api_key or _os.environ.get("LLM_API_KEY", "")
        self._headers: dict[str, str] = {"Authorization": f"Bearer {key}"} if key else {}
        self._timeout = timeout
        self._default_model = default_model

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: dict[str, str] | None = None,
    ) -> ChatCompletionResponse:
        async with httpx.AsyncClient(base_url=self._base, timeout=self._timeout) as client:
            req = ChatCompletionRequest(
                model=model or self._default_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
            )
            response = await client.post(
                "/chat/completions",
                json=req.model_dump(),
                headers=self._headers,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as err:
                body = response.text[:500]
                raise httpx.HTTPStatusError(
                    f"{response.status_code} {response.reason_phrase}: {body}",
                    request=response.request,
                    response=response,
                ) from err
            return ChatCompletionResponse.model_validate(response.json())

    async def chat_json(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        """Return the text content of the first assistant message with JSON mode."""
        resp = await self.chat(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        if not resp.choices:
            return ""
        return resp.choices[0].message.content or ""


# Module-level lazy singleton — instantiated fresh per-task to avoid
# sharing httpx client state across Celery workers.
_llm_service: LLMService | None = None


def get_llm_service() -> LLMService:
    global _llm_service
    if _llm_service is None:
        import os as _os

        _llm_service = LLMService(
            api_key=_os.environ.get("LLM_API_KEY"),
            default_model=_os.environ.get("LLM_DEFAULT_MODEL", "Qwen/Qwen2.5-72B-Instruct"),
        )
    return _llm_service
