"""Per-session successful-run watermarks for WhatsAdmin API ingestion."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class WatermarkStore(Protocol):
    def get(self, session_id: str) -> datetime | None: ...
    def set(self, session_id: str, value: datetime) -> None: ...
    def close(self) -> None: ...


class RedisClient(Protocol):
    def get(self, name: str) -> object: ...
    def set(self, name: str, value: str) -> object: ...
    def close(self) -> None: ...


class RedisWatermarkStore:
    def __init__(self, redis: RedisClient) -> None:
        self._redis = redis

    def get(self, session_id: str) -> datetime | None:
        value = self._redis.get(self._key(session_id))
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode()
        if not isinstance(value, str):
            raise RuntimeError("Redis returned an invalid WhatsAdmin watermark")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise RuntimeError("WhatsAdmin watermark must be timezone-aware")
        return parsed

    def set(self, session_id: str, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("WhatsAdmin watermark must be timezone-aware")
        self._redis.set(self._key(session_id), value.isoformat())

    def close(self) -> None:
        self._redis.close()

    @staticmethod
    def _key(session_id: str) -> str:
        return f"profile_unifier:whatsadmin-api:whatsapp_chat:{session_id}:watermark"
