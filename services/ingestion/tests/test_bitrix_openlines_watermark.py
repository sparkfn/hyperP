from __future__ import annotations

from datetime import UTC, datetime

from src.connectors.bitrix_openlines.watermark import RedisWatermarkStore


class StubRedis:
    def __init__(self) -> None:
        self.value: str | None = None

    def get(self, name: str) -> str | None:
        assert name == "profile_unifier:bitrix_openlines:watermark"
        return self.value

    def set(self, name: str, value: str) -> None:
        assert name == "profile_unifier:bitrix_openlines:watermark"
        self.value = value

    def close(self) -> None:
        return None


def test_watermark_reads_with_overlap_and_writes_utc() -> None:
    redis = StubRedis()
    redis.value = "2026-07-20T08:00:00+00:00"
    store = RedisWatermarkStore(redis)

    assert store.get(overlap_seconds=300) == datetime(2026, 7, 20, 7, 55, tzinfo=UTC)
    store.set(datetime(2026, 7, 20, 9, 0, tzinfo=UTC))
    assert redis.value == "2026-07-20T09:00:00+00:00"
