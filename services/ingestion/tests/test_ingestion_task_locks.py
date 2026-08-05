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

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def eval(self, script: str, numkeys: int, *keys_and_args: str) -> int:
        if numkeys != 1:
            return 0
        key, expected_value, *rest = keys_and_args
        if self.values.get(key) == expected_value:
            if "expire" not in script:
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


def test_source_lock_keys_are_scoped_by_source_and_whatsadmin_entity() -> None:
    from src import tasks

    assert tasks._source_lock_keys("whatsapp_chat", "api", "eko") == (
        "whatsapp_chat:eko",
        "whatsapp_chat:api:eko",
        "whatsapp_chat:backfill:eko",
        "whatsapp_chat:batch:eko",
        "whatsapp_chat:dump:eko",
    )
    assert tasks._source_lock_keys("whatsapp_chat", "api", "speedzone") == (
        "whatsapp_chat:speedzone",
        "whatsapp_chat:api:speedzone",
        "whatsapp_chat:backfill:speedzone",
        "whatsapp_chat:batch:speedzone",
        "whatsapp_chat:dump:speedzone",
    )
    all_entities = tasks._source_lock_keys("whatsapp_chat", "api", None)
    assert all_entities[:5] == tasks._source_lock_keys("whatsapp_chat", "api", "eko")
    assert all_entities[5:] == tasks._source_lock_keys("whatsapp_chat", "api", "speedzone")
    expected_bitrix = (
        "bitrix_chat",
        "bitrix_chat:api",
        "bitrix_chat:backfill",
        "bitrix_chat:batch",
        "bitrix_chat:dump",
    )
    assert tasks._source_lock_keys("bitrix_chat", "api", None) == expected_bitrix
    assert tasks._source_lock_keys("bitrix_chat", "dump", None) == expected_bitrix


def test_source_scopes_conflict_with_legacy_mode_locks_during_rolling_upgrade(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    monkeypatch.setattr(tasks, "_redis_client", lambda: fake_redis)

    with tasks._acquire_source_lock("bitrix_chat:dump"):
        with raises(tasks._SourceAlreadyRunningError):
            with tasks._acquire_source_locks(tasks._source_lock_keys("bitrix_chat", "api", None)):
                pass

    assert fake_redis.values == {}


def test_source_locks_reject_same_source_in_different_modes(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    monkeypatch.setattr(tasks, "_redis_client", lambda: fake_redis)
    api_lock = tasks._source_lock_keys("bitrix_chat", "api", None)
    dump_lock = tasks._source_lock_keys("bitrix_chat", "dump", None)

    with tasks._acquire_source_locks(api_lock):
        with raises(tasks._SourceAlreadyRunningError):
            with tasks._acquire_source_locks(dump_lock):
                pass


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


def test_whatsadmin_source_locks_reject_same_entity_in_different_modes(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    monkeypatch.setattr(tasks, "_redis_client", lambda: fake_redis)
    api_locks = tasks._source_lock_keys("whatsapp_chat", "api", "eko")
    dump_locks = tasks._source_lock_keys("whatsapp_chat", "dump", None)

    with tasks._acquire_source_locks(api_locks):
        with raises(tasks._SourceAlreadyRunningError):
            with tasks._acquire_source_locks(dump_locks):
                pass

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


def test_same_celery_task_owner_is_identified_for_redelivery(monkeypatch: MonkeyPatch) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    monkeypatch.setattr(tasks, "_redis_client", lambda: fake_redis)

    with tasks._acquire_source_lock("bitrix_chat", "task-123"):
        with raises(tasks._SourceAlreadyRunningError) as raised:
            with tasks._acquire_source_lock("bitrix_chat", "task-123"):
                pass

    assert raised.value.held_by_same_task is True


def test_source_lock_retries_when_lease_expires_between_set_and_get(
    monkeypatch: MonkeyPatch,
) -> None:
    from src import tasks

    class ExpiringRedis(_FakeRedis):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def set(self, name: str, value: str, nx: bool, ex: int) -> bool:
            self.attempts += 1
            if self.attempts == 1:
                return False
            return super().set(name, value, nx, ex)

    fake_redis = ExpiringRedis()
    monkeypatch.setattr(tasks, "_redis_client", lambda: fake_redis)

    with tasks._acquire_source_lock("bitrix_chat", "task-123"):
        assert fake_redis.attempts == 2

    assert fake_redis.values == {}


def test_init_lock_renewal_requires_current_owner(monkeypatch: MonkeyPatch) -> None:
    from src import tasks

    fake_redis = _FakeRedis()
    fake_redis.values[tasks._INIT_LOCK_KEY] = "init-owner"
    monkeypatch.setattr(tasks, "_redis_client", lambda: fake_redis)

    tasks._renew_init_lock(fake_redis, "init-owner")

    with raises(RuntimeError, match="initialization lock"):
        tasks._renew_init_lock(fake_redis, "different-owner")
