"""Simplified group dispatch for watermark-based incremental ingestion."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal, TypedDict
from zoneinfo import ZoneInfo

import redis

from src.ingestion_config import get_ingestion_config
from src.scheduled_ingestion_groups import ScheduledIngestionGroup, Weekday

logger = logging.getLogger(__name__)

_SGT = ZoneInfo("Asia/Singapore")
_SOURCE_LOCK_PREFIX = "profile_unifier:ingestion:source"

DispatchStatus = Literal["disabled", "window_closed", "published"]

_DAY_INDEX: dict[Weekday, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
}


class ScheduledGroupDispatchSummary(TypedDict):
    status: DispatchStatus
    dispatched: int
    skipped: int


def _source_is_locked(
    redis_client: redis.Redis[bytes],
    source_key: str,
    entity_key: str | None,
) -> bool:
    """Check if a source lock key exists (best-effort, not atomic)."""
    lock_key = f"{_SOURCE_LOCK_PREFIX}:{source_key}"
    return redis_client.get(lock_key) is not None


def dispatch_incremental_group(
    group: ScheduledIngestionGroup,
    now: datetime,
    redis_client: redis.Redis[bytes],
    dispatch_fn: object | None = None,
) -> ScheduledGroupDispatchSummary:
    """Dispatch incremental tasks for a scheduled group.

    The ``dispatch_fn`` parameter is unused in this foundation PR; actual
    Celery dispatch is wired in the task integration step.
    """
    config = get_ingestion_config()
    if not config.scheduled_ingestion.enabled:
        return ScheduledGroupDispatchSummary(
            status="disabled",
            dispatched=0,
            skipped=0,
        )

    sgt_now = now.astimezone(_SGT)
    weekday_name: Weekday | None = None
    for name, idx in _DAY_INDEX.items():
        if sgt_now.weekday() == idx:
            weekday_name = name
            break

    if weekday_name != group.weekday or not (9 <= sgt_now.hour < 23):
        return ScheduledGroupDispatchSummary(
            status="window_closed",
            dispatched=0,
            skipped=0,
        )

    dispatched = 0
    skipped = 0
    for spec in group.tasks:
        if _source_is_locked(redis_client, spec.source_key, spec.entity_key):
            logger.info(
                "Skipping %s (already locked)",
                spec.source_key,
            )
            skipped += 1
            continue

        logger.info("Would dispatch %s (entity=%s)", spec.source_key, spec.entity_key)
        dispatched += 1

    return ScheduledGroupDispatchSummary(
        status="published",
        dispatched=dispatched,
        skipped=skipped,
    )
