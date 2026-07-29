from __future__ import annotations

from datetime import UTC, datetime

import pytest
from src.connectors.whatsadmin_api import watermark
from src.connectors.whatsadmin_api.watermark import RedisWatermarkStore


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.closed = False

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)

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


def test_page_checkpoint_round_trip_and_delete() -> None:
    redis = FakeRedis()
    store = RedisWatermarkStore(redis)
    checkpoint = watermark.PageCheckpoint(
        changed_since="2026-07-16T05:00:00+00:00",
        cursor="opaque-next",
        snapshot_at=datetime(2026, 7, 17, 5, 0, tzinfo=UTC),
        complete=False,
    )

    store.set_checkpoint("eko", "ses_1", checkpoint)

    assert store.get_checkpoint("eko", "ses_1") == checkpoint
    store.delete_checkpoint("eko", "ses_1")
    assert store.get_checkpoint("eko", "ses_1") is None


def test_extraction_retries_round_trip_and_clear() -> None:
    redis = FakeRedis()
    store = RedisWatermarkStore(redis)
    retries = [
        {
            "chat_id": "chat-1",
            "session_id": "ses_1",
            "entity_key": "eko",
            "msg_text": "private transcript",
            "participants": [],
            "message_endpoints": [],
            "chat_name": "Customer",
            "whatsapp_user_id": "6590000000@c.us",
            "observed_at": "2026-07-17T05:20:00+00:00",
            "failure_code": "malformed_response",
            "attempts": 3,
            "session_phone": None,
            "source_id_scope": "eko-ses_1",
        }
    ]

    store.set_extraction_retries("eko", "ses_1", retries)

    assert store.get_extraction_retries("eko", "ses_1") == retries
    store.set_extraction_retries("eko", "ses_1", [])
    assert store.get_extraction_retries("eko", "ses_1") == []


def test_extraction_retries_reject_malformed_redis_json() -> None:
    redis = FakeRedis()
    redis.values["profile_unifier:whatsadmin-api:whatsapp_chat:eko:ses_1:retries"] = "{"
    store = RedisWatermarkStore(redis)

    with pytest.raises(RuntimeError, match="invalid WhatsAdmin extraction retries"):
        store.get_extraction_retries("eko", "ses_1")
