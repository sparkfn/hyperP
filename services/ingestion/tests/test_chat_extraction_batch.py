"""Regression tests for chat connector LLM extraction batching."""

from __future__ import annotations

import json
from collections.abc import Sequence

from pytest import MonkeyPatch
from src.llm import ChatMessage


class _FakeLlmService:
    async def chat_json(self, messages: Sequence[ChatMessage]) -> str:
        _ = messages
        return json.dumps(
            {
                "persons": [{"name": "Ada", "phone": "+6512345678"}],
                "transactions": [],
                "confidence": 0.9,
            }
        )


class _FakeSettings:
    llm_request_delay_seconds = 0.5


def test_chat_extraction_batch_runs_async_gather_inside_event_loop(
    monkeypatch: MonkeyPatch,
) -> None:
    from src.connectors import chat_helpers

    sleep_calls: list[float] = []

    async def fake_sleep(delay_seconds: float) -> None:
        sleep_calls.append(delay_seconds)

    monkeypatch.setattr(chat_helpers, "get_llm_service", lambda: _FakeLlmService())
    monkeypatch.setattr(chat_helpers, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(chat_helpers.asyncio, "sleep", fake_sleep)

    results = chat_helpers.run_extraction_batch(["hello", "world", "again"])

    assert sleep_calls == [0.5, 1.0]
    assert len(results) == 3
    assert results[0] is not None
    assert results[0]["confidence"] == 0.9
    assert results[0]["persons"][0]["name"] == "Ada"
