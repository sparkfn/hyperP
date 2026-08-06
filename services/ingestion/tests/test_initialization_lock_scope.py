"""Initialization-lock admission and telemetry contracts."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from pytest import LogCaptureFixture, MonkeyPatch


class _FakeInitRedis:
    def __init__(self, *, failed_acquisitions: int = 0) -> None:
        self.failed_acquisitions = failed_acquisitions
        self.sequence = 0
        self.values: dict[str, str] = {}
        self.sorted_sets: dict[str, dict[str, float]] = {}

    def incr(self, key: str) -> int:
        del key
        self.sequence += 1
        return self.sequence

    def zadd(self, key: str, values: dict[str, int]) -> int:
        sorted_set = self.sorted_sets.setdefault(key, {})
        sorted_set.update(values)
        return len(values)

    def zrem(self, key: str, member: str) -> int:
        sorted_set = self.sorted_sets.setdefault(key, {})
        if member not in sorted_set:
            return 0
        del sorted_set[member]
        return 1

    def eval(self, script: str, key_count: int, *keys_and_args: str) -> int:
        if key_count == 4:
            _expiry_key, _order_key, _sequence_key, lock_key, *_args, lock_id = keys_and_args
            if self.failed_acquisitions > 0:
                self.failed_acquisitions -= 1
                return 0
            self.values[lock_key] = lock_id
            return 1

        if key_count == 2:
            expiry_key, order_key, waiter_id = keys_and_args
            self.zrem(expiry_key, waiter_id)
            self.zrem(order_key, waiter_id)
            return 1

        assert key_count == 1
        lock_key, expected_owner = keys_and_args
        if self.values.get(lock_key) != expected_owner:
            return 0
        del self.values[lock_key]
        return 1


class _AdvancingClock:
    def __init__(self, step_seconds: float) -> None:
        self.current = 0.0
        self.step_seconds = step_seconds

    def monotonic(self) -> float:
        value = self.current
        self.current += self.step_seconds
        return value


def test_initialization_lock_emits_one_wait_warning_and_duration_events(
    monkeypatch: MonkeyPatch,
    caplog: LogCaptureFixture,
) -> None:
    from src import tasks

    redis_client = _FakeInitRedis(failed_acquisitions=2)
    clock = _AdvancingClock(step_seconds=3.0)
    monkeypatch.setattr(tasks, "_redis_client", lambda: redis_client)
    monkeypatch.setattr(
        tasks.uuid,
        "uuid4",
        lambda: type("OpaqueOwner", (), {"hex": "source-record-123"})(),
    )
    monkeypatch.setattr(tasks.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(tasks.time, "sleep", lambda _seconds: None)
    caplog.set_level(logging.INFO, logger=tasks.__name__)

    with tasks._acquire_init_lock("lifecycle_reconciliation"):
        assert tasks._INIT_LOCK_KEY in redis_client.values

    messages = [record.getMessage() for record in caplog.records]
    warnings = [
        message
        for message in messages
        if message.startswith("initialization_lock_wait_slo_exceeded")
    ]
    acquired = [
        message for message in messages if message.startswith("initialization_lock_acquired")
    ]
    released = [
        message for message in messages if message.startswith("initialization_lock_released")
    ]

    assert len(warnings) == 1
    assert len(acquired) == 1
    assert len(released) == 1
    assert "requester_class=lifecycle_reconciliation" in warnings[0]
    assert "wait_seconds=" in warnings[0]
    assert "wait_seconds=" in acquired[0]
    assert "hold_seconds=" in released[0]
    assert "source-record-123" not in "\n".join(messages)
    assert tasks._INIT_LOCK_KEY not in redis_client.values


def test_source_waiter_registration_refresh_and_pruning_are_one_server_timed_script() -> None:
    from src import tasks

    script = tasks._INIT_LOCK_ACQUIRE_SCRIPT

    assert "redis.call('time')" in script
    assert "redis.call('incr', KEYS[3])" in script
    assert "redis.call('zadd', KEYS[2], sequence, ARGV[1])" in script
    assert "redis.call('zadd', KEYS[1], now + tonumber(ARGV[3]), ARGV[1])" in script
    assert "if not has_expiry or not has_order then" in script
    assert "redis.call('zrem', KEYS[1], waiter)" in script
    assert "redis.call('zrem', KEYS[2], waiter)" in script


def test_knows_work_does_not_hold_initialization_lock(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    init_mutex = threading.Lock()
    knows_work_started = threading.Event()
    release_knows_work = threading.Event()
    source_initialization_finished = threading.Event()
    failures: list[BaseException] = []

    @contextmanager
    def _init_lock() -> Iterator[str]:
        assert init_mutex.acquire(timeout=1)
        try:
            yield "init"
        finally:
            init_mutex.release()

    class _Client:
        def __init__(self, _settings: object) -> None:
            pass

        def close(self) -> None:
            pass

    def _blocked_batch(*_args: object, **_kwargs: object) -> dict[str, object]:
        knows_work_started.set()
        assert release_knows_work.wait(timeout=2)
        return {
            "phase": "contacts",
            "linked": 0,
            "scanned": 0,
            "next_cursor": None,
        }

    monkeypatch.setattr(tasks, "get_settings", lambda: type("S", (), {"log_level": "INFO"})())
    monkeypatch.setattr(tasks, "setup_logging", lambda _level: None)
    monkeypatch.setattr(tasks, "_acquire_init_lock", _init_lock)
    monkeypatch.setattr(tasks, "_renew_init_lock_lease", lambda _lock_id: _null_context())
    monkeypatch.setattr(tasks, "initialize_ingestion_graph", lambda: None)
    monkeypatch.setattr(tasks, "claim_knows_materialization_gate", lambda *_args: True)
    monkeypatch.setattr(tasks, "release_knows_materialization_queue_gate", lambda *_args: True)
    monkeypatch.setattr(tasks, "_acquire_source_lock", lambda *_args: _null_value_context())
    monkeypatch.setattr(tasks, "Neo4jClient", _Client)
    monkeypatch.setattr(tasks, "materialize_knows_batch", _blocked_batch)

    def _run_knows() -> None:
        try:
            tasks.materialize_knows_task.push_request(id="knows-task", retries=0)
            try:
                tasks.materialize_knows_task.run("contacts")
            finally:
                tasks.materialize_knows_task.pop_request()
        except BaseException as exc:
            failures.append(exc)

    knows_thread = threading.Thread(target=_run_knows)
    knows_thread.start()
    assert knows_work_started.wait(timeout=1)

    def _run_source_init() -> None:
        try:
            tasks._initialize_graph_under_lock("source_ingestion")
            source_initialization_finished.set()
        except BaseException as exc:
            failures.append(exc)

    source_thread = threading.Thread(target=_run_source_init)
    source_thread.start()
    acquired_while_knows_blocked = source_initialization_finished.wait(timeout=1)
    release_knows_work.set()
    source_thread.join(timeout=2)
    knows_thread.join(timeout=2)

    assert acquired_while_knows_blocked
    assert failures == []


def test_graph_initialization_telemetry_uses_safe_requester_class(
    monkeypatch: MonkeyPatch,
    caplog: LogCaptureFixture,
) -> None:
    from src import tasks

    lock_active = False

    @contextmanager
    def _init_lock() -> Iterator[str]:
        nonlocal lock_active
        lock_active = True
        try:
            yield "source_ingestion:opaque-owner"
        finally:
            lock_active = False

    def _initialize() -> None:
        assert lock_active

    monkeypatch.setattr(tasks, "_acquire_init_lock", _init_lock)
    monkeypatch.setattr(tasks, "_renew_init_lock_lease", lambda _lock_id: _null_context())
    monkeypatch.setattr(tasks, "initialize_ingestion_graph", _initialize)
    caplog.set_level(logging.INFO, logger=tasks.__name__)

    tasks._initialize_graph_under_lock("source_ingestion")

    messages = [record.getMessage() for record in caplog.records]
    completion = [
        message for message in messages if message.startswith("initialization_graph_complete")
    ]
    assert len(completion) == 1
    assert "requester_class=source_ingestion" in completion[0]
    assert "initialization_seconds=" in completion[0]


@contextmanager
def _null_context() -> Iterator[None]:
    yield


@contextmanager
def _null_value_context() -> Iterator[str]:
    yield "source-lock"


class _AdmissionRedis:
    """Faithful state model for the initialization admission Lua contract."""

    def __init__(self) -> None:
        self.now = 1_000
        self.sequence = 0
        self.lock_owner: str | None = None
        self.order: dict[str, int] = {}
        self.expiry: dict[str, int] = {}

    def eval(self, script: str, key_count: int, *keys_and_args: str) -> int:
        from src import tasks

        if script == tasks._INIT_WAITER_CLEANUP_SCRIPT:
            assert key_count == 2
            waiter_id = keys_and_args[-1]
            self.order.pop(waiter_id, None)
            self.expiry.pop(waiter_id, None)
            return 1
        if script == tasks._SOURCE_LOCK_RELEASE_SCRIPT:
            assert key_count == 1
            expected_owner = keys_and_args[-1]
            if self.lock_owner != expected_owner:
                return 0
            self.lock_owner = None
            return 1

        assert script == tasks._INIT_LOCK_ACQUIRE_SCRIPT
        assert key_count == 4
        waiter_id, requester_class, waiter_lease, _lock_lease, lock_id = keys_and_args[-5:]
        for expired_waiter in [
            waiter for waiter, expires_at in self.expiry.items() if expires_at <= self.now
        ]:
            self.expiry.pop(expired_waiter, None)
            self.order.pop(expired_waiter, None)
        if requester_class == "source_ingestion":
            if waiter_id not in self.expiry or waiter_id not in self.order:
                self.expiry.pop(waiter_id, None)
                self.order.pop(waiter_id, None)
                self.sequence += 1
                self.order[waiter_id] = self.sequence
            self.expiry[waiter_id] = self.now + int(waiter_lease)
            oldest = min(self.order, key=self.order.__getitem__)
            if oldest != waiter_id:
                return 0
        elif self.order:
            return 0
        if self.lock_owner is not None:
            return 0
        self.lock_owner = lock_id
        if requester_class == "source_ingestion":
            self.expiry.pop(waiter_id, None)
            self.order.pop(waiter_id, None)
        return 1


def _attempt_admission(
    client: _AdmissionRedis,
    requester_class: str,
    waiter_id: str,
    lock_id: str,
) -> bool:
    from src import tasks

    return (
        client.eval(
            tasks._INIT_LOCK_ACQUIRE_SCRIPT,
            4,
            tasks._INIT_SOURCE_WAITER_EXPIRY_KEY,
            tasks._INIT_SOURCE_WAITER_ORDER_KEY,
            tasks._INIT_SOURCE_WAITER_SEQUENCE_KEY,
            tasks._INIT_LOCK_KEY,
            waiter_id,
            requester_class,
            str(tasks._INIT_WAITER_LEASE_SECONDS),
            str(tasks._LOCK_LEASE_SECONDS),
            lock_id,
        )
        == 1
    )


def _release_admission(client: _AdmissionRedis, lock_id: str) -> None:
    from src import tasks

    assert client.eval(tasks._SOURCE_LOCK_RELEASE_SCRIPT, 1, tasks._INIT_LOCK_KEY, lock_id) == 1


def test_source_waiters_are_fifo_and_lifecycle_yields_until_the_queue_drains() -> None:
    client = _AdmissionRedis()

    assert _attempt_admission(client, "lifecycle_reconciliation", "", "holder")
    assert not _attempt_admission(client, "source_ingestion", "source-1", "source-lock-1")
    assert not _attempt_admission(client, "source_ingestion", "source-2", "source-lock-2")
    assert not _attempt_admission(client, "knows_contacts", "", "lifecycle-late")
    _release_admission(client, "holder")

    assert not _attempt_admission(client, "source_ingestion", "source-2", "source-lock-2")
    assert _attempt_admission(client, "source_ingestion", "source-1", "source-lock-1")
    _release_admission(client, "source-lock-1")
    assert _attempt_admission(client, "source_ingestion", "source-2", "source-lock-2")
    _release_admission(client, "source-lock-2")
    assert _attempt_admission(client, "knows_contacts", "", "lifecycle-after-drain")


def test_expired_or_cancelled_source_waiters_cannot_block_admission() -> None:
    from src import tasks

    client = _AdmissionRedis()
    assert _attempt_admission(client, "lifecycle_reconciliation", "", "holder")
    assert not _attempt_admission(client, "source_ingestion", "expired", "expired-lock")
    assert not _attempt_admission(client, "source_ingestion", "cancelled", "cancelled-lock")
    client.eval(
        tasks._INIT_WAITER_CLEANUP_SCRIPT,
        2,
        tasks._INIT_SOURCE_WAITER_EXPIRY_KEY,
        tasks._INIT_SOURCE_WAITER_ORDER_KEY,
        "cancelled",
    )
    client.now += tasks._INIT_WAITER_LEASE_SECONDS + 1
    _release_admission(client, "holder")

    assert _attempt_admission(client, "knows_contacts", "", "lifecycle-after-prune")
    assert client.order == {}
    assert client.expiry == {}


def test_pruned_live_source_waiter_rejoins_with_a_new_sequence() -> None:
    from src import tasks

    client = _AdmissionRedis()
    assert _attempt_admission(client, "lifecycle_reconciliation", "", "holder")
    assert not _attempt_admission(client, "source_ingestion", "source", "source-lock")
    first_sequence = client.order["source"]
    client.now += tasks._INIT_WAITER_LEASE_SECONDS + 1

    assert not _attempt_admission(client, "source_ingestion", "source", "source-lock")
    assert client.order["source"] > first_sequence


def test_waiter_cleanup_failure_does_not_fail_successful_initialization(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    class _CleanupFailRedis(_FakeInitRedis):
        def eval(self, script: str, key_count: int, *keys_and_args: str) -> int:
            if script == tasks._INIT_WAITER_CLEANUP_SCRIPT:
                raise RuntimeError("cleanup unavailable")
            return super().eval(script, key_count, *keys_and_args)

    client = _CleanupFailRedis()
    monkeypatch.setattr(tasks, "_redis_client", lambda: client)

    with tasks._acquire_init_lock("source_ingestion"):
        pass


def test_waiter_cleanup_failure_preserves_original_acquisition_error(
    monkeypatch: MonkeyPatch,
) -> None:
    from pytest import raises
    from src import tasks

    class _AcquireAndCleanupFailRedis(_FakeInitRedis):
        def eval(self, script: str, key_count: int, *keys_and_args: str) -> int:
            if script == tasks._INIT_LOCK_ACQUIRE_SCRIPT:
                raise ValueError("acquisition failed")
            if script == tasks._INIT_WAITER_CLEANUP_SCRIPT:
                raise RuntimeError("cleanup unavailable")
            return super().eval(script, key_count, *keys_and_args)

    monkeypatch.setattr(tasks, "_redis_client", _AcquireAndCleanupFailRedis)

    with raises(ValueError, match="acquisition failed"):
        with tasks._acquire_init_lock("source_ingestion"):
            pass


def test_init_lock_release_retries_before_reporting_success(monkeypatch: MonkeyPatch) -> None:
    from src import tasks

    class _TransientReleaseRedis(_FakeInitRedis):
        def __init__(self) -> None:
            super().__init__()
            self.release_attempts = 0

        def eval(self, script: str, key_count: int, *keys_and_args: str) -> int:
            if script == tasks._SOURCE_LOCK_RELEASE_SCRIPT:
                self.release_attempts += 1
                if self.release_attempts < 3:
                    raise RuntimeError("temporary release failure")
            return super().eval(script, key_count, *keys_and_args)

    client = _TransientReleaseRedis()
    monkeypatch.setattr(tasks, "_redis_client", lambda: client)
    monkeypatch.setattr(tasks.time, "sleep", lambda _seconds: None)

    with tasks._acquire_init_lock("lifecycle_reconciliation"):
        pass

    assert client.release_attempts == 3


def test_unconfirmed_init_lock_release_fails_the_successful_context(
    monkeypatch: MonkeyPatch,
) -> None:
    from pytest import raises
    from src import tasks

    class _FailedReleaseRedis(_FakeInitRedis):
        def eval(self, script: str, key_count: int, *keys_and_args: str) -> int:
            if script == tasks._SOURCE_LOCK_RELEASE_SCRIPT:
                raise RuntimeError("redis unavailable")
            return super().eval(script, key_count, *keys_and_args)

    monkeypatch.setattr(tasks, "_redis_client", _FailedReleaseRedis)
    monkeypatch.setattr(tasks.time, "sleep", lambda _seconds: None)

    with raises(RuntimeError, match="Could not confirm"):
        with tasks._acquire_init_lock("lifecycle_reconciliation"):
            pass
