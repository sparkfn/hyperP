"""Recurring repair for legacy records that arrive after marker migrations."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext

from celery import Task
from celery.exceptions import Reject, Retry
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

    assert entries == [
        {
            "task": "src.tasks.reconcile_lifecycle_task",
            "schedule": 3600.0,
            "options": {"queue": "lifecycle"},
        }
    ]
    celery_app.loader.import_default_modules()
    registered_task = celery_app.tasks["src.tasks.reconcile_lifecycle_task"]
    assert isinstance(registered_task, LifecycleReconciliationTask)


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.now = 1_000

    def set(self, key: str, value: str, *, nx: bool) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def eval(self, script: str, _key_count: int, key: str, value: str) -> object:
        from src import lifecycle_reconciliation_queue as queue

        current = self.values.get(key)
        if script == queue._ROOT_CLAIM_SCRIPT:
            if current is not None:
                owner = current.split("|", 2)[2] if "|" in current else current
                return [0, owner]
            marker = f"publishing|{self.now}|{value}"
            self.values[key] = marker
            return [1, value, marker]
        if script == queue._CLAIM_QUEUED_SCRIPT:
            owner = current.split("|", 2)[2] if current and "|" in current else current
            if owner != value:
                return 0
            self.values[key] = value
            return 1
        if script == queue._RELEASE_QUEUE_GATE_SCRIPT:
            owner = current.split("|", 2)[2] if current and "|" in current else current
            if owner != value:
                return 0
            del self.values[key]
            return 1
        raise AssertionError("unexpected Lua script")


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


def test_publish_failure_retains_ambiguous_queue_gate(monkeypatch: MonkeyPatch) -> None:
    from src import lifecycle_reconciliation_queue, tasks

    fake_redis = _FakeRedis()
    monkeypatch.setattr(lifecycle_reconciliation_queue, "_redis_client", lambda: fake_redis)

    def _fail_publish(_task: Task, **_options: object) -> AsyncResult:
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(Task, "apply_async", _fail_publish)

    with raises(RuntimeError, match="broker unavailable"):
        tasks.reconcile_lifecycle_task.apply_async()

    value = next(iter(fake_redis.values.values()))
    assert value.startswith("publishing|")


def test_same_owner_source_collision_publishes_actual_same_id_retry(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    published = _stub_lifecycle_publish(monkeypatch, fake_redis)
    fake_redis.values["profile_unifier:lifecycle-reconciliation:queued"] = "reconciliation-task"
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: type("S", (), {"log_level": "INFO"})(),
    )
    monkeypatch.setattr(tasks, "setup_logging", lambda _level: None)

    def _same_owner(_task_id: str | None) -> object:
        raise tasks._SourceAlreadyRunningError(
            "lifecycle-reconciliation",
            held_by_same_task=True,
        )

    monkeypatch.setattr(tasks, "run_lifecycle_reconciliation", _same_owner)
    tasks.reconcile_lifecycle_task.push_request(
        id="reconciliation-task",
        retries=0,
        called_directly=False,
        args=(),
        kwargs={},
        delivery_info={},
    )
    try:
        with raises(Retry):
            tasks.reconcile_lifecycle_task.run()
    finally:
        tasks.reconcile_lifecycle_task.pop_request()

    assert published == ["reconciliation-task"]
    assert list(fake_redis.values.values()) == ["reconciliation-task"]


def test_new_run_can_be_scheduled_after_prior_run_finishes(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    published = _stub_lifecycle_publish(monkeypatch, fake_redis)
    monkeypatch.setattr(
        tasks,
        "run_lifecycle_reconciliation",
        lambda _task_id=None: {
            "status": "complete",
            "source_records": 0,
            "projections": 0,
        },
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

    def _fail_reconciliation(_task_id: str | None = None) -> object:
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
    init_lock_active = False
    source_lock_active = False

    @contextmanager
    def _init_lock() -> Iterator[str]:
        nonlocal init_lock_active
        assert not init_lock_active
        init_lock_active = True
        calls.append("init_lock_enter")
        try:
            yield "init"
        finally:
            calls.append("init_lock_exit")
            init_lock_active = False

    @contextmanager
    def _source_lock(_key: str, owner_id: str | None = None) -> Iterator[str]:
        nonlocal source_lock_active
        assert owner_id == "task-123"
        source_lock_active = True
        calls.append("source_lock_enter")
        try:
            yield owner_id
        finally:
            calls.append("source_lock_exit")
            source_lock_active = False

    class _Client:
        def __init__(self, _settings: object) -> None:
            calls.append("client")

        def verify_connectivity(self) -> None:
            calls.append("verify")

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(tasks, "Neo4jClient", _Client)

    def _initialize() -> None:
        assert init_lock_active
        assert source_lock_active
        calls.append("initialize")

    def _reconcile_source(_client: object) -> int:
        assert not init_lock_active
        assert source_lock_active
        calls.append("source")
        return 1

    def _reconcile_projection(_client: object) -> int:
        assert not init_lock_active
        assert source_lock_active
        calls.append("projection")
        return 2

    monkeypatch.setattr(tasks, "initialize_ingestion_graph", _initialize)
    monkeypatch.setattr(
        tasks,
        "reconcile_source_record_lifecycle",
        _reconcile_source,
    )
    monkeypatch.setattr(
        tasks,
        "reconcile_projection_relationship_lifecycle",
        _reconcile_projection,
    )
    monkeypatch.setattr(tasks, "_acquire_source_lock", _source_lock)
    monkeypatch.setattr(tasks, "_acquire_init_lock", _init_lock)
    monkeypatch.setattr(tasks, "_renew_init_lock_lease", lambda _lock_id: nullcontext())
    monkeypatch.setattr(tasks, "_renew_source_lock_lease", lambda *_args: nullcontext())

    result = tasks.run_lifecycle_reconciliation("task-123")

    assert result == {"status": "complete", "source_records": 1, "projections": 2}
    assert calls == [
        "source_lock_enter",
        "init_lock_enter",
        "initialize",
        "init_lock_exit",
        "client",
        "verify",
        "source",
        "projection",
        "close",
        "source_lock_exit",
    ]


def test_source_initialization_is_not_blocked_by_active_reconciliation_work(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    init_mutex = threading.Lock()
    lifecycle_work_started = threading.Event()
    release_lifecycle_work = threading.Event()
    source_initialization_finished = threading.Event()
    lifecycle_failures: list[BaseException] = []
    source_failures: list[BaseException] = []

    @contextmanager
    def _init_lock() -> Iterator[str]:
        acquired = init_mutex.acquire(timeout=1)
        assert acquired
        try:
            yield "init"
        finally:
            init_mutex.release()

    class _Client:
        def __init__(self, _settings: object) -> None:
            pass

        def verify_connectivity(self) -> None:
            pass

        def close(self) -> None:
            pass

    def _block_reconciliation(_client: object) -> int:
        lifecycle_work_started.set()
        assert release_lifecycle_work.wait(timeout=2)
        return 1

    monkeypatch.setattr(tasks, "get_settings", lambda: object())
    monkeypatch.setattr(tasks, "Neo4jClient", _Client)
    monkeypatch.setattr(tasks, "initialize_ingestion_graph", lambda: None)
    monkeypatch.setattr(tasks, "_acquire_init_lock", _init_lock)
    monkeypatch.setattr(tasks, "_renew_init_lock_lease", lambda _lock_id: nullcontext())
    monkeypatch.setattr(
        tasks,
        "_acquire_source_lock",
        lambda _key, _owner_id=None: nullcontext("lifecycle-lock"),
    )
    monkeypatch.setattr(tasks, "_renew_source_lock_lease", lambda *_args: nullcontext())
    monkeypatch.setattr(tasks, "reconcile_source_record_lifecycle", _block_reconciliation)
    monkeypatch.setattr(
        tasks,
        "reconcile_projection_relationship_lifecycle",
        lambda _client: 2,
    )

    def _run_lifecycle() -> None:
        try:
            tasks.run_lifecycle_reconciliation("lifecycle-task")
        except BaseException as exc:
            lifecycle_failures.append(exc)

    lifecycle_thread = threading.Thread(target=_run_lifecycle)
    lifecycle_thread.start()
    assert lifecycle_work_started.wait(timeout=1)

    def _run_source_initialization() -> None:
        try:
            tasks._initialize_graph_under_lock("source_ingestion")
            source_initialization_finished.set()
        except BaseException as exc:
            source_failures.append(exc)

    source_thread = threading.Thread(target=_run_source_initialization)
    source_thread.start()
    acquired_while_reconciliation_blocked = source_initialization_finished.wait(timeout=1)
    release_lifecycle_work.set()
    source_thread.join(timeout=2)
    lifecycle_thread.join(timeout=2)

    assert acquired_while_reconciliation_blocked
    assert not source_thread.is_alive()
    assert not lifecycle_thread.is_alive()
    assert lifecycle_failures == []
    assert source_failures == []


def test_partial_reconciliation_commit_converges_on_rerun(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    state = {"source_reconciled": False, "projection_reconciled": False}
    projection_attempts = 0

    class _Client:
        def __init__(self, _settings: object) -> None:
            pass

        def verify_connectivity(self) -> None:
            pass

        def close(self) -> None:
            pass

    def _source(_client: object) -> int:
        if state["source_reconciled"]:
            return 0
        state["source_reconciled"] = True
        return 1

    def _projection(_client: object) -> int:
        nonlocal projection_attempts
        projection_attempts += 1
        if projection_attempts == 1:
            raise RuntimeError("projection transaction failed")
        state["projection_reconciled"] = True
        return 1

    monkeypatch.setattr(tasks, "_initialize_graph_under_lock", lambda _requester: None)
    monkeypatch.setattr(tasks, "_acquire_source_lock", lambda *_args: nullcontext("lock"))
    monkeypatch.setattr(tasks, "_renew_source_lock_lease", lambda *_args: nullcontext())
    monkeypatch.setattr(tasks, "get_settings", lambda: object())
    monkeypatch.setattr(tasks, "Neo4jClient", _Client)
    monkeypatch.setattr(tasks, "reconcile_source_record_lifecycle", _source)
    monkeypatch.setattr(tasks, "reconcile_projection_relationship_lifecycle", _projection)

    with raises(RuntimeError, match="projection transaction failed"):
        tasks.run_lifecycle_reconciliation("first-task")

    assert state == {"source_reconciled": True, "projection_reconciled": False}
    assert tasks.run_lifecycle_reconciliation("second-task") == {
        "status": "complete",
        "source_records": 0,
        "projections": 1,
    }
    assert state == {"source_reconciled": True, "projection_reconciled": True}


def test_concurrent_periodic_reconciliation_is_an_idempotent_skip(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    def _busy(_key: str, _owner_id: str | None = None) -> object:
        raise tasks._SourceAlreadyRunningError("lifecycle-reconciliation")

    monkeypatch.setattr(tasks, "_initialize_graph_under_lock", lambda _requester: None)
    monkeypatch.setattr(tasks, "_acquire_source_lock", _busy)

    assert tasks.reconcile_lifecycle_task.run() == {
        "status": "already_running",
        "source_records": 0,
        "projections": 0,
    }


def test_successful_ingestion_queues_lifecycle_reconciliation(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    calls: list[str] = []
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: type("S", (), {"log_level": "INFO"})(),
    )
    monkeypatch.setattr(tasks, "setup_logging", lambda _level: None)
    monkeypatch.setattr(
        tasks,
        "_initialize_graph_under_lock",
        lambda requester: calls.append(f"initialize:{requester}"),
    )
    monkeypatch.setattr(
        tasks,
        "_acquire_source_locks",
        lambda _keys, _owner=None: nullcontext((("source", "lock"),)),
    )
    monkeypatch.setattr(tasks, "_acquire_ingestion_slot", lambda _cap: nullcontext("slot"))
    monkeypatch.setattr(tasks, "_renew_ingestion_leases", lambda *_args: nullcontext())
    monkeypatch.setattr(
        tasks,
        "run_ingestion",
        lambda *_args, **_kwargs: calls.append("ingest") or {"status": "completed"},
    )
    monkeypatch.setattr(
        tasks.reconcile_lifecycle_task,
        "apply_async",
        lambda **options: calls.append(f"reconcile:{options['queue']}"),
    )

    tasks.run_ingestion_task.run("speedzone_phppos")

    assert calls == ["initialize:source_ingestion", "ingest", "reconcile:lifecycle"]


def test_ambiguous_reconciliation_publish_error_keeps_running_owner(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import lifecycle_reconciliation_queue as queue
    from src import tasks

    fake_redis = _FakeRedis()
    monkeypatch.setattr(queue, "_redis_client", lambda: fake_redis)

    def _accepted_then_lost(
        _task: Task,
        *,
        task_id: str,
        **_options: object,
    ) -> AsyncResult:
        assert queue.claim_lifecycle_reconciliation_queue_gate(task_id)
        raise RuntimeError("broker response lost")

    monkeypatch.setattr(Task, "apply_async", _accepted_then_lost)

    with raises(RuntimeError, match="broker response lost"):
        tasks.reconcile_lifecycle_task.apply_async(task_id="running")

    assert list(fake_redis.values.values()) == ["running"]


def test_reconciliation_retry_publish_failure_requeues_current_delivery(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import lifecycle_reconciliation_queue as queue
    from src import tasks

    fake_redis = _FakeRedis()
    gate_key = "profile_unifier:lifecycle-reconciliation:queued"
    fake_redis.values[gate_key] = "retry-id"
    monkeypatch.setattr(queue, "_redis_client", lambda: fake_redis)
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: type("S", (), {"log_level": "INFO"})(),
    )
    monkeypatch.setattr(tasks, "setup_logging", lambda _level: None)

    def _same_owner(_task_id: str | None) -> object:
        raise tasks._SourceAlreadyRunningError(
            "lifecycle-reconciliation",
            held_by_same_task=True,
        )

    def _broker_down(_task: Task, **_options: object) -> AsyncResult:
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(tasks, "run_lifecycle_reconciliation", _same_owner)
    monkeypatch.setattr(Task, "apply_async", _broker_down)
    tasks.reconcile_lifecycle_task.push_request(
        id="retry-id",
        retries=0,
        called_directly=False,
        args=(),
        kwargs={},
        delivery_info={},
    )
    try:
        with raises(Reject) as raised:
            tasks.reconcile_lifecycle_task.run()
    finally:
        tasks.reconcile_lifecycle_task.pop_request()

    assert raised.value.requeue is True
    assert fake_redis.values[gate_key] == "retry-id"
