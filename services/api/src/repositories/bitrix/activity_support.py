"""Typed state and aggregation helpers for bounded Bitrix activity reads."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from src.display_format import format_display_date
from src.types_crm import (
    BitrixDealScope,
    CrmActivityFailureReason,
    CrmActivityKindCount,
    CrmCallClassificationCount,
)

CacheDisposition = Literal["miss", "hit", "coalesced", "disabled"]


@dataclass(frozen=True)
class Activity:
    activity_id: str
    kind: str
    call_classification: str | None
    event_at: datetime


class ReadError(Exception):
    def __init__(self, reason: CrmActivityFailureReason) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Budget:
    max_requests: int
    max_pages: int
    max_rows: int
    requests: int = 0
    pages: int = 0
    rows: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def reserve_request(self) -> None:
        async with self.lock:
            if self.requests >= self.max_requests:
                raise ReadError("request_limit")
            self.requests += 1

    async def reserve_page(self) -> None:
        async with self.lock:
            if self.pages >= self.max_pages:
                raise ReadError("page_limit")
            self.pages += 1

    async def reserve_row(self) -> None:
        async with self.lock:
            if self.rows >= self.max_rows:
                raise ReadError("row_limit")
            self.rows += 1


@dataclass
class ReadState:
    activities: dict[str, Activity] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def add(self, activity: Activity) -> None:
        async with self.lock:
            existing = self.activities.get(activity.activity_id)
            if existing is None or activity.event_at > existing.event_at:
                self.activities[activity.activity_id] = activity

    async def snapshot(self) -> list[Activity]:
        async with self.lock:
            return list(self.activities.values())


def _daily_series(items: list[Activity], fetched: datetime) -> list[int]:
    return [
        sum(1 for item in items if (fetched.date() - item.event_at.date()).days == 29 - index)
        for index in range(30)
    ]


def _change(current: int, old: int) -> int | None:
    return None if old == 0 else round((current - old) * 100 / old)


def aggregate_activity_metrics(
    scope: BitrixDealScope,
    fetched: datetime,
    budget: Budget,
    activities: list[Activity],
    cache: CacheDisposition,
    source_instance: str,
) -> dict[str, object]:
    deduped = list({item.activity_id: item for item in activities}.values())
    dates = [item.event_at for item in deduped]
    cutoff = fetched - timedelta(days=30)
    prior = fetched - timedelta(days=60)
    recent = [item for item in deduped if cutoff <= item.event_at <= fetched]
    prior_rows = [item for item in deduped if prior <= item.event_at < cutoff]
    kinds = Counter(item.kind for item in deduped)
    calls = [item for item in deduped if item.kind == "call"]
    classifications = Counter(item.call_classification or "unknown" for item in calls)
    kind_rows = [
        CrmActivityKindCount(
            history_kind=key,
            count=value,
            last_event_at=max(item.event_at for item in deduped if item.kind == key).isoformat(),
            last_event_at_display=format_display_date(
                max(item.event_at for item in deduped if item.kind == key).isoformat()
            ),
        )
        for key, value in sorted(kinds.items())
    ]
    return {
        "source_instance": source_instance,
        "fetched_at": fetched.isoformat(),
        "fetched_at_display": format_display_date(fetched.isoformat()),
        "cache_disposition": cache,
        "queried_deal_count": len(scope.deal_ids),
        "resolved_deal_count": scope.resolved_deal_count,
        "request_count": budget.requests,
        "page_count": budget.pages,
        "row_count": budget.rows,
        "activity_count": len(deduped),
        "call_count": len(calls),
        "activity_kind_breakdown": kind_rows,
        "call_classification_breakdown": [
            CrmCallClassificationCount(classification=key, count=value)
            for key, value in sorted(classifications.items())
        ],
        "first_activity_at": min(dates).isoformat() if dates else None,
        "first_activity_at_display": format_display_date(min(dates).isoformat()) if dates else None,
        "last_activity_at": max(dates).isoformat() if dates else None,
        "last_activity_at_display": format_display_date(max(dates).isoformat()) if dates else None,
        "recent_30d_activity_count": len(recent),
        "recent_30d_call_count": sum(item.kind == "call" for item in recent),
        "recent_30d_daily_activity_counts": _daily_series(deduped, fetched),
        "recent_30d_daily_call_counts": _daily_series(calls, fetched),
        "recent_30d_activity_change_pct": _change(len(recent), len(prior_rows)),
        "recent_30d_call_change_pct": _change(
            sum(item.kind == "call" for item in recent),
            sum(item.kind == "call" for item in prior_rows),
        ),
    }
