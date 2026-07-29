"""Direct API execution contracts for on-demand profile analysis."""

from __future__ import annotations

from typing import cast

import pytest
from src.config import AppConfig
from src.llm.service import ChatMessage
from src.proclaude.service import MessageParam, ProclaudeService
from src.profile_analysis_repository import ClaimedProfileAnalysisPerson, DueProfileAnalysis
from src.profile_analysis_service import run_profile_analysis_request
from src.profile_analysis_worker_types import (
    LlmProfileAnalysisTextService,
    ProfileAnalysisExecutionSummary,
)


class _Client:
    def __init__(self, _config: AppConfig) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Repository:
    def __init__(self, _client: _Client) -> None:
        self.claims = 0
        self.completed: list[tuple[str, str, str]] = []

    def claim_request(self, **_kwargs: object) -> ClaimedProfileAnalysisPerson | None:
        self.claims += 1
        return ClaimedProfileAnalysisPerson(
            person_id="person-1",
            input_revision=7,
            due=(DueProfileAnalysis("sales", 1),),
        )

    def request_is_waiting(self, *, request_id: str) -> bool:
        _ = request_id
        return False

    def complete_request(self, *, request_id: str, claim_token: str, status: str) -> None:
        self.completed.append((request_id, claim_token, status))

    def obsolete_inactive_request(self, *, request_id: str) -> None:
        self.completed.append((request_id, "", "obsolete"))

    def release_claim(self, *, person_id: str, claim_token: str) -> bool:
        _ = (person_id, claim_token)
        return True


class _InactiveRepository(_Repository):
    def claim_request(self, **_kwargs: object) -> ClaimedProfileAnalysisPerson | None:
        self.claims += 1
        return None


def _summary(
    *,
    attempted: int = 1,
    succeeded: int = 1,
    unexpected_failures: int = 0,
) -> ProfileAnalysisExecutionSummary:
    return {
        "claimed": 1,
        "attempted": attempted,
        "succeeded": succeeded,
        "failed": 0,
        "obsolete": 0,
        "unexpected_failures": unexpected_failures,
        "released": 1,
        "has_more": False,
    }


def test_direct_request_completes_without_celery(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client(AppConfig(neo4j_password="test"))
    repository = _Repository(client)
    monkeypatch.setattr("src.profile_analysis_service.Neo4jClient", lambda _config: client)
    monkeypatch.setattr(
        "src.profile_analysis_service.Neo4jProfileAnalysisRepository",
        lambda _client: repository,
    )
    monkeypatch.setattr(
        "src.profile_analysis_service.run_profile_analysis_person",
        lambda **_kwargs: _summary(),
    )

    result = run_profile_analysis_request("request-1", AppConfig(neo4j_password="test"))

    assert result["succeeded"] == 1
    assert len(repository.completed) == 1
    assert repository.completed[0][0] == "request-1"
    assert repository.completed[0][1]
    assert repository.completed[0][2] == "succeeded"
    assert client.closed is True


def test_direct_request_marks_unexpected_runtime_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client(AppConfig(neo4j_password="test"))
    repository = _Repository(client)
    monkeypatch.setattr("src.profile_analysis_service.Neo4jClient", lambda _config: client)
    monkeypatch.setattr(
        "src.profile_analysis_service.Neo4jProfileAnalysisRepository",
        lambda _client: repository,
    )
    monkeypatch.setattr(
        "src.profile_analysis_service.run_profile_analysis_person",
        lambda **_kwargs: _summary(attempted=0, succeeded=0, unexpected_failures=1),
    )

    with pytest.raises(RuntimeError, match="did not complete one safe attempt"):
        run_profile_analysis_request("request-1", AppConfig(neo4j_password="test"))

    assert len(repository.completed) == 1
    assert repository.completed[0][0] == "request-1"
    assert repository.completed[0][1]
    assert repository.completed[0][2] == "failed"
    assert client.closed is True


def test_unclaimable_inactive_request_is_finalized_without_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client(AppConfig(neo4j_password="test"))
    repository = _InactiveRepository(client)
    monkeypatch.setattr("src.profile_analysis_service.Neo4jClient", lambda _config: client)
    monkeypatch.setattr(
        "src.profile_analysis_service.Neo4jProfileAnalysisRepository",
        lambda _client: repository,
    )

    def unexpected_generation(**_kwargs: object) -> ProfileAnalysisExecutionSummary:
        raise AssertionError("generation must not run for an inactive request")

    monkeypatch.setattr(
        "src.profile_analysis_service.run_profile_analysis_person",
        unexpected_generation,
    )

    result = run_profile_analysis_request("request-1", AppConfig(neo4j_password="test"))

    assert result["claimed"] == 0
    assert repository.completed == [("request-1", "", "obsolete")]
    assert client.closed is True


class _Proclaude:
    def __init__(self) -> None:
        self.system: str | None = None
        self.messages: list[MessageParam] = []
        self.closed = False

    async def create_message_text(
        self,
        messages: list[MessageParam],
        model: str | None = None,
        max_tokens: int = 1024,
        system: str | None = None,
        temperature: float = 0.0,
    ) -> str:
        _ = (model, max_tokens, temperature)
        self.system = system
        self.messages = messages
        return "Observed activity.\nLimitations: Evidence is sparse."

    async def close(self) -> None:
        self.closed = True


def test_profile_analysis_adapter_uses_proclaude_plain_text() -> None:
    service = _Proclaude()
    adapter = LlmProfileAnalysisTextService(cast(ProclaudeService, service))

    output = adapter.generate(
        [
            ChatMessage(role="system", content="System contract"),
            ChatMessage(role="user", content="Safe snapshot"),
        ],
        max_tokens=700,
    )

    assert output == "Observed activity.\nLimitations: Evidence is sparse."
    assert service.system == "System contract"
    assert [(message.role, message.content) for message in service.messages] == [
        ("user", "Safe snapshot")
    ]
    assert service.closed is True
