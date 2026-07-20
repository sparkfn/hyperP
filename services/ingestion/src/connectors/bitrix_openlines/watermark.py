"""Successful-run watermark storage for Bitrix Open Lines ingestion."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class RedisClient(Protocol):
    def get(self, name: str) -> object: ...
    def set(self, name: str, value: str) -> object: ...
    def close(self) -> None: ...


class RedisWatermarkStore:
    _KEY = "profile_unifier:bitrix_openlines:watermark"

    def __init__(self, redis: RedisClient) -> None:
        self._redis = redis

    def get(self, *, overlap_seconds: int) -> datetime | None:
        value = self._redis.get(self._KEY)
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode()
        if not isinstance(value, str):
            raise RuntimeError("Redis returned an invalid Bitrix Open Lines watermark")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise RuntimeError("Bitrix Open Lines watermark must be timezone-aware")
        return parsed.astimezone(UTC) - timedelta(seconds=overlap_seconds)

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("Bitrix Open Lines watermark must be timezone-aware")
        self._redis.set(self._KEY, value.astimezone(UTC).isoformat())

    def close(self) -> None:
        self._redis.close()
