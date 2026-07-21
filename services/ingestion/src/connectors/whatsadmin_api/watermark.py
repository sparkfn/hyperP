"""Per-session successful-run watermarks for WhatsAdmin API ingestion."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from src.connectors.whatsadmin_api.credentials import WhatsAdminEntity


class WatermarkStore(Protocol):
    def get(self, entity_key: WhatsAdminEntity, session_id: str) -> datetime | None: ...
    def set(
        self,
        entity_key: WhatsAdminEntity,
        session_id: str,
        value: datetime,
    ) -> None: ...
    def close(self) -> None: ...


class RedisClient(Protocol):
    def get(self, name: str) -> object: ...
    def set(self, name: str, value: str) -> object: ...
    def close(self) -> None: ...


class RedisWatermarkStore:
    def __init__(
        self,
        redis: RedisClient,
        legacy_entity: WhatsAdminEntity | None = None,
    ) -> None:
        self._redis = redis
        self._legacy_entity = legacy_entity

    def get(self, entity_key: WhatsAdminEntity, session_id: str) -> datetime | None:
        value = self._redis.get(self._key(entity_key, session_id))
        if value is not None:
            return self._parse(value)
        legacy_value = self._redis.get(self._legacy_key(session_id))
        if legacy_value is None:
            return None
        if self._legacy_entity is None:
            raise RuntimeError("Legacy WhatsAdmin watermark requires WHATSADMIN_LEGACY_ENTITY")
        if entity_key != self._legacy_entity:
            return None
        return self._parse(legacy_value)

    @staticmethod
    def _parse(value: object) -> datetime:
        if isinstance(value, bytes):
            value = value.decode()
        if not isinstance(value, str):
            raise RuntimeError("Redis returned an invalid WhatsAdmin watermark")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise RuntimeError("WhatsAdmin watermark must be timezone-aware")
        return parsed

    def set(
        self,
        entity_key: WhatsAdminEntity,
        session_id: str,
        value: datetime,
    ) -> None:
        if value.tzinfo is None:
            raise ValueError("WhatsAdmin watermark must be timezone-aware")
        self._redis.set(self._key(entity_key, session_id), value.isoformat())

    def close(self) -> None:
        self._redis.close()

    @staticmethod
    def _key(entity_key: WhatsAdminEntity, session_id: str) -> str:
        return f"profile_unifier:whatsadmin-api:whatsapp_chat:{entity_key}:{session_id}:watermark"

    @staticmethod
    def _legacy_key(session_id: str) -> str:
        return f"profile_unifier:whatsadmin-api:whatsapp_chat:{session_id}:watermark"
