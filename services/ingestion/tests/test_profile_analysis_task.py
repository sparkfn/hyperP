"""Celery delivery and typed rollout configuration for profile analysis."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

from pytest import MonkeyPatch
from src.config import Settings
from src.profile_analysis_worker_types import ProfileAnalysisSweepSummary


def _summary(
    *,
    has_more: bool = False,
    unexpected_failures: int = 0,
) -> ProfileAnalysisSweepSummary:
    return {
        "claimed": 1,
        "attempted": 2,
        "succeeded": 2,
        "failed": 0,
        "obsolete": 0,
        "unexpected_failures": unexpected_failures,
        "released": 1,
        "has_more": has_more,
    }


def test_profile_analysis_settings_are_strict_and_disabled_by_default() -> None:
    settings = Settings(neo4j_password="test")
    config = settings.profile_analysis

    assert config.enabled is False
    assert config.batch_size > 0
    assert config.claim_lease.total_seconds() > 0
    assert config.retry_limit > 0
    assert config.periodic_sweep_interval.total_seconds() > 0


def test_compose_forwards_profile_analysis_settings_to_worker_and_beat() -> None:
    compose = (Path(__file__).resolve().parents[3] / "docker-compose.yml").read_text()

    for setting, default in (
        ("PROFILE_ANALYSIS_ENABLED", "false"),
        ("PROFILE_ANALYSIS_BATCH_SIZE", "25"),
        ("PROFILE_ANALYSIS_CLAIM_LEASE_SECONDS", "900"),
        ("PROFILE_ANALYSIS_RETRY_LIMIT", "3"),
        ("PROFILE_ANALYSIS_SWEEP_INTERVAL_SECONDS", "300"),
    ):
        assert f"{setting}: ${{{setting}:-{default}}}" in compose


def test_disabled_sweep_does_not_construct_graph_or_llm_clients(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    settings = Settings(neo4j_password="test", profile_analysis_enabled=False)
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(
        tasks,
        "Neo4jClient",
        lambda _settings: (_ for _ in ()).throw(AssertionError("graph client constructed")),
    )

    result = tasks.run_profile_analysis_sweep_task.run()

    assert result["claimed"] == 0
    assert result["has_more"] is False


def test_bounded_sweep_requeues_once_only_when_work_remains(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    settings = Settings(neo4j_password="test", profile_analysis_enabled=True)
    calls: list[str] = []

    class _Client:
        def __init__(self, _settings: Settings) -> None:
            calls.append("client")

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "setup_logging", lambda _level: None)
    monkeypatch.setattr(tasks, "Neo4jClient", _Client)
    monkeypatch.setattr(
        tasks,
        "_run_profile_analysis_sweep_once",
        lambda: calls.append("client") or calls.append("close") or _summary(has_more=True),
    )
    monkeypatch.setattr(tasks, "_dispatch_profile_analysis_sweep", lambda: calls.append("dispatch"))

    result = tasks.run_profile_analysis_sweep_task.run()

    assert result["has_more"] is True
    assert calls == ["client", "close", "dispatch"]


def test_unexpected_sweep_failure_relies_on_periodic_recovery_without_hot_chaining(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    settings = Settings(neo4j_password="test", profile_analysis_enabled=True)
    calls: list[str] = []
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "setup_logging", lambda _level: None)
    monkeypatch.setattr(
        tasks,
        "_run_profile_analysis_sweep_once",
        lambda: _summary(has_more=True, unexpected_failures=1),
    )
    monkeypatch.setattr(tasks, "_dispatch_profile_analysis_sweep", lambda: calls.append("dispatch"))

    result = tasks.run_profile_analysis_sweep_task.run()

    assert result["has_more"] is True
    assert result["unexpected_failures"] == 1
    assert calls == []


def test_successful_ingestion_dispatch_failure_does_not_change_success(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    settings = Settings(neo4j_password="test", profile_analysis_enabled=True)
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "setup_logging", lambda _level: None)
    monkeypatch.setattr(tasks, "initialize_ingestion_graph", lambda: None)
    monkeypatch.setattr(tasks, "_acquire_init_lock", lambda: nullcontext("init"))
    monkeypatch.setattr(tasks, "_acquire_source_lock", lambda _key: nullcontext("source"))
    monkeypatch.setattr(tasks, "_acquire_ingestion_slot", lambda _cap: nullcontext("slot"))
    monkeypatch.setattr(tasks, "_renew_ingestion_leases", lambda *_args: nullcontext())
    monkeypatch.setattr(
        tasks,
        "run_ingestion",
        lambda *_args, **_kwargs: {
            "ingest_run_id": "run-1",
            "status": "completed",
            "succeeded": 1,
            "errors": 0,
            "skipped": 0,
            "source_key": "speedzone_phppos",
            "mode": "api",
            "dump_path": None,
        },
    )
    monkeypatch.setattr(tasks, "run_lifecycle_reconciliation", lambda: {})
    monkeypatch.setattr(
        tasks.run_profile_analysis_sweep_task,
        "delay",
        lambda: (_ for _ in ()).throw(RuntimeError("broker unavailable")),
    )

    result = tasks.run_ingestion_task.run("speedzone_phppos", "api")

    assert result["status"] == "completed"


def test_periodic_recovery_uses_the_same_single_bounded_sweep_task() -> None:
    from src.celery_app import _beat_schedule, settings

    matching = [
        entry
        for entry in _beat_schedule.values()
        if entry["task"] == "src.tasks.run_profile_analysis_sweep_task"
    ]
    if settings.profile_analysis_enabled:
        assert matching == [
            {
                "task": "src.tasks.run_profile_analysis_sweep_task",
                "schedule": float(settings.profile_analysis_sweep_interval_seconds),
            }
        ]
    else:
        assert matching == []
