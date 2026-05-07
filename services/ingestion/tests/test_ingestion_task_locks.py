"""Regression tests for ingestion task locking."""

from __future__ import annotations

from pytest import MonkeyPatch, raises


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, name: str, value: str, nx: bool, ex: int) -> bool:
        _ = ex
        if nx and name in self.values:
            return False
        self.values[name] = value
        return True

    def eval(self, script: str, numkeys: int, *keys_and_args: str) -> int:
        _ = script
        if numkeys != 1:
            return 0
        key, expected_value = keys_and_args
        if self.values.get(key) == expected_value:
            del self.values[key]
            return 1
        return 0


def test_source_lock_rejects_duplicate_without_releasing_active_lock(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    monkeypatch.setattr(tasks, "_redis_client", lambda: fake_redis)

    with tasks._acquire_source_lock("bitrix_chat"):
        with raises(tasks._SourceAlreadyRunningError):
            with tasks._acquire_source_lock("bitrix_chat"):
                pass

    assert fake_redis.values == {}


def test_source_lock_allows_different_sources(monkeypatch: MonkeyPatch) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    monkeypatch.setattr(tasks, "_redis_client", lambda: fake_redis)

    with tasks._acquire_source_lock("bitrix_chat"):
        with tasks._acquire_source_lock("whatsapp_chat"):
            assert set(fake_redis.values) == {
                "profile_unifier:ingestion:source:bitrix_chat",
                "profile_unifier:ingestion:source:whatsapp_chat",
            }

    assert fake_redis.values == {}
