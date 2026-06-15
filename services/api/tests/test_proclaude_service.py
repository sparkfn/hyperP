"""Tests for src.proclaude.service."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.proclaude.service import (
    ErrorEnvelope,
    MessageParam,
    MessagesResponse,
    ProclaudeAPIError,
    TextBlock,
    ToolUseBlock,
)


class TestMessageParam:
    def test_create_user_message(self) -> None:
        msg = MessageParam(role="user", content="hello")
        assert msg.role == "user"
        assert msg.content == "hello"


class TestMessagesResponse:
    def test_parse_text_response(self) -> None:
        data = {
            "id": "msg_018f0d6f3a40",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello!"}],
            "model": "claude-sonnet-4",
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 3},
        }
        resp = MessagesResponse.model_validate(data)
        assert resp.id == "msg_018f0d6f3a40"
        assert len(resp.content) == 1
        block = resp.content[0]
        assert isinstance(block, TextBlock)
        assert block.text == "Hello!"
        assert resp.usage.input_tokens == 12
        assert resp.usage.output_tokens == 3

    def test_parse_tool_use_response(self) -> None:
        data = {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "get_weather",
                    "input": {"location": "Singapore"},
                }
            ],
            "model": "claude-sonnet-4",
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": {"input_tokens": 20, "output_tokens": 10},
        }
        resp = MessagesResponse.model_validate(data)
        block = resp.content[0]
        assert isinstance(block, ToolUseBlock)
        assert block.name == "get_weather"
        assert block.input == {"location": "Singapore"}


class TestErrorEnvelope:
    def test_parse_error_envelope(self) -> None:
        data = {
            "error": {
                "type": "rate_limit_error",
                "code": "rate_limited",
                "message": "Too many requests",
                "retryable": True,
                "request_id": "req_123",
                "details": {"retry_after_ms": 1000},
            }
        }
        envelope = ErrorEnvelope.model_validate(data)
        assert envelope.error.type == "rate_limit_error"
        assert envelope.error.retryable is True
        assert envelope.error.hint is None


@pytest.mark.asyncio()
class TestProclaudeService:
    async def test_create_message_text_returns_concatenated_text(self) -> None:
        with patch("src.proclaude.service.config") as mock_config:
            mock_config.proclaude_api_base_url = "https://fake.proclaude.example"
            mock_config.proclaude_api_key = "svc_fake"
            mock_config.proclaude_default_model = "claude-sonnet-4"

            with patch("src.proclaude.service.httpx.AsyncClient") as MockClient:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hello!"}],
                    "model": "claude-sonnet-4",
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 12, "output_tokens": 3},
                }
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_resp)
                mock_client.is_closed = False
                MockClient.return_value = mock_client

                from src.proclaude.service import ProclaudeService

                svc = ProclaudeService()
                result = await svc.create_message_text(
                    [MessageParam(role="user", content="Say hello in one word.")],
                )
                assert result == "Hello!"

    async def test_create_message_returns_full_response(self) -> None:
        with patch("src.proclaude.service.config") as mock_config:
            mock_config.proclaude_api_base_url = "https://fake.proclaude.example"
            mock_config.proclaude_api_key = "svc_fake"
            mock_config.proclaude_default_model = "claude-sonnet-4"

            with patch("src.proclaude.service.httpx.AsyncClient") as MockClient:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {
                    "id": "msg_2",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "answer"}],
                    "model": "claude-sonnet-4",
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 5, "output_tokens": 2},
                }
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_resp)
                mock_client.is_closed = False
                MockClient.return_value = mock_client

                from src.proclaude.service import ProclaudeService

                svc = ProclaudeService()
                resp = await svc.create_message(
                    [MessageParam(role="user", content="give an answer")],
                    max_tokens=100,
                )
                assert resp.id == "msg_2"
                assert resp.usage.input_tokens == 5
                mock_client.post.assert_called_once()
                call = mock_client.post.call_args
                assert call.args[0] == "/v1/messages"
                assert call.kwargs["headers"] == {
                    "Authorization": "Bearer svc_fake",
                    "X-Model-Alias": "claude-sonnet-4",
                }
                assert call.kwargs["json"]["max_tokens"] == 100
                assert call.kwargs["json"]["model"] == "claude-sonnet-4"

    async def test_create_message_raises_proclaude_api_error_on_error_envelope(self) -> None:
        with patch("src.proclaude.service.config") as mock_config:
            mock_config.proclaude_api_base_url = "https://fake.proclaude.example"
            mock_config.proclaude_api_key = "svc_fake"
            mock_config.proclaude_default_model = "claude-sonnet-4"

            with patch("src.proclaude.service.httpx.AsyncClient") as MockClient:
                mock_resp = MagicMock()
                mock_resp.status_code = 429
                mock_resp.json.return_value = {
                    "error": {
                        "type": "rate_limit_error",
                        "code": "rate_limited",
                        "message": "Too many requests",
                        "retryable": True,
                        "request_id": "req_123",
                        "details": {},
                    }
                }
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_resp)
                mock_client.is_closed = False
                MockClient.return_value = mock_client

                from src.proclaude.service import ProclaudeService

                svc = ProclaudeService()
                with pytest.raises(ProclaudeAPIError) as exc_info:
                    await svc.create_message(
                        [MessageParam(role="user", content="hi")],
                    )
                assert exc_info.value.status_code == 429
                assert exc_info.value.detail is not None
                assert exc_info.value.detail.type == "rate_limit_error"
                assert exc_info.value.detail.retryable is True

    async def test_close(self) -> None:
        with patch("src.proclaude.service.config") as mock_config:
            mock_config.proclaude_api_base_url = "https://fake.proclaude.example"
            mock_config.proclaude_api_key = ""
            mock_config.proclaude_default_model = None

            mock_client = AsyncMock()
            mock_client.aclose = AsyncMock()
            mock_client.is_closed = False

            with patch("src.proclaude.service.httpx.AsyncClient") as MockClient:
                MockClient.return_value = mock_client

                from src.proclaude.service import ProclaudeService

                svc = ProclaudeService()
                await svc._ensure_client()
                await svc.close()
                mock_client.aclose.assert_called_once()
