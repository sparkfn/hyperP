"""Anthropic Messages (/v1/messages) LLM service."""

from __future__ import annotations

import httpx

from src.llm.base import ChatMessage, LLMService

_DEFAULT_MAX_TOKENS = 4096


class AnthropicService(LLMService):
    """LLM client for the Anthropic Messages wire format (non-streaming)."""

    _endpoint_path = "/v1/messages"

    def _build_payload(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float,
        max_tokens: int | None,
    ) -> dict[str, object]:
        system_parts = [m.content for m in messages if m.role == "system"]
        turns = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
        payload: dict[str, object] = {
            "model": model,
            "messages": turns,
            "max_tokens": max_tokens if max_tokens is not None else _DEFAULT_MAX_TOKENS,
            "temperature": temperature,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        return payload

    def _extra_headers(self, payload: dict[str, object]) -> dict[str, str]:
        model = payload.get("model")
        return {"X-Model-Alias": model} if isinstance(model, str) else {}

    def _parse_text(self, body: object) -> str:
        if not isinstance(body, dict):
            return ""
        content = body.get("content")
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)

    def _is_retryable(self, response: httpx.Response) -> bool:
        if self._is_http_status_retryable(response.status_code):
            return True
        return self._envelope_retryable(response)

    def _retry_after(self, response: httpx.Response, attempt: int) -> float:
        retry_after = self._envelope_retry_after(response)
        if retry_after is not None:
            return max(retry_after, 0.0)
        return super()._retry_after(response, attempt)

    def _envelope_retryable(self, response: httpx.Response) -> bool:
        error = self._error_obj(response)
        return bool(error.get("retryable", False)) if error is not None else False

    def _envelope_retry_after(self, response: httpx.Response) -> float | None:
        error = self._error_obj(response)
        if error is None:
            return None
        value = error.get("retry_after")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    @staticmethod
    def _error_obj(response: httpx.Response) -> dict[str, object] | None:
        try:
            body = response.json()
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        error = body.get("error")
        return error if isinstance(error, dict) else None
