"""Abstract LLM service base: unified chat_json entrypoint + retry loop."""

from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict

from src.ingestion_config import LlmConfig


class ChatMessage(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    role: Literal["system", "user", "assistant"]
    content: str


class LLMService(ABC):
    """Backend-agnostic LLM client.

    Owns the unified ``chat_json`` entrypoint, the retry/backoff loop, and a
    reused ``httpx.AsyncClient``. Subclasses implement protocol-specific hooks.
    """

    _endpoint_path: str

    def __init__(
        self,
        base_url: str | None,
        api_key: str | None,
        default_model: str,
        llm_config: LlmConfig | None = None,
    ) -> None:
        self._base = self._normalize_base(base_url)
        key = api_key or ""
        self._headers: dict[str, str] = {"Authorization": f"Bearer {key}"} if key else {}
        self._default_model_value = default_model
        self._config = llm_config or LlmConfig()

    @property
    def default_model(self) -> str:
        return self._default_model_value

    def _normalize_base(self, base_url: str | None) -> str:
        return (base_url or "").rstrip("/")

    async def chat_json(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        """Return the assistant text for ``messages`` (JSON-by-prompting).

        A fresh ``httpx.AsyncClient`` is opened per call: ingestion drives these
        via ``asyncio.run`` (one event loop per batch), so a reused client would
        be bound to an already-closed loop on the next batch.
        """
        payload = self._build_payload(
            messages, model or self._default_model_value, temperature, max_tokens
        )
        headers = {**self._headers, **self._extra_headers(payload)}
        max_retries = max(self._config.max_retries, 0)
        async with httpx.AsyncClient(
            base_url=self._base, timeout=self._config.timeout_seconds
        ) as client:
            for attempt in range(max_retries + 1):
                try:
                    response = await client.post(self._endpoint_path, json=payload, headers=headers)
                except (httpx.TimeoutException, httpx.TransportError):
                    # A slow/incomplete response or transient network error is
                    # retryable; only give up once retries are exhausted.
                    if attempt == max_retries:
                        raise
                    await asyncio.sleep(self._backoff_delay(attempt))
                    continue
                if response.status_code < 400:
                    return _strip_code_fences(self._parse_text(response.json()))
                if not self._is_retryable(response) or attempt == max_retries:
                    _raise_http_status(response)
                await asyncio.sleep(self._retry_after(response, attempt))
        raise RuntimeError("unreachable LLM retry state")

    def _backoff_delay(self, attempt: int) -> float:
        capped = min(
            self._config.retry_base_delay_seconds * (2**attempt),
            self._config.retry_max_delay_seconds,
        )
        return random.uniform(capped * 0.5, capped)

    def _retry_after(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return max(float(retry_after), 0.0)
            except ValueError:
                pass
        return self._backoff_delay(attempt)

    @staticmethod
    def _is_http_status_retryable(status_code: int) -> bool:
        return status_code == 429 or 500 <= status_code <= 599

    # Protocol-specific hooks ------------------------------------------------

    @abstractmethod
    def _build_payload(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float,
        max_tokens: int | None,
    ) -> dict[str, object]: ...

    def _extra_headers(self, payload: dict[str, object]) -> dict[str, str]:
        return {}

    @abstractmethod
    def _parse_text(self, body: object) -> str: ...

    @abstractmethod
    def _is_retryable(self, response: httpx.Response) -> bool: ...


def _strip_code_fences(text: str) -> str:
    """Unwrap a Markdown code fence around the response.

    OpenAI JSON mode returns bare JSON, but Anthropic/proxy backends have no
    JSON mode and frequently wrap the payload in ```json … ``` fences despite
    the prompt. Strip a leading fence (with optional language tag) and the
    matching closing fence so ``chat_json`` callers can ``json.loads`` directly.
    A response with no leading fence is returned unchanged.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    first_newline = stripped.find("\n")
    if first_newline == -1:
        return stripped
    body = stripped[first_newline + 1 :]
    closing = body.rfind("```")
    if closing != -1:
        body = body[:closing]
    return body.strip()


def _raise_http_status(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as err:
        body = response.text[:500]
        raise httpx.HTTPStatusError(
            f"{response.status_code} {response.reason_phrase}: {body}",
            request=response.request,
            response=response,
        ) from err
