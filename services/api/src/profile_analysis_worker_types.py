"""Typed service, retry, and summary contracts for direct profile analysis."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, TypedDict
from uuid import UUID

import httpx

from src.config import config
from src.llm.service import ChatMessage
from src.proclaude.service import MessageParam, ProclaudeAPIError, ProclaudeService
from src.profile_analysis_models import ProfileAnalysisStatus
from src.profile_analysis_repository import (
    ClaimedProfileAnalysisPerson,
    DueProfileAnalysis,
    ProfileAnalysisPersistenceResult,
)


class ProfileAnalysisExecutionSummary(TypedDict):
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
    """Synchronous adapter around the API's Proclaude client."""

    provider = "proclaude"

    def __init__(self, service: ProclaudeService) -> None:
        self._service = service

    @property
    def default_model(self) -> str:
        return config.proclaude_default_model or "claude-sonnet-4"

    def generate(self, messages: list[ChatMessage], *, max_tokens: int) -> str:
        async def generate_and_close() -> str:
            if not messages or messages[0].role != "system":
                raise ValueError("profile analysis messages must start with a system prompt")
            converted = [
                MessageParam(role=message.role, content=message.content)
                for message in messages[1:]
                if message.role != "system"
            ]
            try:
                return await self._service.create_message_text(
                    converted,
                    system=messages[0].content,
                    temperature=0.0,
                    max_tokens=max_tokens,
                )
            except ProclaudeAPIError as error:
                status = error.status_code or 500
                request = httpx.Request("POST", "https://profile-analysis-provider.invalid")
                response = httpx.Response(status, request=request)
                raise httpx.HTTPStatusError(
                    "profile analysis provider rejected the request",
                    request=request,
                    response=response,
                ) from error
            finally:
                await self._service.close()

        return asyncio.run(generate_and_close())


@dataclass(slots=True)
class ProfileAnalysisExecutionCounts:
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
    clock: Callable[[], datetime]
    uuid_factory: Callable[[], UUID]


def profile_analysis_clock_utc(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("profile analysis clock must be timezone-aware")
    return value.astimezone(UTC)
