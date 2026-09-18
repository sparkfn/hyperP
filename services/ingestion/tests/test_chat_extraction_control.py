"""Deadline-aware call control threading through shared chat extraction."""

from __future__ import annotations

import pytest
from src.connectors import chat_helpers
from src.ingestion_config import IngestionConfig, LlmConfig
from src.llm import ChatMessage, LlmAttemptControl, LlmCallCancelledError

_VALID = (
    '{"conversations": [{"conversation_index": 0, "persons": [], '
    '"possible_persons": [], "confidence": 0.6}]}'
)
_SUMMARY = "=== Summary 1 ===\nCustomer asked about a unit."


class CountingControl:
    """Allow a fixed number of attempts, then refuse like a spent budget."""

    def __init__(self, allowed: int) -> None:
        self._allowed = allowed
        self.attempts = 0

    def before_attempt(self) -> float | None:
        self.attempts += 1
        if self.attempts > self._allowed:
            raise LlmCallCancelledError("bounded chat extraction attempt budget is exhausted")
        return None

    def backoff_seconds(self, requested_seconds: float) -> float:
        return requested_seconds


class FakeService:
    """Stand-in ProClaude service that reserves every call against the control."""

    default_model = "fixture-model"

    def __init__(self, structured: list[str], summary: str | None = _SUMMARY) -> None:
        self._structured = structured
        self._summary = summary
        self.json_calls = 0
        self.text_calls = 0

    async def chat_json(
        self,
        _messages: list[ChatMessage],
        *,
        max_tokens: int | None = None,
        control: object = None,
    ) -> str:
        _ = max_tokens
        self.json_calls += 1
        _reserve(control)
        index = min(self.json_calls - 1, len(self._structured) - 1)
        return self._structured[index]

    async def chat_text(
        self,
        _messages: list[ChatMessage],
        *,
        max_tokens: int | None = None,
        control: object = None,
        preserve_output_format: bool = False,
    ) -> str:
        _ = max_tokens, preserve_output_format
        self.text_calls += 1
        _reserve(control)
        return self._summary or ""


def _reserve(control: LlmAttemptControl | None) -> None:
    if control is not None:
        control.before_attempt()


def _install(
    monkeypatch: pytest.MonkeyPatch,
    service: FakeService,
    *,
    retry_attempts: int = 1,
) -> None:
    config = IngestionConfig(
        llm=LlmConfig(
            chat_extraction_retry_attempts=retry_attempts,
            chat_max_tokens=64,
            chat_batch_size=6,
            chat_batch_max_chars=6000,
        )
    )
    monkeypatch.setattr(chat_helpers, "get_ingestion_config", lambda: config)
    monkeypatch.setattr(chat_helpers, "get_chat_extraction_service", lambda: service)
    monkeypatch.setattr(chat_helpers, "get_chat_summary_service", lambda: service)


def test_structured_and_summary_calls_each_reserve_one_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService([_VALID])
    _install(monkeypatch, service)
    control = CountingControl(allowed=2)

    outcome = chat_helpers.run_extraction_batch_detailed(["hello"], control=control)

    assert control.attempts == 2
    assert outcome.failures == [None]
    result = outcome.results[0]
    assert result is not None
    assert result["summary"] == "Customer asked about a unit."


def test_isolated_retry_reserves_an_attempt_and_recovers_the_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService(["not json", _VALID])
    _install(monkeypatch, service)
    control = CountingControl(allowed=3)

    outcome = chat_helpers.run_extraction_batch_detailed(["hello"], control=control)

    assert control.attempts == 3
    assert service.json_calls == 2
    assert outcome.failures == [None]
    assert outcome.results[0] is not None


def test_exhausted_chat_reports_a_bounded_failure_and_skips_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService(["not json", "still not json"])
    _install(monkeypatch, service)
    control = CountingControl(allowed=5)

    outcome = chat_helpers.run_extraction_batch_detailed(["hello"], control=control)

    assert control.attempts == 2
    assert service.text_calls == 0
    assert outcome.results == [None]
    failure = outcome.failures[0]
    assert failure is not None
    assert failure.code == "malformed_response"
    assert failure.attempts == 2


def test_cancellation_propagates_instead_of_becoming_a_chat_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService([_VALID])
    _install(monkeypatch, service)
    control = CountingControl(allowed=0)

    with pytest.raises(LlmCallCancelledError):
        chat_helpers.run_extraction_batch_detailed(["hello"], control=control)


def test_summary_is_skipped_when_the_budget_cannot_fund_another_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService([_VALID])
    _install(monkeypatch, service)
    control = CountingControl(allowed=1)

    outcome = chat_helpers.run_extraction_batch_detailed(["hello"], control=control)

    assert control.attempts == 2
    assert service.text_calls == 1
    result = outcome.results[0]
    assert result is not None
    assert result["summary"] is None
    assert outcome.failures == [None]


def test_extraction_without_a_control_stays_unbudgeted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService([_VALID])
    _install(monkeypatch, service)

    outcome = chat_helpers.run_extraction_batch_detailed(["hello"])

    assert outcome.results[0] is not None
    assert chat_helpers.run_extraction_batch(["hello"]) != [None]


def test_batch_extraction_returns_immediately_without_text() -> None:
    outcome = chat_helpers.run_extraction_batch_detailed([])

    assert outcome.results == []
    assert outcome.failures == []
