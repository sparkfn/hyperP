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
    assert issubclass(ProclaudeService, OpenAIService)
    assert issubclass(OpenAIService, LLMService)
    assert issubclass(AnthropicService, LLMService)


def test_address_normalization_routes_to_proclaude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Isolate from the real config file: the accessors load LlmConfig from the
    # configured path, which may not exist on the host.
    import src.llm as llm_pkg
    from src.ingestion_config import IngestionConfig

    monkeypatch.setattr(llm_pkg, "_proclaude_service", None)
    monkeypatch.setattr(llm_pkg, "get_ingestion_config", lambda: IngestionConfig())

    proclaude_services = (
        llm_pkg.get_llm_service(),
        llm_pkg.get_chat_summary_service(),
        llm_pkg.get_chat_extraction_service(),
    )
    address_service = llm_pkg.get_address_llm_service()

    assert all(isinstance(service, ProclaudeService) for service in proclaude_services)
    assert len({id(service) for service in proclaude_services}) == 1
    assert isinstance(address_service, ProclaudeService)
    assert all(address_service is service for service in proclaude_services)


def test_service_accessors_share_one_proclaude_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.llm as llm_pkg
    from src.ingestion_config import IngestionConfig

    monkeypatch.setattr(llm_pkg, "_proclaude_service", None)
    monkeypatch.setattr(llm_pkg, "get_ingestion_config", lambda: IngestionConfig())

    assert llm_pkg.get_address_llm_service() is llm_pkg.get_address_llm_service()
    assert llm_pkg.get_chat_extraction_service() is llm_pkg.get_address_llm_service()
    assert llm_pkg.get_chat_summary_service() is llm_pkg.get_llm_service()
    assert llm_pkg.get_address_llm_service() is llm_pkg.get_llm_service()


def test_ingestion_readiness_checks_proclaude_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.llm as llm_pkg

    checked: list[str] = []

    class ReadyService:
        def __init__(self, name: str) -> None:
            self.name = name

        async def validate_readiness(self) -> None:
            checked.append(self.name)

    monkeypatch.setattr(llm_pkg, "_get_proclaude_service", lambda: ReadyService("proclaude"))

    llm_pkg.validate_ingestion_llm_readiness()

    assert checked == ["proclaude"]


def test_gpt_service_uses_connector_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GPT_API_BASE_URL", "https://gpt.example/api/v1")
    monkeypatch.setenv("GPT_API_KEY", "secret-value")
    monkeypatch.setenv("GPT_DEFAULT_MODEL", "available-gpt-model")

    service = GPTService()

    assert service.default_model == "available-gpt-model"


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
                "id": "completion",
                "object": "chat.completion",
                "created": 1,
                "model": "claude-x",
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
async def test_proclaude_service_builds_json_mode_openai_payload(
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
    svc = ProclaudeService(
        base_url="https://proclaude.test", api_key="k", default_model="m"
    )
    out = await svc.chat_json(
        [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="hi")]
    )
    assert out == '{"ok":1}'
    assert str(captured["url"]).endswith("/openai/v1/chat/completions")
    assert captured["alias"] == "m"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "m"
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]


@pytest.mark.asyncio
async def test_gpt_service_builds_json_mode_openai_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured["url"] = str(request.url)
        captured["body"] = _json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"addresses": []}'}}]},
        )

    _patch_client(monkeypatch, httpx.MockTransport(handler))
    service = GPTService(
        base_url="https://gpt.test/api/v1", api_key="k", default_model="gpt-address"
    )

    output = await service.chat_json([ChatMessage(role="user", content="normalize")])

    assert output == '{"addresses": []}'
    assert str(captured["url"]).endswith("/api/v1/chat/completions")
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "gpt-address"
    assert body["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_proclaude_service_can_request_plain_text_for_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured["body"] = _json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "=== Summary 0 ===\nCustomer asked for a quote.",
                        }
                    }
                ]
            },
        )

    _patch_client(monkeypatch, httpx.MockTransport(handler))
    svc = ProclaudeService(
        base_url="https://proclaude.test", api_key="k", default_model="m"
    )

    out = await svc.chat_text([ChatMessage(role="user", content="summarize")])

    assert out == "=== Summary 0 ===\nCustomer asked for a quote."
    body = captured["body"]
    assert isinstance(body, dict)
    assert "response_format" not in body


@pytest.mark.asyncio
async def test_proclaude_readiness_rejects_missing_configured_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"object": "list", "data": [{"id": "claude-haiku-4"}]},
        )

    _patch_client(monkeypatch, httpx.MockTransport(handler))
    svc = ProclaudeService(
        base_url="https://proclaude.test",
        api_key="svc_k",
        default_model="claude-sonnet-4",
    )

    with pytest.raises(RuntimeError, match="claude-sonnet-4.*not available"):
        await svc.validate_readiness()


@pytest.mark.asyncio
async def test_gpt_readiness_requires_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GPT_API_KEY", raising=False)
    service = GPTService(
        base_url="https://gpt.test/api/v1", api_key="", default_model="gpt-address"
    )

    with pytest.raises(RuntimeError, match="GPT_API_KEY is required"):
        await service.validate_readiness()


@pytest.mark.asyncio
async def test_gpt_readiness_uses_configured_endpoint_and_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"data": [{"id": "gpt-address"}]})

    _patch_client(monkeypatch, httpx.MockTransport(handler))
    service = GPTService(
        base_url="https://gpt.test/api/v1",
        api_key="secret-value",
        default_model="gpt-address",
    )

    await service.validate_readiness()

    assert captured == {
        "url": "https://gpt.test/api/v1/models",
        "authorization": "Bearer secret-value",
    }


@pytest.mark.asyncio
async def test_gpt_readiness_rejects_unavailable_configured_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "another-model"}]})

    _patch_client(monkeypatch, httpx.MockTransport(handler))
    service = GPTService(
        base_url="https://gpt.test/api/v1",
        api_key="secret-value",
        default_model="gpt-address",
    )

    with pytest.raises(RuntimeError, match="GPT model 'gpt-address'.*not available") as exc_info:
        await service.validate_readiness()

    assert "secret-value" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_gpt_readiness_rejects_invalid_response_without_exposing_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    _patch_client(monkeypatch, httpx.MockTransport(handler))
    service = GPTService(
        base_url="https://gpt.test/api/v1",
        api_key="secret-value",
        default_model="gpt-address",
    )

    with pytest.raises(
        RuntimeError, match="GPT model readiness returned an invalid response"
    ) as exc_info:
        await service.validate_readiness()

    assert "secret-value" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_chat_json_strips_markdown_code_fences(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the generic Anthropic adapter robust for any direct non-JSON callers.
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
    svc = AnthropicService(
        base_url="https://anthropic.test", api_key="k", default_model="claude-x"
    )
    out = await svc.chat_json([ChatMessage(role="user", content="normalize")])
    assert json.loads(out) == {"addresses": []}  # no fence, parses cleanly
