"""OpenAI-compatible (/v1/chat/completions) LLM service."""

from __future__ import annotations

import httpx

from src.llm.base import ChatMessage, LLMService


class OpenAIService(LLMService):
    """LLM client for servers speaking the OpenAI chat-completions protocol."""

    _endpoint_path = "/chat/completions"

    def _normalize_base(self, base_url: str | None) -> str:
        # Gateways expose the API under /api/v1; ensure exactly a /v1 suffix so
        # the endpoint path resolves to .../v1/chat/completions.
        stripped = (base_url or "").rstrip("/")
        return stripped if stripped.endswith("/v1") else stripped + "/v1"

    def _build_payload(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float,
        max_tokens: int | None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        return payload

    def _parse_text(self, body: object) -> str:
        if not isinstance(body, dict):
            return ""
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        message = first.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        return content if isinstance(content, str) else ""

    def _is_retryable(self, response: httpx.Response) -> bool:
        return self._is_http_status_retryable(response.status_code)
