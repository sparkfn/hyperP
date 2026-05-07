"""Regression tests for ingestion LLM retry behavior."""

from __future__ import annotations

import httpx
import pytest
from src.llm import ChatMessage, LLMService


@pytest.mark.asyncio
async def test_llm_retries_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MAX_RETRIES", "2")
    monkeypatch.setenv("LLM_RETRY_BASE_DELAY_SECONDS", "0")
    monkeypatch.setenv("LLM_RETRY_MAX_DELAY_SECONDS", "0")

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                json={"error": {"message": "rate limited"}},
                headers={"Retry-After": "0"},
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "{}"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    original_async_client = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        _ = kwargs
        return original_async_client(transport=transport, base_url="https://llm.test/v1")

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    svc = LLMService(base_url="https://llm.test/v1", api_key="test", default_model="test-model")
    result = await svc.chat_json([ChatMessage(role="user", content="extract")])

    assert result == "{}"
    assert calls == 2
