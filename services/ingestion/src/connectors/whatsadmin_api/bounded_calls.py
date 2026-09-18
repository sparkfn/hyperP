"""Attempt-aware LLM budget for one bounded WhatsAdmin chat extraction."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from src.bounded_ingestion_models import CancellationSignal, utc_now
from src.llm import LlmCallCancelledError


class BoundedChatCallControl:
    """Reserve and bound every provider attempt of one bounded extraction.

    The control counts the attempt before it is sent, so transport retries,
    malformed-result retries, and summary calls all consume the same finite
    budget and cannot outlive the bounded unit's deadline.
    """

    def __init__(
        self,
        *,
        deadline: datetime | None,
        cancellation: CancellationSignal | None,
        max_attempts: int,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("bounded chat call budget must be positive")
        self._deadline = deadline
        self._cancellation = cancellation
        self._max_attempts = max_attempts
        self._clock = clock
        self._attempts = 0

    @property
    def attempts(self) -> int:
        """Return the number of provider attempts reserved so far."""
        return self._attempts

    def before_attempt(self) -> float | None:
        """Reserve one attempt, or refuse when the budget cannot fund it."""
        self._require_live()
        remaining = self._remaining_seconds()
        if remaining is not None and remaining <= 0:
            raise LlmCallCancelledError("bounded chat extraction deadline passed")
        if self._attempts >= self._max_attempts:
            raise LlmCallCancelledError("bounded chat extraction attempt budget is exhausted")
        self._attempts += 1
        return remaining

    def backoff_seconds(self, requested_seconds: float) -> float:
        """Return a delay that neither cancels the unit nor outlives its deadline."""
        self._require_live()
        remaining = self._remaining_seconds()
        if remaining is None:
            return requested_seconds
        if remaining <= 0:
            raise LlmCallCancelledError("bounded chat extraction deadline passed")
        return min(requested_seconds, remaining)

    def _require_live(self) -> None:
        if self._cancellation is not None and self._cancellation.requested():
            raise LlmCallCancelledError("bounded chat extraction was cancelled")

    def _remaining_seconds(self) -> float | None:
        if self._deadline is None:
            return None
        return max((self._deadline - self._clock()).total_seconds(), 0.0)
