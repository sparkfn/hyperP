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


def test_watermarks_are_isolated_by_session() -> None:
    redis = FakeRedis()
    store = RedisWatermarkStore(redis)
    first = datetime(2026, 7, 17, 5, 0, tzinfo=UTC)
    second = datetime(2026, 7, 17, 5, 5, tzinfo=UTC)

    store.set("ses_1", first)
    store.set("ses_2", second)

    assert store.get("ses_1") == first
    assert store.get("ses_2") == second


def test_watermark_rejects_naive_datetime() -> None:
    store = RedisWatermarkStore(FakeRedis())

    with pytest.raises(ValueError, match="timezone-aware"):
        store.set("ses_1", datetime(2026, 7, 17, 5, 0))
