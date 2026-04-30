"""Tests for src.llm.service."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.llm.service import ChatCompletionChoice, ChatCompletionResponse, ChatMessage


class TestChatMessage:
    def test_create_user_message(self) -> None:
        msg = ChatMessage(role="user", content="hello")
        assert msg.role == "user"
        assert msg.content == "hello"

    def test_create_system_message(self) -> None:
        msg = ChatMessage(role="system", content="you are helpful")
        assert msg.role == "system"
        assert msg.content == "you are helpful"

    def test_model_dump(self) -> None:
        msg = ChatMessage(role="assistant", content="answer")
        assert msg.model_dump() == {"role": "assistant", "content": "answer"}


class TestChatCompletionResponse:
    def test_parse_response(self) -> None:
        data = {
            "id": "chat-1",
            "object": "chat.completion",
            "created": 1234567890,
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello world"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 3,
                "total_tokens": 13,
            },
        }
        resp = ChatCompletionResponse.model_validate(data)
        assert resp.id == "chat-1"
        assert len(resp.choices) == 1
        assert resp.choices[0].message.content == "hello world"
        assert resp.usage is not None
        assert resp.usage["total_tokens"] == 13

    def test_empty_choices(self) -> None:
        data = {
            "id": "chat-2",
            "created": 1234567890,
            "model": "gpt-4o",
            "choices": [],
        }
        resp = ChatCompletionResponse.model_validate(data)
        assert resp.choices == []


@pytest.mark.asyncio()
class TestLLMService:
    async def test_chat_text_returns_message_content(self) -> None:
        with patch("src.llm.service.config") as mock_config:
            mock_config.llm_api_base_url = "https://fake.api.example"
            mock_config.llm_api_key = "fake-key"
            mock_config.llm_default_model = "test-model"

            with patch("src.llm.service.httpx.AsyncClient") as MockClient:
                mock_resp = MagicMock()
                mock_resp.json.return_value = {
                    "id": "chat-1",
                    "created": 1234567890,
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "hello world"},
                            "finish_reason": "stop",
                        }
                    ],
                }
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_resp)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client.is_closed = False
                MockClient.return_value = mock_client

                from src.llm.service import LLMService

                svc = LLMService()
                result = await svc.chat_text(
                    [ChatMessage(role="user", content="say hello")],
                )
                assert result == "hello world"

    async def test_chat_returns_full_response(self) -> None:
        with patch("src.llm.service.config") as mock_config:
            mock_config.llm_api_base_url = "https://fake.api.example"
            mock_config.llm_api_key = "fake-key"
            mock_config.llm_default_model = "test-model"

            with patch("src.llm.service.httpx.AsyncClient") as MockClient:
                mock_resp = MagicMock()
                mock_resp.json.return_value = {
                    "id": "chat-2",
                    "created": 1234567890,
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "answer one"},
                            "finish_reason": "stop",
                        },
                        {
                            "index": 1,
                            "message": {"role": "assistant", "content": "answer two"},
                            "finish_reason": "stop",
                        },
                    ],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 2,
                        "total_tokens": 7,
                    },
                }
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_resp)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client.is_closed = False
                MockClient.return_value = mock_client

                from src.llm.service import LLMService

                svc = LLMService()
                resp = await svc.chat([ChatMessage(role="user", content="give two answers")])
                assert len(resp.choices) == 2
                assert resp.choices[0].message.content == "answer one"
                assert resp.choices[1].message.content == "answer two"
                assert resp.usage is not None
                assert resp.usage["total_tokens"] == 7

    async def test_chat_text_empty_choices_returns_empty_string(self) -> None:
        with patch("src.llm.service.config") as mock_config:
            mock_config.llm_api_base_url = "https://fake.api.example"
            mock_config.llm_api_key = "fake-key"
            mock_config.llm_default_model = "test-model"

            with patch("src.llm.service.httpx.AsyncClient") as MockClient:
                mock_resp = MagicMock()
                mock_resp.json.return_value = {
                    "id": "chat-3",
                    "created": 1234567890,
                    "model": "test-model",
                    "choices": [],
                }
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_resp)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client.is_closed = False
                MockClient.return_value = mock_client

                from src.llm.service import LLMService

                svc = LLMService()
                result = await svc.chat_text([ChatMessage(role="user", content="")])
                assert result == ""

    async def test_close(self) -> None:
        with patch("src.llm.service.config") as mock_config:
            mock_config.llm_api_base_url = "https://fake.api.example"
            mock_config.llm_api_key = ""
            mock_config.llm_default_model = None

            mock_client = AsyncMock()
            mock_client.aclose = AsyncMock()
            mock_client.is_closed = False

            with patch("src.llm.service.httpx.AsyncClient") as MockClient:
                MockClient.return_value = mock_client

                from src.llm.service import LLMService

                svc = LLMService()
                await svc._ensure_client()
                await svc.close()
                mock_client.aclose.assert_called_once()
