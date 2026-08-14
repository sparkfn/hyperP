"""Contention-lock behavior for the dedicated stage-history stream."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from src.stage_history_task_lock import StageHistoryTaskLock, stage_history_task_lock


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiries: dict[str, int] = {}

    def set(
        self,
        name: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> object:
        if nx and name in self.values:
            return None
        self.values[name] = value
        if ex is not None:
            self.expiries[name] = ex
        return True

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        assert numkeys == 1
        key = str(keys_and_args[0])
        owner = str(keys_and_args[1])
        if self.values.get(key) != owner:
            return 0
        if "expire" in script:
            self.expiries[key] = int(keys_and_args[2])
            return 1
        del self.values[key]
        self.expiries.pop(key, None)
        return 1


def test_lock_acquires_once_and_rejects_contention() -> None:
    client = _FakeRedis()
    first = StageHistoryTaskLock(client, "task-a")
    second = StageHistoryTaskLock(client, "task-b")

    assert first.acquire() is True
    assert second.acquire() is False

    first.release()
    assert second.acquire() is True
    second.release()


def test_lock_detects_owner_replacement_during_renewal() -> None:
    client = _FakeRedis()
    lock = StageHistoryTaskLock(client, "task-a")
    assert lock.acquire() is True
    key = next(iter(client.values))
    client.values[key] = "task-b"

    with pytest.raises(RuntimeError, match="was lost"):
        lock.assert_owned()

    lock.release()
    assert client.values[key] == "task-b"


def test_release_never_deletes_another_owner() -> None:
    client = _FakeRedis()
    lock = StageHistoryTaskLock(client, "task-a")
    assert lock.acquire() is True
    key = next(iter(client.values))
    client.values[key] = "replacement"

    lock.release()

    assert client.values[key] == "replacement"


def test_context_manager_releases_owned_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeRedis()

    @contextmanager
    def fake_from_url(*_args: object, **_kwargs: object) -> Iterator[_FakeRedis]:
        yield client

    del fake_from_url
    monkeypatch.setattr(
        "src.stage_history_task_lock.redis.Redis.from_url",
        lambda *_args, **_kwargs: client,
    )

    with stage_history_task_lock("redis://unused", owner="task-a") as lock:
        lock.assert_owned()
        assert client.values

    assert client.values == {}
