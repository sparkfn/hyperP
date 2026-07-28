"""On-demand Celery delivery contracts for Person profile analysis."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pytest import MonkeyPatch
from src.config import Settings
from src.profile_analysis_worker_types import ProfileAnalysisSweepSummary


def _summary() -> ProfileAnalysisSweepSummary:
    return {
        "claimed": 1,
        "attempted": 1,
        "succeeded": 1,
        "failed": 0,
        "obsolete": 0,
        "unexpected_failures": 0,
        "released": 1,
        "has_more": False,
    }


def test_profile_analysis_settings_are_strict_and_disabled_by_default() -> None:
    settings = Settings(neo4j_password="test")
    config = settings.profile_analysis

    assert config.enabled is False
    assert config.claim_lease.total_seconds() > 0
    assert config.retry_limit > 0


def test_compose_forwards_profile_analysis_runtime_settings() -> None:
    compose = (Path(__file__).resolve().parents[3] / "docker-compose.yml").read_text()

    for setting, default in (
        ("PROFILE_ANALYSIS_ENABLED", "false"),
        ("PROFILE_ANALYSIS_CLAIM_LEASE_SECONDS", "900"),
        ("PROFILE_ANALYSIS_RETRY_LIMIT", "3"),
    ):
        assert f"{setting}: ${{{setting}:-{default}}}" in compose


def test_disabled_request_does_not_construct_graph_or_llm_clients(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    settings = Settings(neo4j_password="test", profile_analysis_enabled=False)
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "setup_logging", lambda _level: None)
    monkeypatch.setattr(
        tasks,
        "Neo4jClient",
        lambda _settings: (_ for _ in ()).throw(AssertionError("graph client constructed")),
    )

    result = tasks.run_profile_analysis_request_task.run("request-1")

    assert result["claimed"] == 0
    assert result["has_more"] is False


def test_request_task_processes_only_the_requested_work(monkeypatch: MonkeyPatch) -> None:
    from src import tasks

    settings = Settings(neo4j_password="test", profile_analysis_enabled=True)
    calls: list[str] = []
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "setup_logging", lambda _level: calls.append("logging"))
    monkeypatch.setattr(
        tasks,
        "_run_profile_analysis_request_once",
        lambda request_id: (calls.append(request_id) or _summary(), None),
    )

    result = tasks.run_profile_analysis_request_task.run("request-sales")

    assert result["succeeded"] == 1
    assert calls == ["logging", "request-sales"]


def test_ingestion_task_has_no_profile_analysis_dispatch() -> None:
    source = (Path(__file__).resolve().parents[1] / "src" / "tasks.py").read_text()

    assert "_dispatch_profile_analysis_sweep" not in source
    assert "run_profile_analysis_sweep_task" not in source


def test_celery_beat_has_no_profile_analysis_recovery_sweep() -> None:
    from src.celery_app import _beat_schedule

    assert all("profile-analysis" not in str(name) for name in _beat_schedule)
    assert all(
        "profile_analysis" not in str(entry.get("task"))
        for entry in _beat_schedule.values()
    )


def test_queued_request_waiting_on_another_claim_is_retried(monkeypatch: MonkeyPatch) -> None:
    from src import tasks

    settings = Settings(neo4j_password="test", profile_analysis_enabled=True)
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "setup_logging", lambda _level: None)
    monkeypatch.setattr(
        tasks,
        "_run_profile_analysis_request_once",
        lambda _request_id: (
            tasks._empty_profile_analysis_summary(has_more=True),
            datetime.now(UTC),
        ),
    )

    with pytest.raises(tasks.Retry):
        tasks.run_profile_analysis_request_task.run("request-waiting")


def test_request_task_logs_traceback_and_returns_safe_failure_summary(
    monkeypatch: MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src import tasks

    settings = Settings(neo4j_password="test", profile_analysis_enabled=True)
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "setup_logging", lambda _level: None)

    def fail_request(_request_id: str) -> tuple[ProfileAnalysisSweepSummary, datetime | None]:
        raise RuntimeError("restricted Neo4j detail")

    monkeypatch.setattr(tasks, "_run_profile_analysis_request_once", fail_request)

    with caplog.at_level(logging.ERROR, logger=tasks.__name__):
        result = tasks.run_profile_analysis_request_task.run("request-1")

    failure_records = [
        record
        for record in caplog.records
        if record.getMessage() == "Profile-analysis request failed; safe_code=request_failed"
    ]
    assert result == tasks._empty_profile_analysis_summary(unexpected_failures=1)
    assert len(failure_records) == 1
    assert failure_records[0].exc_info is not None
    assert "restricted Neo4j detail" not in repr(result)
