"""Single-flight and task-state contracts for deferred KNOWS materialization."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from celery import Task
from celery.exceptions import Reject, Retry
from celery.result import AsyncResult
from pytest import MonkeyPatch, raises


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.now = 1_000
        self.fail = False

    def get(self, key: str) -> str | None:
        if self.fail:
            raise RuntimeError("redis unavailable")
        return self.values.get(key)

    def eval(self, script: str, _key_count: int, key: str, *args: str) -> object:
        from src import knows_materialization_queue as queue

        if self.fail:
            raise RuntimeError("redis unavailable")
        current = self.values.get(key)
        if script == queue._ROOT_CLAIM_SCRIPT:
            (task_id,) = args
            if current is not None:
                parts = current.split("|", 2)
                owner = parts[2] if len(parts) == 3 else current
                return [0, owner]
            marker = f"publishing|{self.now}|{task_id}"
            self.values[key] = marker
            return [1, task_id, marker]
        if script == queue._RELEASE_SCRIPT:
            expected = args[0]
            owner = current.split("|", 2)[2] if current and "|" in current else current
            if owner != expected:
                return 0
            del self.values[key]
            return 1
        if script == queue._CLAIM_SCRIPT:
            task_id, predecessor = args
            owner = current.split("|", 2)[2] if current and "|" in current else current
            if owner == task_id or (predecessor and current == predecessor):
                self.values[key] = task_id
                return 1
            return 0
        if script == queue._TRANSFER_SCRIPT:
            current_task_id, next_task_id = args
            if current != current_task_id:
                return 0
            self.values[key] = f"publishing|{self.now}|{next_task_id}"
            return 1
        if script == queue._MARK_QUEUED_SCRIPT:
            task_id = args[0]
            owner = current.split("|", 2)[2] if current and "|" in current else current
            if owner != task_id:
                return 0
            self.values[key] = task_id
            return 1
        raise AssertionError("unexpected Lua script")


def _stub_publish(monkeypatch: MonkeyPatch) -> list[tuple[str, tuple[object, ...]]]:
    published: list[tuple[str, tuple[object, ...]]] = []

    def _publish(
        _task: Task,
        args: tuple[object, ...] | None = None,
        *,
        task_id: str | None = None,
        **_options: object,
    ) -> AsyncResult:
        assert task_id is not None
        published.append((task_id, args or ()))
        return AsyncResult(task_id)

    monkeypatch.setattr(Task, "apply_async", _publish)
    return published


def _stub_task_runtime(monkeypatch: MonkeyPatch) -> None:
    from src import tasks

    monkeypatch.setattr(tasks, "get_settings", lambda: type("S", (), {"log_level": "INFO"})())
    monkeypatch.setattr(tasks, "setup_logging", lambda _level: None)
    monkeypatch.setattr(tasks, "_initialize_graph_under_lock", lambda _requester: None)
    monkeypatch.setattr(tasks, "_acquire_source_lock", lambda *_args: _null_context("lock"))
    monkeypatch.setattr(tasks, "Neo4jClient", _Client)


@contextmanager
def _null_context(value: str) -> Iterator[str]:
    yield value


class _Client:
    def __init__(self, _settings: object) -> None:
        pass

    def close(self) -> None:
        pass


def test_phase_roots_are_deduplicated_independently(monkeypatch: MonkeyPatch) -> None:
    from src import knows_materialization_queue as queue
    from src import tasks

    redis_client = _FakeRedis()
    published = _stub_publish(monkeypatch)
    monkeypatch.setattr(queue, "_redis_client", lambda: redis_client)

    contacts = tasks.materialize_knows_task.apply_async(args=("contacts",))
    duplicate = tasks.materialize_knows_task.apply_async(args=("contacts",))
    chats = tasks.materialize_knows_task.apply_async(args=("chat_relationships",))

    assert contacts.id == duplicate.id
    assert chats.id != contacts.id
    assert [task_id for task_id, _args in published] == [contacts.id, chats.id]


def test_ambiguous_publishing_claim_is_not_replaced_by_elapsed_time(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import knows_materialization_queue as queue
    from src import tasks

    redis_client = _FakeRedis()
    key = queue._gate_key("contacts")
    redis_client.values[key] = "publishing|1|abandoned"
    published = _stub_publish(monkeypatch)
    monkeypatch.setattr(queue, "_redis_client", lambda: redis_client)

    redis_client.now = 10_000
    result = tasks.materialize_knows_task.apply_async(args=("contacts",), task_id="replacement")

    assert result.id == "abandoned"
    assert published == []
    assert redis_client.values[key] == "publishing|1|abandoned"


def test_keyword_phase_roots_use_the_same_deduplication_gate(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import knows_materialization_queue as queue
    from src import tasks

    redis_client = _FakeRedis()
    published = _stub_publish(monkeypatch)
    monkeypatch.setattr(queue, "_redis_client", lambda: redis_client)

    first = tasks.materialize_knows_task.apply_async(kwargs={"phase": "contacts"})
    duplicate = tasks.materialize_knows_task.apply_async(kwargs={"phase": "contacts"})

    assert first.id == duplicate.id
    assert [task_id for task_id, _args in published] == [first.id]


def test_same_id_retry_publishes_but_external_duplicate_does_not(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import knows_materialization_queue as queue
    from src import tasks

    redis_client = _FakeRedis()
    key = queue._gate_key("contacts")
    redis_client.values[key] = "task-1"
    published = _stub_publish(monkeypatch)
    monkeypatch.setattr(queue, "_redis_client", lambda: redis_client)

    duplicate = tasks.materialize_knows_task.apply_async(args=("contacts",), task_id="task-1")
    with queue.allow_knows_retry_publication():
        retried = tasks.materialize_knows_task.apply_async(args=("contacts",), task_id="task-1")

    assert duplicate.id == "task-1"
    assert retried.id == "task-1"
    assert published == [("task-1", ("contacts",))]


def test_successor_claims_predecessor_or_publishing_ownership(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import knows_materialization_queue as queue

    redis_client = _FakeRedis()
    monkeypatch.setattr(queue, "_redis_client", lambda: redis_client)
    key = queue._gate_key("contacts")
    redis_client.values[key] = "current"

    assert queue.claim_knows_materialization_gate("contacts", "next", "current")
    assert queue.claim_knows_materialization_gate("contacts", "next", "current")
    redis_client.values[key] = "publishing|1000|newer"
    assert queue.claim_knows_materialization_gate("contacts", "newer", "current")
    assert not queue.claim_knows_materialization_gate("contacts", "stale", "current")


def test_owner_checked_transfer_and_release_cannot_delete_successor(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import knows_materialization_queue as queue

    redis_client = _FakeRedis()
    monkeypatch.setattr(queue, "_redis_client", lambda: redis_client)
    key = queue._gate_key("contacts")
    redis_client.values[key] = "current"

    assert queue.transfer_knows_materialization_gate("contacts", "current", "next")
    assert not queue.release_knows_materialization_queue_gate("contacts", "current")
    assert queue.gate_owner("contacts") == "next"
    assert queue.release_knows_materialization_queue_gate("contacts", "next")
    assert key not in redis_client.values


def test_root_publish_failure_retains_ambiguous_gate(monkeypatch: MonkeyPatch) -> None:
    from src import knows_materialization_queue as queue
    from src import tasks

    redis_client = _FakeRedis()
    monkeypatch.setattr(queue, "_redis_client", lambda: redis_client)

    def _fail_publish(_task: Task, **_options: object) -> AsyncResult:
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(Task, "apply_async", _fail_publish)

    with raises(RuntimeError, match="broker unavailable"):
        tasks.materialize_knows_task.apply_async(args=("contacts",), task_id="root")

    value = redis_client.values[queue._gate_key("contacts")]
    assert value.startswith("publishing|")
    assert queue.gate_owner("contacts") == "root"


def test_parent_first_continuation_transfers_gate_and_publishes(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import knows_materialization_queue as queue
    from src import tasks

    redis_client = _FakeRedis()
    key = queue._gate_key("contacts")
    redis_client.values[key] = "parent"
    published = _stub_publish(monkeypatch)
    monkeypatch.setattr(queue, "_redis_client", lambda: redis_client)
    _stub_task_runtime(monkeypatch)
    monkeypatch.setattr(
        tasks,
        "materialize_knows_batch",
        lambda *_args, **_kwargs: {
            "phase": "contacts",
            "linked": 1,
            "scanned": 2,
            "next_cursor": "cursor-2",
        },
    )

    tasks.materialize_knows_task.push_request(id="parent", retries=0)
    try:
        result = tasks.materialize_knows_task.run("contacts")
    finally:
        tasks.materialize_knows_task.pop_request()

    assert result["next_cursor"] == "cursor-2"
    assert len(published) == 1
    successor, args = published[0]
    assert args == ("contacts", "cursor-2", "parent")
    assert redis_client.values[key] == successor


def test_successor_first_handoff_is_not_overwritten(monkeypatch: MonkeyPatch) -> None:
    from src import knows_materialization_queue as queue
    from src import tasks

    redis_client = _FakeRedis()
    key = queue._gate_key("contacts")
    redis_client.values[key] = "parent"
    monkeypatch.setattr(queue, "_redis_client", lambda: redis_client)
    _stub_task_runtime(monkeypatch)
    monkeypatch.setattr(
        tasks,
        "materialize_knows_batch",
        lambda *_args, **_kwargs: {
            "phase": "contacts",
            "linked": 1,
            "scanned": 1,
            "next_cursor": "cursor-1",
        },
    )
    successors: list[str] = []

    def _publish(
        _task: Task,
        args: tuple[object, ...],
        *,
        task_id: str,
        **_options: object,
    ) -> AsyncResult:
        successors.append(task_id)
        assert queue.claim_knows_materialization_gate("contacts", task_id, "parent")
        return AsyncResult(task_id)

    monkeypatch.setattr(Task, "apply_async", _publish)
    tasks.materialize_knows_task.push_request(id="parent", retries=0)
    try:
        tasks.materialize_knows_task.run("contacts")
    finally:
        tasks.materialize_knows_task.pop_request()

    assert redis_client.values[key] == successors[0]


def test_second_gate_validation_makes_delayed_delivery_a_noop(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    _stub_task_runtime(monkeypatch)
    claims = iter((True, False))
    monkeypatch.setattr(tasks, "claim_knows_materialization_gate", lambda *_args: next(claims))
    monkeypatch.setattr(
        tasks,
        "materialize_knows_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    tasks.materialize_knows_task.push_request(id="delayed", retries=0)
    try:
        result = tasks.materialize_knows_task.run("contacts")
    finally:
        tasks.materialize_knows_task.pop_request()

    assert result["linked"] == 0
    assert result["complete"] is False


def test_source_lock_collision_publishes_actual_same_id_retry(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import knows_materialization_queue as queue
    from src import tasks

    redis_client = _FakeRedis()
    redis_client.values[queue._gate_key("contacts")] = "retry-id"
    monkeypatch.setattr(queue, "_redis_client", lambda: redis_client)
    _stub_task_runtime(monkeypatch)

    @contextmanager
    def _busy(*_args: object) -> Iterator[str]:
        raise tasks._SourceAlreadyRunningError("knows-materialization:contacts")
        yield "unreachable"

    monkeypatch.setattr(tasks, "_acquire_source_lock", _busy)
    published = _stub_publish(monkeypatch)
    tasks.materialize_knows_task.push_request(
        id="retry-id",
        retries=0,
        called_directly=False,
        args=("contacts",),
        kwargs={},
        delivery_info={},
    )
    try:
        with raises(Retry):
            tasks.materialize_knows_task.run("contacts")
    finally:
        tasks.materialize_knows_task.pop_request()

    assert published == [("retry-id", ("contacts",))]
    assert redis_client.values[queue._gate_key("contacts")] == "retry-id"


def test_terminal_completion_strictly_releases_gate(monkeypatch: MonkeyPatch) -> None:
    from src import knows_materialization_queue as queue
    from src import tasks

    redis_client = _FakeRedis()
    key = queue._gate_key("contacts")
    redis_client.values[key] = "terminal"
    monkeypatch.setattr(queue, "_redis_client", lambda: redis_client)
    _stub_task_runtime(monkeypatch)
    monkeypatch.setattr(
        tasks,
        "materialize_knows_batch",
        lambda *_args, **_kwargs: {
            "phase": "contacts",
            "linked": 0,
            "scanned": 0,
            "next_cursor": None,
        },
    )

    tasks.materialize_knows_task.push_request(id="terminal", retries=0)
    try:
        result = tasks.materialize_knows_task.run("contacts")
    finally:
        tasks.materialize_knows_task.pop_request()

    assert result["complete"] is True
    assert key not in redis_client.values


def test_ordinary_failure_releases_gate(monkeypatch: MonkeyPatch) -> None:
    from src import knows_materialization_queue as queue
    from src import tasks

    redis_client = _FakeRedis()
    key = queue._gate_key("contacts")
    redis_client.values[key] = "failed"
    monkeypatch.setattr(queue, "_redis_client", lambda: redis_client)
    _stub_task_runtime(monkeypatch)
    monkeypatch.setattr(
        tasks,
        "materialize_knows_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("neo4j failed")),
    )

    def _unexpected_publish(_task: Task, **_options: object) -> AsyncResult:
        raise AssertionError("failed batches must not publish continuations")

    monkeypatch.setattr(Task, "apply_async", _unexpected_publish)

    tasks.materialize_knows_task.push_request(id="failed", retries=0)
    try:
        with raises(Reject, match="neo4j failed"):
            tasks.materialize_knows_task.run("contacts")
    finally:
        tasks.materialize_knows_task.pop_request()

    assert key not in redis_client.values


def test_ambiguous_continuation_publish_keeps_recoverable_successor_gate(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import knows_materialization_queue as queue
    from src import tasks

    redis_client = _FakeRedis()
    key = queue._gate_key("contacts")
    redis_client.values[key] = "parent"
    monkeypatch.setattr(queue, "_redis_client", lambda: redis_client)
    _stub_task_runtime(monkeypatch)
    monkeypatch.setattr(
        tasks,
        "materialize_knows_batch",
        lambda *_args, **_kwargs: {
            "phase": "contacts",
            "linked": 1,
            "scanned": 1,
            "next_cursor": "cursor-1",
        },
    )

    def _uncertain(_task: Task, **_options: object) -> AsyncResult:
        raise RuntimeError("publish outcome unknown")

    monkeypatch.setattr(Task, "apply_async", _uncertain)
    tasks.materialize_knows_task.push_request(id="parent", retries=0)
    try:
        with raises(Reject, match="publish outcome unknown"):
            tasks.materialize_knows_task.run("contacts")
    finally:
        tasks.materialize_knows_task.pop_request()

    value = redis_client.values[key]
    assert value.startswith("publishing|")
    assert queue.gate_owner("contacts") != "parent"


def test_ambiguous_root_publish_error_cannot_delete_a_running_owner(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import knows_materialization_queue as queue
    from src import tasks

    redis_client = _FakeRedis()
    key = queue._gate_key("contacts")
    monkeypatch.setattr(queue, "_redis_client", lambda: redis_client)

    def _accepted_then_lost(
        _task: Task,
        *,
        task_id: str,
        **_options: object,
    ) -> AsyncResult:
        assert queue.claim_knows_materialization_gate("contacts", task_id, None)
        raise RuntimeError("broker response lost")

    monkeypatch.setattr(Task, "apply_async", _accepted_then_lost)

    with raises(RuntimeError, match="broker response lost"):
        tasks.materialize_knows_task.apply_async(args=("contacts",), task_id="running")

    assert redis_client.values[key] == "running"


def test_retry_bypasses_redis_after_task_ownership_was_validated(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import knows_materialization_queue as queue
    from src import tasks

    redis_client = _FakeRedis()
    key = queue._gate_key("contacts")
    redis_client.values[key] = "retry-id"
    monkeypatch.setattr(queue, "_redis_client", lambda: redis_client)
    _stub_task_runtime(monkeypatch)
    published = _stub_publish(monkeypatch)

    @contextmanager
    def _busy(*_args: object) -> Iterator[str]:
        redis_client.fail = True
        raise tasks._SourceAlreadyRunningError("knows-materialization:contacts")
        yield "unreachable"

    monkeypatch.setattr(tasks, "_acquire_source_lock", _busy)
    tasks.materialize_knows_task.push_request(
        id="retry-id",
        retries=0,
        called_directly=False,
        args=("contacts",),
        kwargs={},
        delivery_info={},
    )
    try:
        with raises(Retry):
            tasks.materialize_knows_task.run("contacts")
    finally:
        tasks.materialize_knows_task.pop_request()

    assert published == [("retry-id", ("contacts",))]


def test_retry_publish_failure_requeues_current_delivery_without_clearing_gate(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import knows_materialization_queue as queue
    from src import tasks

    redis_client = _FakeRedis()
    key = queue._gate_key("contacts")
    redis_client.values[key] = "retry-id"
    monkeypatch.setattr(queue, "_redis_client", lambda: redis_client)
    _stub_task_runtime(monkeypatch)

    @contextmanager
    def _busy(*_args: object) -> Iterator[str]:
        raise tasks._SourceAlreadyRunningError("knows-materialization:contacts")
        yield "unreachable"

    def _broker_down(_task: Task, **_options: object) -> AsyncResult:
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(tasks, "_acquire_source_lock", _busy)
    monkeypatch.setattr(Task, "apply_async", _broker_down)
    tasks.materialize_knows_task.push_request(
        id="retry-id",
        retries=0,
        called_directly=False,
        args=("contacts",),
        kwargs={},
        delivery_info={},
    )
    try:
        with raises(Reject) as raised:
            tasks.materialize_knows_task.run("contacts")
    finally:
        tasks.materialize_knows_task.pop_request()

    assert raised.value.requeue is True
    assert redis_client.values[key] == "retry-id"
