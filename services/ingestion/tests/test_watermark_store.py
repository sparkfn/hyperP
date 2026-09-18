"""Unit tests for the watermark store (in-memory Redis fake)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from src.watermark_store import (
    IngestionWatermark,
    _redis_key,
    load_watermark,
    reset_watermark,
    save_watermark,
)


class FakeRedis:
    """Minimal in-memory stand-in for redis.Redis."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, name: str) -> bytes | str | None:
        return self._store.get(name)

    def set(self, name: str, value: str | bytes, **kwargs: object) -> object:
        self._store[name] = value.decode("utf-8") if isinstance(value, bytes) else value
        return True

    def delete(self, *names: str) -> object:
        for n in names:
            self._store.pop(n, None)
        return len(names)


def test_load_returns_none_when_absent() -> None:
    r = FakeRedis()
    wm = load_watermark(r, "fundbox")
    assert wm.updated_at is None
    assert wm.source_key == "fundbox"
    assert wm.entity_key is None


def test_save_and_load_roundtrip() -> None:
    r = FakeRedis()
    ts = datetime(2026, 9, 18, 10, 30, 0, tzinfo=UTC)
    wm = IngestionWatermark(updated_at=ts, source_key="fundbox", entity_key=None)
    save_watermark(r, wm)
    loaded = load_watermark(r, "fundbox")
    assert loaded.updated_at == ts
    assert loaded.source_key == "fundbox"


def test_save_rejects_none_updated_at() -> None:
    r = FakeRedis()
    wm = IngestionWatermark(updated_at=None, source_key="fundbox", entity_key=None)
    with pytest.raises(ValueError, match="Cannot save"):
        save_watermark(r, wm)


def test_reset_removes_watermark() -> None:
    r = FakeRedis()
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    wm = IngestionWatermark(updated_at=ts, source_key="fundbox", entity_key=None)
    save_watermark(r, wm)
    reset_watermark(r, "fundbox")
    loaded = load_watermark(r, "fundbox")
    assert loaded.updated_at is None


def test_entity_key_in_redis_key() -> None:
    assert _redis_key("fundbox", None) == "profile_unifier:watermark:fundbox"
    assert _redis_key("whatsapp_chat", "eko") == "profile_unifier:watermark:whatsapp_chat:eko"


def test_timezone_awareness() -> None:
    r = FakeRedis()
    ts = datetime(2026, 6, 15, 8, 0, 0, tzinfo=UTC)
    wm = IngestionWatermark(updated_at=ts, source_key="src", entity_key=None)
    save_watermark(r, wm)
    loaded = load_watermark(r, "src")
    assert loaded.updated_at is not None
    assert loaded.updated_at.tzinfo is not None
    assert loaded.updated_at == ts
