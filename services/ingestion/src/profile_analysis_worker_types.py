"""Typed service, retry, and summary contracts for profile-analysis sweeps."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, TypedDict
from uuid import UUID

from src.llm import ChatMessage, LLMService
from src.profile_analysis_models import ProfileAnalysisStatus
from src.profile_analysis_repository import (
    ClaimedProfileAnalysisPerson,
    DueProfileAnalysis,
    ProfileAnalysisPersistenceResult,
)


class ProfileAnalysisSweepSummary(TypedDict):
    claimed: int
    attempted: int
    succeeded: int
    failed: int
    obsolete: int
    unexpected_failures: int
    released: int
    has_more: bool


class ProfileAnalysisTextService(Protocol):
    provider: str

    @property
    def default_model(self) -> str: ...

    def generate(self, messages: list[ChatMessage], *, max_tokens: int) -> str: ...


class LlmProfileAnalysisTextService:
    """Synchronous adapter around the existing asynchronous LLM hierarchy."""

    provider = "proclaude"

    def __init__(self, service: LLMService) -> None:
        self._service = service

    @property
    def default_model(self) -> str:
        return self._service.default_model

    def generate(self, messages: list[ChatMessage], *, max_tokens: int) -> str:
        return asyncio.run(
            self._service.chat_json(
                messages,
                temperature=0.0,
                max_tokens=max_tokens,
            )
        )


@dataclass(frozen=True, slots=True)
class ProfileAnalysisRetryPolicy:
    max_attempts: int
    base: timedelta
    cap: timedelta

    def next_retry_at(self, due: DueProfileAnalysis, now: datetime) -> datetime | None:
        if due.attempt_number >= self.max_attempts:
            return None
        multiplier = 1 << (due.attempt_number - 1)
        delay = self.base * multiplier
        if delay > self.cap:
            delay = self.cap
        return now + delay


@dataclass(slots=True)
class ProfileAnalysisSweepCounts:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    obsolete: int = 0
    unexpected_failures: int = 0
    released: int = 0

    def record(self, result: ProfileAnalysisPersistenceResult) -> None:
        if result.status is ProfileAnalysisStatus.SUCCEEDED:
            self.succeeded += 1
        elif result.status is ProfileAnalysisStatus.FAILED:
            self.failed += 1
        else:
            self.obsolete += 1


@dataclass(frozen=True, slots=True)
class ProfileAnalysisAttemptContext:
    person: ClaimedProfileAnalysisPerson
    due: DueProfileAnalysis
    fingerprint: str
    prompt_version: str
    text_service: ProfileAnalysisTextService
    started_at: datetime
    retry_policy: ProfileAnalysisRetryPolicy
    clock: Callable[[], datetime]
    uuid_factory: Callable[[], UUID]


def profile_analysis_clock_utc(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("profile analysis clock must be timezone-aware")
    return value.astimezone(UTC)
