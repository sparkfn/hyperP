"""Redis exclusion lock for a read-only Bitrix capability re-gate run.

The helper atomically reserves a dedicated capability lock plus the complete
legacy ``bitrix_chat`` source-lock set used by normal ingestion. This prevents a
capability census from overlapping a rolling-upgrade combined Bitrix task. It has
no dependency on Celery, ingestion tasks, graph repositories, or Bitrix clients.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

CAPABILITY_RUN_LOCK_KEY = "profile_unifier:ingestion:bitrix-capability"
LEGACY_BITRIX_SOURCE_LOCK_KEYS = (
    "profile_unifier:ingestion:source:bitrix_chat",
    "profile_unifier:ingestion:source:bitrix_chat:api",
    "profile_unifier:ingestion:source:bitrix_chat:backfill",
    "profile_unifier:ingestion:source:bitrix_chat:batch",
    "profile_unifier:ingestion:source:bitrix_chat:dump",
)
CAPABILITY_RUN_LOCK_KEYS = (CAPABILITY_RUN_LOCK_KEY, *LEGACY_BITRIX_SOURCE_LOCK_KEYS)
DEFAULT_CAPABILITY_RUN_LOCK_LEASE_SECONDS = 60 * 60

_ACQUIRE_ALL_IF_FREE_SCRIPT = """
for index, lock_key in ipairs(KEYS) do
    if redis.call('exists', lock_key) == 1 then
        return -index
    end
end
for _, lock_key in ipairs(KEYS) do
    redis.call('set', lock_key, ARGV[1], 'EX', ARGV[2])
end
return 1
"""
_RELEASE_ALL_IF_OWNER_SCRIPT = """
local released = 0
for _, lock_key in ipairs(KEYS) do
    if redis.call('get', lock_key) == ARGV[1] then
        released = released + redis.call('del', lock_key)
    end
end
return released
"""
_RENEW_ALL_IF_OWNER_SCRIPT = """
for index, lock_key in ipairs(KEYS) do
    if redis.call('get', lock_key) ~= ARGV[1] then
        return -index
    end
end
for _, lock_key in ipairs(KEYS) do
    redis.call('expire', lock_key, ARGV[2])
end
return 1
"""


class CapabilityRunLockClient(Protocol):
    """Minimal Redis boundary required by the capability-run exclusion helper."""

    def eval(self, script: str, numkeys: int, *keys_and_args: str) -> int: ...


class CapabilityRunAlreadyActiveError(RuntimeError):
    """Raised when a capability or normal Bitrix ingestion lock is already held."""

    def __init__(self, lock_key: str) -> None:
        super().__init__(f"Bitrix capability run lock is already held: {lock_key}")
        self.lock_key = lock_key


@dataclass(frozen=True)
class CapabilityRunLockLease:
    """Owned capability and legacy-lock set, with explicit lease renewal support."""

    _client: CapabilityRunLockClient
    owner_id: str
    lease_seconds: int

    def renew(self) -> None:
        """Renew every exclusion only when this invocation still owns all of them."""
        result = self._client.eval(
            _RENEW_ALL_IF_OWNER_SCRIPT,
            len(CAPABILITY_RUN_LOCK_KEYS),
            *CAPABILITY_RUN_LOCK_KEYS,
            self.owner_id,
            str(self.lease_seconds),
        )
        if result != 1:
            raise RuntimeError(f"Bitrix capability run lock was lost: {_failed_lock_key(result)}")


def _validate_lease_seconds(lease_seconds: int) -> None:
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 1:
        raise ValueError("lease_seconds must be a positive integer")


def _validate_owner_id(owner_id: str | None) -> str:
    if owner_id is None:
        return uuid.uuid4().hex
    if not owner_id:
        raise ValueError("owner_id must be non-empty when provided")
    return owner_id


def _failed_lock_key(result: int) -> str:
    index = -result
    if 1 <= index <= len(CAPABILITY_RUN_LOCK_KEYS):
        return CAPABILITY_RUN_LOCK_KEYS[index - 1]
    return "unknown"


def _release_all_if_owned(client: CapabilityRunLockClient, owner_id: str) -> None:
    client.eval(
        _RELEASE_ALL_IF_OWNER_SCRIPT,
        len(CAPABILITY_RUN_LOCK_KEYS),
        *CAPABILITY_RUN_LOCK_KEYS,
        owner_id,
    )


@contextmanager
def acquire_capability_run_lock(
    client: CapabilityRunLockClient,
    *,
    owner_id: str | None = None,
    lease_seconds: int = DEFAULT_CAPABILITY_RUN_LOCK_LEASE_SECONDS,
) -> Iterator[CapabilityRunLockLease]:
    """Acquire no-wait exclusions for a bounded, read-only capability run.

    A single Redis script checks and reserves the dedicated capability key and
    every normal ``bitrix_chat`` source/mode key. Therefore a normal task cannot
    acquire a partially overlapping lock set between individual lock operations.
    The helper raises rather than queues when any incompatible run is active.
    """
    _validate_lease_seconds(lease_seconds)
    lock_owner = _validate_owner_id(owner_id)
    result = client.eval(
        _ACQUIRE_ALL_IF_FREE_SCRIPT,
        len(CAPABILITY_RUN_LOCK_KEYS),
        *CAPABILITY_RUN_LOCK_KEYS,
        lock_owner,
        str(lease_seconds),
    )
    if result != 1:
        raise CapabilityRunAlreadyActiveError(_failed_lock_key(result))
    try:
        yield CapabilityRunLockLease(client, lock_owner, lease_seconds)
    finally:
        _release_all_if_owned(client, lock_owner)
