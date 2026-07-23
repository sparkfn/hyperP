"""Persisted cache of resolved Open Lines dialog metadata for unselected chats.

The backfill scans tens of thousands of CRM IMOPENLINES_SESSION activities and,
historically, called ``im.dialog.get`` per chat only to discard nearly all of
them because their Open Lines configuration was not selected. Caching the
resolved configuration for chats that were found to be unselected lets later
runs and resumptions skip that rate-limited lookup entirely.
"""

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

from src.connectors.bitrix_openlines.models import DialogMetadata


class RedisHashClient(Protocol):
    def hget(self, name: str, key: str) -> object: ...
    def hset(self, name: str, key: str, value: str) -> object: ...
    def delete(self, *names: str) -> object: ...
    def close(self) -> None: ...


@runtime_checkable
class DialogConfigCache(Protocol):
    def get(self, chat_id: int) -> DialogMetadata | None: ...
    def set(self, chat_id: int, dialog: DialogMetadata) -> None: ...
    def close(self) -> None: ...


class RedisDialogConfigCache:
    _KEY = "profile_unifier:bitrix_openlines:dialog_config"

    def __init__(self, redis: RedisHashClient) -> None:
        self._redis = redis

    def get(self, chat_id: int) -> DialogMetadata | None:
        value = self._redis.hget(self._KEY, str(chat_id))
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode()
        if not isinstance(value, str):
            raise RuntimeError("Redis returned an invalid Bitrix dialog cache entry")
        parsed: object = json.loads(value)
        if not isinstance(parsed, dict):
            raise RuntimeError("Redis returned an invalid Bitrix dialog cache entry")
        config_id = parsed.get("config_id")
        connector_id = parsed.get("connector_id")
        if not isinstance(config_id, str) or not isinstance(connector_id, str):
            raise RuntimeError("Redis returned an invalid Bitrix dialog cache entry")
        return DialogMetadata(chat_id, config_id, connector_id)

    def set(self, chat_id: int, dialog: DialogMetadata) -> None:
        self._redis.hset(
            self._KEY,
            str(chat_id),
            json.dumps(
                {"config_id": dialog.config_id, "connector_id": dialog.connector_id},
                separators=(",", ":"),
            ),
        )

    def close(self) -> None:
        self._redis.close()
