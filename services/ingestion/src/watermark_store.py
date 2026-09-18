"""Redis-backed high-water-mark store for incremental ingestion."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

import redis

logger = logging.getLogger(__name__)

_KEY_PREFIX = "profile_unifier:watermark"


@dataclass(frozen=True)
class IngestionWatermark:
    """High-water-mark for one source (or source+entity) ingestion stream."""

    updated_at: datetime | None
    source_key: str
    entity_key: str | None


def _redis_key(source_key: str, entity_key: str | None) -> str:
    if entity_key is not None:
        return f"{_KEY_PREFIX}:{source_key}:{entity_key}"
    return f"{_KEY_PREFIX}:{source_key}"


def load_watermark(
    client: redis.Redis[bytes],
    source_key: str,
    entity_key: str | None = None,
) -> IngestionWatermark:
    """Load the stored watermark, returning ``updated_at=None`` on first run."""
    key = _redis_key(source_key, entity_key)
    raw = client.get(key)
    if raw is None:
        return IngestionWatermark(updated_at=None, source_key=source_key, entity_key=entity_key)
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    data: dict[str, str] = json.loads(text)
    updated_at = datetime.fromisoformat(data["updated_at"])
    return IngestionWatermark(updated_at=updated_at, source_key=source_key, entity_key=entity_key)


def save_watermark(client: redis.Redis[bytes], watermark: IngestionWatermark) -> None:
    """Persist a watermark after a successful full drain."""
    if watermark.updated_at is None:
        raise ValueError("Cannot save a watermark with updated_at=None")
    key = _redis_key(watermark.source_key, watermark.entity_key)
    client.set(key, json.dumps({"updated_at": watermark.updated_at.isoformat()}))


def reset_watermark(
    client: redis.Redis[bytes],
    source_key: str,
    entity_key: str | None = None,
) -> None:
    """Delete the stored watermark, forcing next run to bootstrap."""
    client.delete(_redis_key(source_key, entity_key))
