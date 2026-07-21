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


def test_whatsadmin_source_lock_keys_are_scoped_by_entity() -> None:
    from src import tasks

    assert tasks._source_lock_keys("whatsapp_chat", "api", "eko") == ("whatsapp_chat:eko",)
    assert tasks._source_lock_keys("whatsapp_chat", "api", "speedzone") == (
        "whatsapp_chat:speedzone",
    )
    assert tasks._source_lock_keys("whatsapp_chat", "api", None) == (
        "whatsapp_chat:eko",
        "whatsapp_chat:speedzone",
    )
    assert tasks._source_lock_keys("whatsapp_chat", "batch", None) == (
        "whatsapp_chat:eko",
        "whatsapp_chat:speedzone",
    )
    assert tasks._source_lock_keys("whatsapp_chat", "dump", None) == (
        "whatsapp_chat:eko",
        "whatsapp_chat:speedzone",
    )
    assert tasks._source_lock_keys("bitrix_chat", "api", None) == ("bitrix_chat",)


def test_entity_specific_whatsadmin_locks_can_run_concurrently(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    monkeypatch.setattr(tasks, "_redis_client", lambda: fake_redis)

    with tasks._acquire_source_locks(("whatsapp_chat:eko",)):
        with tasks._acquire_source_locks(("whatsapp_chat:speedzone",)):
            assert set(fake_redis.values) == {
                "profile_unifier:ingestion:source:whatsapp_chat:eko",
                "profile_unifier:ingestion:source:whatsapp_chat:speedzone",
            }

    assert fake_redis.values == {}


def test_entity_specific_whatsadmin_lock_rejects_same_entity(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    monkeypatch.setattr(tasks, "_redis_client", lambda: fake_redis)

    with tasks._acquire_source_locks(("whatsapp_chat:eko",)):
        with raises(tasks._SourceAlreadyRunningError):
            with tasks._acquire_source_locks(("whatsapp_chat:eko",)):
                pass


def test_combined_whatsadmin_lock_conflicts_with_entity_runs(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    monkeypatch.setattr(tasks, "_redis_client", lambda: fake_redis)
    combined = ("whatsapp_chat:eko", "whatsapp_chat:speedzone")

    with tasks._acquire_source_locks(("whatsapp_chat:eko",)):
        with raises(tasks._SourceAlreadyRunningError):
            with tasks._acquire_source_locks(combined):
                pass

    with tasks._acquire_source_locks(combined):
        with raises(tasks._SourceAlreadyRunningError):
            with tasks._acquire_source_locks(("whatsapp_chat:speedzone",)):
                pass


def test_failed_combined_lock_releases_partially_acquired_lock(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    monkeypatch.setattr(tasks, "_redis_client", lambda: fake_redis)

    with tasks._acquire_source_locks(("whatsapp_chat:speedzone",)):
        with raises(tasks._SourceAlreadyRunningError):
            with tasks._acquire_source_locks(("whatsapp_chat:eko", "whatsapp_chat:speedzone")):
                pass
        assert set(fake_redis.values) == {
            "profile_unifier:ingestion:source:whatsapp_chat:speedzone"
        }


def test_all_source_locks_are_renewed(monkeypatch: MonkeyPatch) -> None:
    from src import tasks

    renewed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tasks,
        "_renew_source_lock",
        lambda _client, source_key, lock_id: renewed.append((source_key, lock_id)),
    )

    tasks._renew_source_locks(
        _FakeRedis(),
        (
            ("whatsapp_chat:eko", "eko-lock"),
            ("whatsapp_chat:speedzone", "speedzone-lock"),
        ),
    )

    assert renewed == [
        ("whatsapp_chat:eko", "eko-lock"),
        ("whatsapp_chat:speedzone", "speedzone-lock"),
    ]
