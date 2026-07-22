"""Per-session successful-run watermarks for WhatsAdmin API ingestion."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

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


@runtime_checkable
class PageCheckpointStore(Protocol):
    def get_checkpoint(
        self,
        entity_key: WhatsAdminEntity,
        session_id: str,
    ) -> PageCheckpoint | None: ...
    def set_checkpoint(
        self,
        entity_key: WhatsAdminEntity,
        session_id: str,
        checkpoint: PageCheckpoint,
    ) -> None: ...
    def delete_checkpoint(self, entity_key: WhatsAdminEntity, session_id: str) -> None: ...


class RedisClient(Protocol):
    def get(self, name: str) -> object: ...
    def set(self, name: str, value: str) -> object: ...
    def delete(self, *names: str) -> object: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class PageCheckpoint:
    changed_since: str | None
    cursor: str | None
    snapshot_at: datetime
    complete: bool


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

    def get_checkpoint(
        self,
        entity_key: WhatsAdminEntity,
        session_id: str,
    ) -> PageCheckpoint | None:
        value = self._redis.get(self._checkpoint_key(entity_key, session_id))
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode()
        if not isinstance(value, str):
            raise RuntimeError("Redis returned an invalid WhatsAdmin page checkpoint")
        parsed: object = json.loads(value)
        if not isinstance(parsed, dict):
            raise RuntimeError("Redis returned an invalid WhatsAdmin page checkpoint")
        changed_since = parsed.get("changed_since")
        cursor = parsed.get("cursor")
        snapshot_text = parsed.get("snapshot_at")
        complete = parsed.get("complete")
        if changed_since is not None and not isinstance(changed_since, str):
            raise RuntimeError("WhatsAdmin checkpoint changed_since must be text")
        if cursor is not None and not isinstance(cursor, str):
            raise RuntimeError("WhatsAdmin checkpoint cursor must be text")
        if not isinstance(snapshot_text, str) or not isinstance(complete, bool):
            raise RuntimeError("WhatsAdmin checkpoint omitted required fields")
        snapshot_at = datetime.fromisoformat(snapshot_text)
        if snapshot_at.tzinfo is None:
            raise RuntimeError("WhatsAdmin checkpoint snapshot must be timezone-aware")
        return PageCheckpoint(changed_since, cursor, snapshot_at, complete)

    def set_checkpoint(
        self,
        entity_key: WhatsAdminEntity,
        session_id: str,
        checkpoint: PageCheckpoint,
    ) -> None:
        if checkpoint.snapshot_at.tzinfo is None:
            raise ValueError("WhatsAdmin checkpoint snapshot must be timezone-aware")
        payload = json.dumps(
            {
                "changed_since": checkpoint.changed_since,
                "cursor": checkpoint.cursor,
                "snapshot_at": checkpoint.snapshot_at.isoformat(),
                "complete": checkpoint.complete,
            },
            separators=(",", ":"),
        )
        self._redis.set(self._checkpoint_key(entity_key, session_id), payload)

    def delete_checkpoint(self, entity_key: WhatsAdminEntity, session_id: str) -> None:
        self._redis.delete(self._checkpoint_key(entity_key, session_id))

    def close(self) -> None:
        self._redis.close()

    @staticmethod
    def _key(entity_key: WhatsAdminEntity, session_id: str) -> str:
        return f"profile_unifier:whatsadmin-api:whatsapp_chat:{entity_key}:{session_id}:watermark"

    @staticmethod
    def _legacy_key(session_id: str) -> str:
        return f"profile_unifier:whatsadmin-api:whatsapp_chat:{session_id}:watermark"

    @staticmethod
    def _checkpoint_key(entity_key: WhatsAdminEntity, session_id: str) -> str:
        return f"profile_unifier:whatsadmin-api:whatsapp_chat:{entity_key}:{session_id}:page"
