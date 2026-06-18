"""Regression tests for ingestion LLM retry behavior."""

from __future__ import annotations

import httpx
import pytest
from src.ingestion_config import LlmConfig
from src.llm import ChatMessage, GPTService, ProclaudeService

_NO_DELAY = LlmConfig(max_retries=2, retry_base_delay_seconds=0.0, retry_max_delay_seconds=0.0)


def _patch_client(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    original = httpx.AsyncClient

    def factory(**kwargs: object) -> httpx.AsyncClient:
        base = kwargs.get("base_url")
        return original(transport=transport, base_url=base if isinstance(base, str) else "")

    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.mark.asyncio
async def test_gpt_retries_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
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
                "id": "c",
                "created": 1,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "{}"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    _patch_client(monkeypatch, httpx.MockTransport(handler))
    svc = GPTService(
        base_url="https://llm.test/v1", api_key="k", default_model="m", llm_config=_NO_DELAY
    )
    result = await svc.chat_json([ChatMessage(role="user", content="extract")])
    assert result == "{}"
    assert calls == 2


@pytest.mark.asyncio
async def test_retries_on_read_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(
            200,
            json={
                "id": "c",
                "created": 1,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "{}"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    _patch_client(monkeypatch, httpx.MockTransport(handler))
    svc = GPTService(
        base_url="https://llm.test/v1", api_key="k", default_model="m", llm_config=_NO_DELAY
    )
    result = await svc.chat_json([ChatMessage(role="user", content="extract")])
    assert result == "{}"
    assert calls == 2


@pytest.mark.asyncio
async def test_read_timeout_propagates_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    _patch_client(monkeypatch, httpx.MockTransport(handler))
    svc = GPTService(
        base_url="https://llm.test/v1", api_key="k", default_model="m", llm_config=_NO_DELAY
    )
    with pytest.raises(httpx.ReadTimeout):
        await svc.chat_json([ChatMessage(role="user", content="extract")])
    # max_retries=2 → 3 total attempts before giving up.
    assert calls == 3


@pytest.mark.asyncio
async def test_proclaude_retries_on_envelope_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                503,
                json={
                    "error": {
                        "type": "overloaded",
                        "code": "overloaded",
                        "message": "busy",
                        "retryable": True,
                        "request_id": "r",
                        "details": {},
                        "retry_after": 0,
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "msg",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "{}"}],
                "model": "claude-x",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    _patch_client(monkeypatch, httpx.MockTransport(handler))
    svc = ProclaudeService(
        base_url="https://proclaude.test",
        api_key="svc_k",
        default_model="claude-x",
        llm_config=_NO_DELAY,
    )
    result = await svc.chat_json([ChatMessage(role="user", content="extract")])
    assert result == "{}"
    assert calls == 2
