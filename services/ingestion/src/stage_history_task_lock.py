"""Redis contention lock for the dedicated CRM stage-history stream."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol, cast

import redis

_STAGE_HISTORY_LOCK_KEY = "profile_unifier:ingestion:source:bitrix_chat:crm_stage_history"
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""
_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""


class RedisLockClient(Protocol):
    def set(
        self,
        name: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> object: ...

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object: ...


@dataclass(slots=True)
class StageHistoryTaskLock:
    client: RedisLockClient
    owner: str
    lease_seconds: int = 3600
    renewal_seconds: int = 300
    _stop: threading.Event = field(init=False, repr=False)
    _lost: threading.Event = field(init=False, repr=False)
    _thread: threading.Thread | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.owner.strip():
            raise ValueError("stage-history lock owner must be non-empty")
        if self.lease_seconds < 2 or not 0 < self.renewal_seconds < self.lease_seconds:
            raise ValueError("stage-history lock renewal must be inside its positive lease")
        self._stop = threading.Event()
        self._lost = threading.Event()

    def acquire(self) -> bool:
        acquired = self.client.set(
            _STAGE_HISTORY_LOCK_KEY,
            self.owner,
            nx=True,
            ex=self.lease_seconds,
        )
        if acquired is not True:
            return False
        self._thread = threading.Thread(target=self._renew, daemon=True)
        self._thread.start()
        return True

    def assert_owned(self) -> None:
        if self._lost.is_set() or not self._renew_once():
            raise RuntimeError("stage-history contention lock was lost")

    def release(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=float(self.renewal_seconds) + 1.0)
        self.client.eval(_RELEASE_SCRIPT, 1, _STAGE_HISTORY_LOCK_KEY, self.owner)

    def _renew(self) -> None:
        while not self._stop.wait(self.renewal_seconds):
            if not self._renew_once():
                return

    def _renew_once(self) -> bool:
        renewed = self.client.eval(
            _RENEW_SCRIPT,
            1,
            _STAGE_HISTORY_LOCK_KEY,
            self.owner,
            self.lease_seconds,
        )
        if renewed == 1:
            return True
        self._lost.set()
        return False


@contextmanager
def stage_history_task_lock(
    broker_url: str,
    *,
    owner: str,
) -> Iterator[StageHistoryTaskLock]:
    client = cast(
        RedisLockClient,
        redis.Redis.from_url(broker_url, decode_responses=True),
    )
    lock = StageHistoryTaskLock(client, owner)
    if not lock.acquire():
        raise RuntimeError("crm_stage_history is already running")
    try:
        yield lock
    finally:
        lock.release()
