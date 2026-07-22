"""Successful-run watermark storage for Bitrix Open Lines ingestion."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


class RedisClient(Protocol):
    def get(self, name: str) -> object: ...
    def set(self, name: str, value: str) -> object: ...
    def delete(self, *names: str) -> object: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class BackfillCheckpoint:
    crm_start: int | None


@runtime_checkable
class BackfillCheckpointStore(Protocol):
    def get_backfill_checkpoint(self) -> BackfillCheckpoint | None: ...
    def set_backfill_checkpoint(self, checkpoint: BackfillCheckpoint) -> None: ...
    def clear_backfill_checkpoint(self) -> None: ...


class RedisWatermarkStore:
    _KEY = "profile_unifier:bitrix_openlines:watermark"
    _BACKFILL_KEY = "profile_unifier:bitrix_openlines:backfill"

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

    def get_backfill_checkpoint(self) -> BackfillCheckpoint | None:
        value = self._redis.get(self._BACKFILL_KEY)
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode()
        if not isinstance(value, str):
            raise RuntimeError("Redis returned an invalid Bitrix backfill checkpoint")
        parsed: object = json.loads(value)
        if not isinstance(parsed, dict):
            raise RuntimeError("Redis returned an invalid Bitrix backfill checkpoint")
        crm_start = parsed.get("crm_start")
        if crm_start is not None and (
            not isinstance(crm_start, int) or isinstance(crm_start, bool)
        ):
            raise RuntimeError("Bitrix backfill checkpoint cursor must be an integer")
        return BackfillCheckpoint(crm_start=crm_start)

    def set_backfill_checkpoint(self, checkpoint: BackfillCheckpoint) -> None:
        self._redis.set(
            self._BACKFILL_KEY,
            json.dumps({"crm_start": checkpoint.crm_start}, separators=(",", ":")),
        )

    def clear_backfill_checkpoint(self) -> None:
        self._redis.delete(self._BACKFILL_KEY)

    def close(self) -> None:
        self._redis.close()
