"""Recurring repair for legacy records that arrive after marker migrations."""

from __future__ import annotations

from contextlib import nullcontext

from celery import Task
from celery.exceptions import Reject
from celery.result import AsyncResult
from pytest import MonkeyPatch, raises


def test_periodic_reconciliation_is_registered_once_hourly() -> None:
    from src.celery_app import _beat_schedule, celery_app
    from src.lifecycle_reconciliation_queue import LifecycleReconciliationTask

    entries = [
        entry
        for entry in _beat_schedule.values()
        if entry["task"] == "src.tasks.reconcile_lifecycle_task"
    ]

    assert entries == [{"task": "src.tasks.reconcile_lifecycle_task", "schedule": 3600.0}]
    celery_app.loader.import_default_modules()
    registered_task = celery_app.tasks["src.tasks.reconcile_lifecycle_task"]
    assert isinstance(registered_task, LifecycleReconciliationTask)


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, key: str, value: str, *, nx: bool) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def eval(self, _script: str, _key_count: int, key: str, value: str) -> int:
        if self.values.get(key) != value:
            return 0
        del self.values[key]
        return 1


def _stub_lifecycle_publish(
    monkeypatch: MonkeyPatch,
    fake_redis: _FakeRedis,
) -> list[str]:
    from src import lifecycle_reconciliation_queue

    published: list[str] = []

    def _publish(
        _task: Task,
        args: tuple[object, ...] | None = None,
        kwargs: dict[str, object] | None = None,
        task_id: str | None = None,
        producer: object | None = None,
        link: object | None = None,
        link_error: object | None = None,
        shadow: str | None = None,
        **options: object,
    ) -> AsyncResult:
        del args, kwargs, producer, link, link_error, shadow, options
        assert task_id is not None
        published.append(task_id)
        return AsyncResult(task_id)

    monkeypatch.setattr(lifecycle_reconciliation_queue, "_redis_client", lambda: fake_redis)
    monkeypatch.setattr(Task, "apply_async", _publish)
    return published


def test_duplicate_scheduling_publishes_only_one_queue_message(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    published = _stub_lifecycle_publish(monkeypatch, fake_redis)

    first = tasks.reconcile_lifecycle_task.apply_async()
    duplicate = tasks.reconcile_lifecycle_task.apply_async()

    assert duplicate.id == first.id
    assert published == [first.id]
    assert list(fake_redis.values.values()) == [first.id]


def test_publish_failure_releases_queue_gate(monkeypatch: MonkeyPatch) -> None:
    from src import lifecycle_reconciliation_queue, tasks

    fake_redis = _FakeRedis()
    monkeypatch.setattr(lifecycle_reconciliation_queue, "_redis_client", lambda: fake_redis)

    def _fail_publish(_task: Task, **_options: object) -> AsyncResult:
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(Task, "apply_async", _fail_publish)

    with raises(RuntimeError, match="broker unavailable"):
        tasks.reconcile_lifecycle_task.apply_async()

    assert fake_redis.values == {}


def test_new_run_can_be_scheduled_after_prior_run_finishes(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    published = _stub_lifecycle_publish(monkeypatch, fake_redis)
    monkeypatch.setattr(
        tasks,
        "run_lifecycle_reconciliation",
        lambda: {"status": "complete", "source_records": 0, "projections": 0},
    )
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: type("S", (), {"log_level": "INFO"})(),
    )
    monkeypatch.setattr(tasks, "setup_logging", lambda _level: None)

    first = tasks.reconcile_lifecycle_task.apply_async()
    tasks.reconcile_lifecycle_task.push_request(id=first.id)
    try:
        tasks.reconcile_lifecycle_task.run()
    finally:
        tasks.reconcile_lifecycle_task.pop_request()

    second = tasks.reconcile_lifecycle_task.apply_async()

    assert second.id != first.id
    assert published == [first.id, second.id]


def test_task_failure_preserves_reject_behavior_and_releases_queue_gate(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    _stub_lifecycle_publish(monkeypatch, fake_redis)
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: type("S", (), {"log_level": "INFO"})(),
    )
    monkeypatch.setattr(tasks, "setup_logging", lambda _level: None)

    def _fail_reconciliation() -> object:
        raise RuntimeError("reconciliation failed")

    monkeypatch.setattr(tasks, "run_lifecycle_reconciliation", _fail_reconciliation)

    queued = tasks.reconcile_lifecycle_task.apply_async()
    tasks.reconcile_lifecycle_task.push_request(id=queued.id)
    try:
        with raises(Reject, match="reconciliation failed"):
            tasks.reconcile_lifecycle_task.run()
    finally:
        tasks.reconcile_lifecycle_task.pop_request()

    assert fake_redis.values == {}


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
