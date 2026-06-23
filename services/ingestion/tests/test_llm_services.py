"""Tests for the ingestion LLM service hierarchy."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from src.llm import (
    AnthropicService,
    ChatMessage,
    GPTService,
    LLMService,
    OpenAIService,
    ProclaudeService,
)


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler: httpx.MockTransport) -> None:
    original = httpx.AsyncClient

    def factory(**kwargs: object) -> httpx.AsyncClient:
        base = kwargs.get("base_url")
        return original(transport=handler, base_url=base if isinstance(base, str) else "")

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def test_class_hierarchy() -> None:
    assert issubclass(GPTService, OpenAIService)
    assert issubclass(ProclaudeService, AnthropicService)
    assert issubclass(OpenAIService, LLMService)
    assert issubclass(AnthropicService, LLMService)


def test_proclaude_and_gpt_service_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate from the real config file: the accessors load LlmConfig from the
    # configured path, which may not exist on the host.
    import src.llm as llm_pkg
    from src.ingestion_config import IngestionConfig

    monkeypatch.setattr(llm_pkg, "_proclaude_service", None)
    monkeypatch.setattr(llm_pkg, "_gpt_service", None)
    monkeypatch.setattr(llm_pkg, "get_ingestion_config", lambda: IngestionConfig())

    # Proclaude (prose) backs the generic + chat-summary accessors.
    assert isinstance(llm_pkg.get_llm_service(), ProclaudeService)
    assert isinstance(llm_pkg.get_chat_summary_service(), ProclaudeService)
    # GPT (JSON mode) backs address normalization + structured chat extraction.
    assert isinstance(llm_pkg.get_address_llm_service(), GPTService)
    assert isinstance(llm_pkg.get_chat_extraction_service(), GPTService)


def test_service_accessors_share_one_singleton_per_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.llm as llm_pkg
    from src.ingestion_config import IngestionConfig

    monkeypatch.setattr(llm_pkg, "_proclaude_service", None)
    monkeypatch.setattr(llm_pkg, "_gpt_service", None)
    monkeypatch.setattr(llm_pkg, "get_ingestion_config", lambda: IngestionConfig())

    assert llm_pkg.get_chat_extraction_service() is llm_pkg.get_address_llm_service()
    assert llm_pkg.get_chat_summary_service() is llm_pkg.get_llm_service()


def test_fresh_client_per_call_across_event_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    # run_extraction_batch / address normalization call asyncio.run per batch, so
    # each chat_json must open its own client — a reused client would be bound to
    # the previous (closed) event loop. Lock in a fresh client per call.
    constructed = 0
    original = httpx.AsyncClient

    def handler(_request: httpx.Request) -> httpx.Response:
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

    transport = httpx.MockTransport(handler)

    def factory(**kwargs: object) -> httpx.AsyncClient:
        nonlocal constructed
        constructed += 1
        base = kwargs.get("base_url")
        return original(transport=transport, base_url=base if isinstance(base, str) else "")

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    svc = ProclaudeService(base_url="https://proclaude.test", api_key="k", default_model="claude-x")

    out1 = asyncio.run(svc.chat_json([ChatMessage(role="user", content="a")]))
    out2 = asyncio.run(svc.chat_json([ChatMessage(role="user", content="b")]))

    assert out1 == "{}"
    assert out2 == "{}"
    assert constructed == 2  # one fresh client per call, not a reused singleton


@pytest.mark.asyncio
async def test_gpt_service_builds_openai_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured["url"] = str(request.url)
        captured["body"] = _json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "x",
                "created": 1,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"ok":1}'},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    _patch_client(monkeypatch, httpx.MockTransport(handler))
    svc = GPTService(base_url="https://gpt.test/api/v1", api_key="k", default_model="m")
    out = await svc.chat_json(
        [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="hi")]
    )
    assert out == '{"ok":1}'
    assert str(captured["url"]).endswith("/v1/chat/completions")
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "m"
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]


@pytest.mark.asyncio
async def test_chat_json_strips_markdown_code_fences(monkeypatch: pytest.MonkeyPatch) -> None:
    # Anthropic/proclaude has no JSON mode, so models often wrap JSON in ```json
    # fences. chat_json must return parseable JSON regardless.
    import json

    fenced = '```json\n{\n  "addresses": []\n}\n```'

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "m",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": fenced}],
                "model": "claude-x",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    _patch_client(monkeypatch, httpx.MockTransport(handler))
    svc = ProclaudeService(base_url="https://proclaude.test", api_key="k", default_model="claude-x")
    out = await svc.chat_json([ChatMessage(role="user", content="normalize")])
    assert json.loads(out) == {"addresses": []}  # no fence, parses cleanly


@pytest.mark.asyncio
async def test_proclaude_service_splits_system_and_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured["url"] = str(request.url)
        captured["body"] = _json.loads(request.content)
        captured["alias"] = request.headers.get("X-Model-Alias")
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": '{"a":'},
                    {"type": "text", "text": "1}"},
                ],
                "model": "claude-x",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        )

    _patch_client(monkeypatch, httpx.MockTransport(handler))
    svc = ProclaudeService(
        base_url="https://proclaude.test", api_key="svc_k", default_model="claude-x"
    )
    out = await svc.chat_json(
        [
            ChatMessage(role="system", content="be json"),
            ChatMessage(role="user", content="extract"),
        ]
    )
    assert out == '{"a":1}'
    assert str(captured["url"]).endswith("/v1/messages")
    assert captured["alias"] == "claude-x"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["system"] == "be json"
    assert body["messages"] == [{"role": "user", "content": "extract"}]
