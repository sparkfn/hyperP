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


def test_source_mode_lock_rejects_duplicate_without_releasing_active_lock(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    monkeypatch.setattr(tasks, "_redis_client", lambda: fake_redis)

    with tasks._acquire_source_lock("bitrix_chat:api"):
        with raises(tasks._SourceAlreadyRunningError):
            with tasks._acquire_source_lock("bitrix_chat:api"):
                pass

    assert fake_redis.values == {}


def test_source_mode_lock_allows_different_sources(monkeypatch: MonkeyPatch) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    monkeypatch.setattr(tasks, "_redis_client", lambda: fake_redis)

    with tasks._acquire_source_lock("bitrix_chat:api"):
        with tasks._acquire_source_lock("whatsapp_chat:api"):
            assert set(fake_redis.values) == {
                "profile_unifier:ingestion:source:bitrix_chat:api",
                "profile_unifier:ingestion:source:whatsapp_chat:api",
            }

    assert fake_redis.values == {}


def test_source_lock_keys_are_scoped_by_mode_and_whatsadmin_entity() -> None:
    from src import tasks

    assert tasks._source_lock_keys("whatsapp_chat", "api", "eko") == ("whatsapp_chat:api:eko",)
    assert tasks._source_lock_keys("whatsapp_chat", "api", "speedzone") == (
        "whatsapp_chat:api:speedzone",
    )
    assert tasks._source_lock_keys("whatsapp_chat", "api", None) == (
        "whatsapp_chat:api:eko",
        "whatsapp_chat:api:speedzone",
    )
    assert tasks._source_lock_keys("whatsapp_chat", "batch", None) == (
        "whatsapp_chat:batch:eko",
        "whatsapp_chat:batch:speedzone",
    )
    assert tasks._source_lock_keys("whatsapp_chat", "dump", None) == (
        "whatsapp_chat:dump:eko",
        "whatsapp_chat:dump:speedzone",
    )
    assert tasks._source_lock_keys("bitrix_chat", "api", None) == ("bitrix_chat:api",)
    assert tasks._source_lock_keys("bitrix_chat", "dump", None) == ("bitrix_chat:dump",)


def test_source_mode_locks_allow_same_source_in_different_modes(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    monkeypatch.setattr(tasks, "_redis_client", lambda: fake_redis)
    api_lock = tasks._source_lock_keys("bitrix_chat", "api", None)
    dump_lock = tasks._source_lock_keys("bitrix_chat", "dump", None)

    with tasks._acquire_source_locks(api_lock):
        with tasks._acquire_source_locks(dump_lock):
            assert set(fake_redis.values) == {
                "profile_unifier:ingestion:source:bitrix_chat:api",
                "profile_unifier:ingestion:source:bitrix_chat:dump",
            }


def test_entity_specific_whatsadmin_locks_can_run_concurrently(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    monkeypatch.setattr(tasks, "_redis_client", lambda: fake_redis)

    with tasks._acquire_source_locks(("whatsapp_chat:api:eko",)):
        with tasks._acquire_source_locks(("whatsapp_chat:api:speedzone",)):
            assert set(fake_redis.values) == {
                "profile_unifier:ingestion:source:whatsapp_chat:api:eko",
                "profile_unifier:ingestion:source:whatsapp_chat:api:speedzone",
            }

    assert fake_redis.values == {}


def test_whatsadmin_source_mode_locks_allow_same_entity_in_different_modes(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    monkeypatch.setattr(tasks, "_redis_client", lambda: fake_redis)
    api_locks = tasks._source_lock_keys("whatsapp_chat", "api", "eko")
    dump_locks = tasks._source_lock_keys("whatsapp_chat", "dump", None)

    with tasks._acquire_source_locks(api_locks):
        with tasks._acquire_source_locks(dump_locks):
            assert set(fake_redis.values) == {
                "profile_unifier:ingestion:source:whatsapp_chat:api:eko",
                "profile_unifier:ingestion:source:whatsapp_chat:dump:eko",
                "profile_unifier:ingestion:source:whatsapp_chat:dump:speedzone",
            }

    assert fake_redis.values == {}


def test_entity_specific_whatsadmin_lock_rejects_same_entity(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    monkeypatch.setattr(tasks, "_redis_client", lambda: fake_redis)

    with tasks._acquire_source_locks(("whatsapp_chat:api:eko",)):
        with raises(tasks._SourceAlreadyRunningError):
            with tasks._acquire_source_locks(("whatsapp_chat:api:eko",)):
                pass


def test_combined_whatsadmin_lock_conflicts_with_entity_runs(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    monkeypatch.setattr(tasks, "_redis_client", lambda: fake_redis)
    combined = ("whatsapp_chat:api:eko", "whatsapp_chat:api:speedzone")

    with tasks._acquire_source_locks(("whatsapp_chat:api:eko",)):
        with raises(tasks._SourceAlreadyRunningError):
            with tasks._acquire_source_locks(combined):
                pass

    with tasks._acquire_source_locks(combined):
        with raises(tasks._SourceAlreadyRunningError):
            with tasks._acquire_source_locks(("whatsapp_chat:api:speedzone",)):
                pass


def test_failed_combined_lock_releases_partially_acquired_lock(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    monkeypatch.setattr(tasks, "_redis_client", lambda: fake_redis)

    with tasks._acquire_source_locks(("whatsapp_chat:api:speedzone",)):
        with raises(tasks._SourceAlreadyRunningError):
            with tasks._acquire_source_locks(
                ("whatsapp_chat:api:eko", "whatsapp_chat:api:speedzone")
            ):
                pass
        assert set(fake_redis.values) == {
            "profile_unifier:ingestion:source:whatsapp_chat:api:speedzone"
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
            ("whatsapp_chat:api:eko", "eko-lock"),
            ("whatsapp_chat:api:speedzone", "speedzone-lock"),
        ),
    )

    assert renewed == [
        ("whatsapp_chat:api:eko", "eko-lock"),
        ("whatsapp_chat:api:speedzone", "speedzone-lock"),
    ]
