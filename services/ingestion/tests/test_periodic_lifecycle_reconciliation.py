"""Recurring repair for legacy records that arrive after marker migrations."""

from __future__ import annotations

from contextlib import nullcontext

from pytest import MonkeyPatch


def test_periodic_reconciliation_is_registered_once_at_short_interval() -> None:
    from src.celery_app import _beat_schedule

    entries = [
        entry
        for entry in _beat_schedule.values()
        if entry["task"] == "src.tasks.reconcile_lifecycle_task"
    ]

    assert entries == [{"task": "src.tasks.reconcile_lifecycle_task", "schedule": 300.0}]


def test_periodic_reconciliation_repairs_source_then_projection_and_closes_client(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    calls: list[str] = []

    class _Client:
        def __init__(self, _settings: object) -> None:
            calls.append("client")

        def verify_connectivity(self) -> None:
            calls.append("verify")

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(tasks, "Neo4jClient", _Client)
    monkeypatch.setattr(
        tasks,
        "reconcile_source_record_lifecycle",
        lambda _client: calls.append("source") or 1,
    )
    monkeypatch.setattr(
        tasks,
        "reconcile_projection_relationship_lifecycle",
        lambda _client: calls.append("projection") or 2,
    )
    monkeypatch.setattr(tasks, "_acquire_source_lock", lambda _key: nullcontext("lock"))
    monkeypatch.setattr(tasks, "_acquire_init_lock", lambda: nullcontext("init"))

    result = tasks.reconcile_lifecycle_task.run()

    assert result == {"status": "complete", "source_records": 1, "projections": 2}
    assert calls == ["client", "verify", "source", "projection", "close"]


def test_concurrent_periodic_reconciliation_is_an_idempotent_skip(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    def _busy(_key: str) -> object:
        raise tasks._SourceAlreadyRunningError("lifecycle-reconciliation")

    monkeypatch.setattr(tasks, "_acquire_source_lock", _busy)

    assert tasks.reconcile_lifecycle_task.run() == {
        "status": "already_running",
        "source_records": 0,
        "projections": 0,
    }


def test_successful_ingestion_runs_low_cost_reconciliation_afterward(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    calls: list[str] = []
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: type("S", (), {"log_level": "INFO", "max_concurrent_ingestions": 1})(),
    )
    monkeypatch.setattr(tasks, "setup_logging", lambda _level: None)
    monkeypatch.setattr(tasks, "initialize_ingestion_graph", lambda: calls.append("initialize"))
    monkeypatch.setattr(tasks, "_acquire_init_lock", lambda: nullcontext("init"))
    monkeypatch.setattr(tasks, "_acquire_source_lock", lambda _key: nullcontext("source"))
    monkeypatch.setattr(tasks, "_acquire_ingestion_slot", lambda _cap: nullcontext("slot"))
    monkeypatch.setattr(tasks, "_renew_ingestion_leases", lambda *_args: nullcontext())
    monkeypatch.setattr(
        tasks,
        "run_ingestion",
        lambda *_args, **_kwargs: calls.append("ingest") or {"status": "complete"},
    )
    monkeypatch.setattr(
        tasks,
        "run_lifecycle_reconciliation",
        lambda: (
            calls.append("reconcile")
            or {
                "status": "complete",
                "source_records": 0,
                "projections": 0,
            }
        ),
    )

    tasks.run_ingestion_task.run("speedzone_phppos")

    assert calls == ["initialize", "ingest", "reconcile"]
