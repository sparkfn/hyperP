"""Attempt-aware LLM call control: reservation, timeouts, and cancellation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from src.bounded_ingestion_models import AttemptContext, RunScope, Usage
from src.connectors.whatsadmin_api.bounded_calls import BoundedChatCallControl
from src.ingestion_config import LlmConfig
from src.llm import ChatMessage, LlmCallCancelledError, ProclaudeService

_NO_DELAY = LlmConfig(max_retries=2, retry_base_delay_seconds=0.0, retry_max_delay_seconds=0.0)
_BODY = {
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
}


class ProbeControl:
    """Record every reservation and bound each attempt to a fixed duration."""

    def __init__(self, *, remaining: float | None = 5.0, refuses: bool = False) -> None:
        self._remaining = remaining
        self._refuses = refuses
        self.attempts = 0
        self.delays: list[float] = []

    def before_attempt(self) -> float | None:
        self.attempts += 1
        if self._refuses:
            raise LlmCallCancelledError("bounded attempt budget exhausted")
        return self._remaining

    def backoff_seconds(self, requested_seconds: float) -> float:
        self.delays.append(requested_seconds)
        return min(requested_seconds, self._remaining or requested_seconds)


class StubShutdown:
    def __init__(self, requested: bool = False) -> None:
        self._requested = requested

    def requested(self) -> bool:
        return self._requested


def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[object],
) -> list[float | None]:
    """Patch ``httpx.AsyncClient`` and record every request timeout."""
    timeouts: list[float | None] = []

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> bool:
            return False

        async def post(
            self,
            _path: str,
            *,
            json: object = None,
            headers: object = None,
            timeout: float | None = None,
        ) -> httpx.Response:
            _ = json, headers
            timeouts.append(timeout)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return httpx.Response(200, json=_BODY)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    return timeouts


def _service() -> ProclaudeService:
    return ProclaudeService(
        base_url="https://llm.test/v1",
        api_key="k",
        default_model="m",
        llm_config=_NO_DELAY,
    )


@pytest.mark.asyncio
async def test_every_transport_attempt_is_reserved_and_timeout_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("POST", "https://llm.test/v1/chat/completions")
    timeouts = _patch_client(
        monkeypatch,
        [httpx.ReadTimeout("timed out", request=request), None],
    )
    control = ProbeControl(remaining=5.0)

    result = await _service().chat_json(
        [ChatMessage(role="user", content="extract")],
        control=control,
    )

    assert result == "{}"
    assert control.attempts == 2
    assert timeouts == [5.0, 5.0]


@pytest.mark.asyncio
async def test_legacy_callers_keep_the_configured_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts = _patch_client(monkeypatch, [None])

    await _service().chat_json([ChatMessage(role="user", content="extract")])

    assert timeouts == [_NO_DELAY.timeout_seconds]


@pytest.mark.asyncio
async def test_chat_text_honours_the_supplied_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts = _patch_client(monkeypatch, [None])
    control = ProbeControl(remaining=2.5)

    await _service().chat_text(
        [ChatMessage(role="user", content="summarize")],
        control=control,
    )

    assert control.attempts == 1
    assert timeouts == [2.5]


@pytest.mark.asyncio
async def test_cancellation_during_backoff_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("POST", "https://llm.test/v1/chat/completions")
    _patch_client(monkeypatch, [httpx.ReadTimeout("timed out", request=request), None])

    class CancellingControl(ProbeControl):
        def backoff_seconds(self, requested_seconds: float) -> float:
            raise LlmCallCancelledError("deadline passed during backoff")

    with pytest.raises(LlmCallCancelledError):
        await _service().chat_json(
            [ChatMessage(role="user", content="extract")],
            control=CancellingControl(),
        )


@pytest.mark.asyncio
async def test_refusal_before_the_first_attempt_prevents_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts = _patch_client(monkeypatch, [None])

    with pytest.raises(LlmCallCancelledError):
        await _service().chat_json(
            [ChatMessage(role="user", content="extract")],
            control=ProbeControl(refuses=True),
        )

    assert timeouts == []


def test_bounded_call_control_counts_attempts_and_enforces_the_cap() -> None:
    clock = lambda: datetime(2026, 9, 17, 5, 0, tzinfo=UTC)  # noqa: E731
    control = BoundedChatCallControl(
        deadline=datetime(2026, 9, 17, 6, 0, tzinfo=UTC),
        cancellation=StubShutdown(),
        max_attempts=2,
        clock=clock,
    )

    assert control.before_attempt() == 3600.0
    assert control.attempts == 1
    assert control.backoff_seconds(5.0) == 5.0
    assert control.before_attempt() == 3600.0
    assert control.attempts == 2
    with pytest.raises(LlmCallCancelledError, match="budget is exhausted"):
        control.before_attempt()


def test_bounded_call_control_refuses_past_deadline_and_cancellation() -> None:
    now = datetime(2026, 9, 17, 6, 0, tzinfo=UTC)
    expired = BoundedChatCallControl(
        deadline=now - timedelta(seconds=1),
        cancellation=None,
        max_attempts=3,
        clock=lambda: now,
    )
    with pytest.raises(LlmCallCancelledError, match="deadline passed"):
        expired.before_attempt()

    cancelled = BoundedChatCallControl(
        deadline=now + timedelta(hours=1),
        cancellation=StubShutdown(requested=True),
        max_attempts=3,
        clock=lambda: now,
    )
    with pytest.raises(LlmCallCancelledError, match="cancelled"):
        cancelled.before_attempt()


def test_bounded_call_control_clamps_backoff_to_the_remaining_budget() -> None:
    now = datetime(2026, 9, 17, 5, 59, 59, tzinfo=UTC)
    control = BoundedChatCallControl(
        deadline=now + timedelta(seconds=2),
        cancellation=None,
        max_attempts=3,
        clock=lambda: now,
    )

    assert control.backoff_seconds(30.0) == 2.0
    assert control.backoff_seconds(0.5) == 0.5


def test_bounded_call_control_rejects_a_non_positive_budget() -> None:
    with pytest.raises(ValueError, match="positive"):
        BoundedChatCallControl(deadline=None, cancellation=None, max_attempts=0)


def test_bounded_call_control_without_a_deadline_is_unbounded() -> None:
    control = BoundedChatCallControl(
        deadline=None,
        cancellation=None,
        max_attempts=1,
        clock=lambda: datetime(2026, 9, 17, tzinfo=UTC),
    )

    assert control.before_attempt() is None
    assert control.backoff_seconds(3.0) == 3.0
    with pytest.raises(LlmCallCancelledError):
        control.before_attempt()


def test_bounded_call_control_uses_the_attempt_occurrence_deadline() -> None:
    context = _context()
    control = BoundedChatCallControl(
        deadline=context.operation_deadline_at,
        cancellation=context.cancellation,
        max_attempts=2,
        clock=lambda: datetime(2026, 9, 17, 5, 0, tzinfo=UTC),
    )

    assert control.before_attempt() == 300.0


def _context() -> AttemptContext:
    scope = RunScope(
        environment="test",
        reset_generation=1,
        source_key="whatsapp_chat",
        control_instance_id="control",
        entity_key="eko",
        stream_key=None,
        mode="delta",
        configuration_fingerprint="fingerprint",
        connector_version="v1",
        checkpoint_schema_version=1,
        source_window={},
    )
    from src.resumable import CheckpointDescriptor

    return AttemptContext(
        logical_run_id="logical",
        ingest_run_id="attempt-1",
        worker_task_id="task",
        attempt_generation=1,
        fencing_token=1,
        lease_token="lease",
        global_slot_index=0,
        global_slot_fencing_token=1,
        scope=scope,
        occurrence=None,
        checkpoint=CheckpointDescriptor(
            phase="whatsadmin",
            cursor={},
            source_window={},
            last_committed_record_id=None,
            connector_version="v1",
            schema_version=1,
            replay_boundary="boundary",
        ),
        usage=Usage(),
        reserved_usage=Usage(),
        operation_deadline_at=datetime(2026, 9, 17, 5, 5, tzinfo=UTC),
    )
