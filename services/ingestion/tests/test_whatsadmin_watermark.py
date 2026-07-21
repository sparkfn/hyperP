from __future__ import annotations

from datetime import UTC, datetime

import pytest
from src.connectors.whatsadmin_api.watermark import RedisWatermarkStore


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.closed = False

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def close(self) -> None:
        self.closed = True


def test_watermarks_are_isolated_by_entity_and_session() -> None:
    redis = FakeRedis()
    store = RedisWatermarkStore(redis)
    first = datetime(2026, 7, 17, 5, 0, tzinfo=UTC)
    second = datetime(2026, 7, 17, 5, 5, tzinfo=UTC)
    third = datetime(2026, 7, 17, 5, 10, tzinfo=UTC)

    store.set("eko", "shared_session", first)
    store.set("speedzone", "shared_session", second)
    store.set("eko", "eko_second_session", third)

    assert store.get("eko", "shared_session") == first
    assert store.get("speedzone", "shared_session") == second
    assert store.get("eko", "eko_second_session") == third
    assert len(redis.values) == 3


def test_legacy_watermark_is_used_only_for_its_configured_entity() -> None:
    redis = FakeRedis()
    legacy = datetime(2026, 7, 16, 5, 0, tzinfo=UTC)
    redis.values["profile_unifier:whatsadmin-api:whatsapp_chat:shared_session:watermark"] = (
        legacy.isoformat()
    )
    store = RedisWatermarkStore(redis, legacy_entity="speedzone")

    assert store.get("eko", "shared_session") is None
    assert store.get("speedzone", "shared_session") == legacy


def test_legacy_watermark_without_configured_owner_fails_closed() -> None:
    redis = FakeRedis()
    redis.values["profile_unifier:whatsadmin-api:whatsapp_chat:ses_1:watermark"] = datetime(
        2026, 7, 16, 5, 0, tzinfo=UTC
    ).isoformat()
    store = RedisWatermarkStore(redis)

    with pytest.raises(RuntimeError, match="WHATSADMIN_LEGACY_ENTITY"):
        store.get("eko", "ses_1")


def test_entity_watermark_takes_precedence_over_legacy_watermark() -> None:
    redis = FakeRedis()
    legacy = datetime(2026, 7, 16, 5, 0, tzinfo=UTC)
    current = datetime(2026, 7, 17, 5, 0, tzinfo=UTC)
    redis.values["profile_unifier:whatsadmin-api:whatsapp_chat:ses_1:watermark"] = (
        legacy.isoformat()
    )
    store = RedisWatermarkStore(redis, legacy_entity="eko")
    store.set("eko", "ses_1", current)

    assert store.get("eko", "ses_1") == current


def test_watermark_rejects_naive_datetime() -> None:
    store = RedisWatermarkStore(FakeRedis())

    with pytest.raises(ValueError, match="timezone-aware"):
        store.set("eko", "ses_1", datetime(2026, 7, 17, 5, 0))
