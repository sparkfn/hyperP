from __future__ import annotations

from datetime import UTC, datetime

from src.connectors.bitrix_openlines.watermark import BackfillCheckpoint, RedisWatermarkStore


class StubRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)

    def close(self) -> None:
        return None


def test_watermark_reads_with_overlap_and_writes_utc() -> None:
    redis = StubRedis()
    redis.values["profile_unifier:bitrix_openlines:watermark"] = (
        "2026-07-20T08:00:00+00:00"
    )
    store = RedisWatermarkStore(redis)

    assert store.get(overlap_seconds=300) == datetime(2026, 7, 20, 7, 55, tzinfo=UTC)
    store.set(datetime(2026, 7, 20, 9, 0, tzinfo=UTC))
    assert redis.values["profile_unifier:bitrix_openlines:watermark"] == (
        "2026-07-20T09:00:00+00:00"
    )


def test_backfill_checkpoint_round_trip_and_clear() -> None:
    redis = StubRedis()
    store = RedisWatermarkStore(redis)

    store.set_backfill_checkpoint(BackfillCheckpoint(crm_start=150))

    assert store.get_backfill_checkpoint() == BackfillCheckpoint(crm_start=150)
    store.clear_backfill_checkpoint()
    assert store.get_backfill_checkpoint() is None
