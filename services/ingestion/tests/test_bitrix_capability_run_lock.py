"""Tests for isolated Bitrix capability-run exclusion locking."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from src.connectors.bitrix_stage_history.capability_run_lock import (
    CAPABILITY_RUN_LOCK_KEY,
    CAPABILITY_RUN_LOCK_KEYS,
    LEGACY_BITRIX_SOURCE_LOCK_KEYS,
    CapabilityRunAlreadyActiveError,
    acquire_capability_run_lock,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}
        self.eval_calls: list[tuple[str, int, tuple[str, ...]]] = []

    def eval(self, script: str, numkeys: int, *keys_and_args: str) -> int:
        self.eval_calls.append((script, numkeys, keys_and_args))
        keys = keys_and_args[:numkeys]
        arguments = keys_and_args[numkeys:]
        if "exists" in script:
            owner_id, lease_seconds = arguments
            for index, lock_key in enumerate(keys, start=1):
                if lock_key in self.values:
                    return -index
            for lock_key in keys:
                self.values[lock_key] = owner_id
                self.expirations[lock_key] = int(lease_seconds)
            return 1
        owner_id = arguments[0]
        if "expire" in script:
            lease_seconds = arguments[1]
            for index, lock_key in enumerate(keys, start=1):
                if self.values.get(lock_key) != owner_id:
                    return -index
            for lock_key in keys:
                self.expirations[lock_key] = int(lease_seconds)
            return 1
        released = 0
        for lock_key in keys:
            if self.values.get(lock_key) == owner_id:
                del self.values[lock_key]
                self.expirations.pop(lock_key, None)
                released += 1
        return released


@contextmanager
def held_lock(client: FakeRedis, key: str, owner: str) -> Iterator[None]:
    client.values[key] = owner
    try:
        yield
    finally:
        client.values.pop(key, None)


def test_capability_lock_atomically_acquires_dedicated_and_all_legacy_bitrix_locks() -> None:
    client = FakeRedis()

    with acquire_capability_run_lock(client, owner_id="capability-run", lease_seconds=90) as lease:
        assert lease.owner_id == "capability-run"
        assert client.values == {key: "capability-run" for key in CAPABILITY_RUN_LOCK_KEYS}
        assert client.expirations == {key: 90 for key in CAPABILITY_RUN_LOCK_KEYS}
        acquire_script, key_count, arguments = client.eval_calls[0]
        assert "exists" in acquire_script
        assert key_count == len(CAPABILITY_RUN_LOCK_KEYS)
        assert arguments[:key_count] == CAPABILITY_RUN_LOCK_KEYS

    assert client.values == {}
    assert client.expirations == {}


@pytest.mark.parametrize("legacy_lock_key", LEGACY_BITRIX_SOURCE_LOCK_KEYS)
def test_capability_lock_refuses_every_normal_bitrix_source_or_mode_lock(
    legacy_lock_key: str,
) -> None:
    client = FakeRedis()

    with held_lock(client, legacy_lock_key, "normal-ingestion"):
        with pytest.raises(CapabilityRunAlreadyActiveError) as raised:
            with acquire_capability_run_lock(client, owner_id="capability-run"):
                pass
        assert client.values == {legacy_lock_key: "normal-ingestion"}

    assert raised.value.lock_key == legacy_lock_key
    assert client.values == {}
    assert len(client.eval_calls) == 1


def test_duplicate_capability_run_refuses_without_mutating_legacy_locks() -> None:
    client = FakeRedis()
    client.values[CAPABILITY_RUN_LOCK_KEY] = "other-capability-run"

    with pytest.raises(CapabilityRunAlreadyActiveError) as raised:
        with acquire_capability_run_lock(client, owner_id="candidate"):
            pass

    assert raised.value.lock_key == CAPABILITY_RUN_LOCK_KEY
    assert client.values == {CAPABILITY_RUN_LOCK_KEY: "other-capability-run"}
    assert len(client.eval_calls) == 1


def test_release_does_not_delete_a_replaced_lock_owner() -> None:
    client = FakeRedis()

    with acquire_capability_run_lock(client, owner_id="capability-run"):
        client.values[LEGACY_BITRIX_SOURCE_LOCK_KEYS[0]] = "replacement"
        client.values[CAPABILITY_RUN_LOCK_KEY] = "replacement"

    assert client.values == {
        CAPABILITY_RUN_LOCK_KEY: "replacement",
        LEGACY_BITRIX_SOURCE_LOCK_KEYS[0]: "replacement",
    }


def test_lease_renewal_requires_current_ownership_of_all_locks() -> None:
    client = FakeRedis()

    with acquire_capability_run_lock(client, owner_id="capability-run", lease_seconds=45) as lease:
        lease.renew()
        assert client.expirations == {key: 45 for key in CAPABILITY_RUN_LOCK_KEYS}
        client.values[LEGACY_BITRIX_SOURCE_LOCK_KEYS[-1]] = "replacement"
        with pytest.raises(RuntimeError, match="source:bitrix_chat:dump"):
            lease.renew()


@pytest.mark.parametrize("lease_seconds", [0, -1, True])
def test_capability_lock_rejects_invalid_lease_seconds(lease_seconds: int) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        with acquire_capability_run_lock(FakeRedis(), lease_seconds=lease_seconds):
            pass


def test_capability_lock_rejects_empty_owner_id() -> None:
    with pytest.raises(ValueError, match="owner_id"):
        with acquire_capability_run_lock(FakeRedis(), owner_id=""):
            pass
