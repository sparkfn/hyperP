"""Per-source Fundbox API checkpoint serialization."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Protocol

_PREFIX = "profile_unifier:fundbox_api:watermark"
_SOURCE_IDS_PREFIX = "profile_unifier:fundbox_api:source_ids"


class CheckpointPipeline(Protocol):
    def set(self, key: str, value: str) -> object: ...
    def execute(self) -> object: ...


class CheckpointStore(Protocol):
    def get(self, key: str) -> object: ...
    def set(self, key: str, value: str) -> object: ...
    def pipeline(self, *, transaction: bool) -> CheckpointPipeline: ...


def load_watermark(redis: CheckpointStore, source_key: str, overlap_seconds: int) -> str | None:
    raw = redis.get(f"{_PREFIX}:{source_key}")
    if raw is None:
        return None
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    overlapped = value.astimezone(UTC) - timedelta(seconds=overlap_seconds)
    return overlapped.isoformat()


def save_watermark(redis: CheckpointStore, source_key: str, watermark: str) -> None:
    value = datetime.fromisoformat(watermark.replace("Z", "+00:00")).astimezone(UTC)
    redis.set(f"{_PREFIX}:{source_key}", value.isoformat())


def load_source_ids(redis: CheckpointStore, source_key: str) -> set[int] | None:
    raw = redis.get(f"{_SOURCE_IDS_PREFIX}:{source_key}")
    if raw is None:
        return None
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    values = json.loads(text)
    if not isinstance(values, list) or any(type(value) is not int for value in values):
        raise ValueError(f"Invalid Fundbox source ID checkpoint for {source_key!r}")
    return set(values)


def save_reconciliation_state(
    redis: CheckpointStore,
    source_key: str,
    source_ids: set[int],
    watermark: str | None,
) -> None:
    pipeline = redis.pipeline(transaction=True)
    pipeline.set(
        f"{_SOURCE_IDS_PREFIX}:{source_key}",
        json.dumps(sorted(source_ids), separators=(",", ":")),
    )
    if watermark is not None:
        value = datetime.fromisoformat(watermark.replace("Z", "+00:00")).astimezone(UTC)
        pipeline.set(f"{_PREFIX}:{source_key}", value.isoformat())
    pipeline.execute()
