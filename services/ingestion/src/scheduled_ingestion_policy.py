"""Pure, host-timezone-independent policy for weekly scheduler occurrences."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

SCHEDULE_TIMEZONE = "Asia/Singapore"


@dataclass(frozen=True)
class OccurrenceContext:
    """Persisted occurrence deadline, independent of task delivery time."""

    occurrence_id: str
    starts_at: datetime
    drain_starts_at: datetime
    cutoff_at: datetime
    next_eligible_at: datetime
    timezone: str = SCHEDULE_TIMEZONE
    scheduled: bool = True

    def __post_init__(self) -> None:
        values = (
            self.starts_at,
            self.drain_starts_at,
            self.cutoff_at,
            self.next_eligible_at,
        )
        if not self.occurrence_id.strip():
            raise ValueError("occurrence ID must be non-empty")
        if any(
            value.tzinfo is None or value.utcoffset() is None
            for value in values
        ):
            raise ValueError(
                "occurrence timestamps must be timezone-aware"
            )


WeekdayName = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
OccurrenceEligibility = Literal["before_open", "open", "draining", "closed"]

_WEEKDAY_NUMBERS: dict[WeekdayName, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
}
_SCHEDULE_ZONE = ZoneInfo(SCHEDULE_TIMEZONE)
_OPENING = time(hour=9)
_CUTOFF = time(hour=23)


@dataclass(frozen=True)
class ScheduledOccurrence:
    """A named occurrence and its current admission state."""

    context: OccurrenceContext
    eligibility: OccurrenceEligibility


def weekly_occurrence(
    *,
    group_key: str,
    weekday: WeekdayName,
    now: datetime,
    drain_reserve_seconds: float,
) -> ScheduledOccurrence:
    """Return the most recent configured weekly occurrence for ``group_key``.

    A delivery received on a later weekday is deliberately associated with the
    prior occurrence and is therefore closed. It cannot manufacture a catch-up
    window on an intervening day.
    """
    _require_aware(now)
    if not group_key.strip():
        raise ValueError("scheduled group key must be non-empty")
    reserve = timedelta(seconds=drain_reserve_seconds)
    if reserve <= timedelta() or reserve >= timedelta(hours=14):
        raise ValueError("scheduled drain reserve must be between zero and fourteen hours")

    local_now = now.astimezone(_SCHEDULE_ZONE)
    days_since = (local_now.weekday() - _WEEKDAY_NUMBERS[weekday]) % 7
    opening_date = local_now.date() - timedelta(days=days_since)
    starts_local = datetime.combine(opening_date, _OPENING, tzinfo=_SCHEDULE_ZONE)
    cutoff_local = datetime.combine(opening_date, _CUTOFF, tzinfo=_SCHEDULE_ZONE)
    drain_local = cutoff_local - reserve
    next_local = starts_local + timedelta(days=7)
    context = OccurrenceContext(
        occurrence_id=f"scheduled:{group_key}:{opening_date.isoformat()}",
        starts_at=starts_local.astimezone(UTC),
        drain_starts_at=drain_local.astimezone(UTC),
        cutoff_at=cutoff_local.astimezone(UTC),
        next_eligible_at=next_local.astimezone(UTC),
    )
    return ScheduledOccurrence(context=context, eligibility=occurrence_eligibility(context, now))


def occurrence_eligibility(
    occurrence: OccurrenceContext,
    now: datetime,
) -> OccurrenceEligibility:
    """Classify admission without consulting host-local time or Celery state."""
    _require_aware(now)
    instant = now.astimezone(UTC)
    if instant < occurrence.starts_at:
        return "before_open"
    if instant < occurrence.drain_starts_at:
        return "open"
    if instant < occurrence.cutoff_at:
        return "draining"
    return "closed"


def scheduled_group_for_weekday(now: datetime) -> WeekdayName | None:
    """Return today's source-group weekday, leaving Sunday maintenance closed."""
    _require_aware(now)
    local_weekday = now.astimezone(_SCHEDULE_ZONE).weekday()
    for weekday, number in _WEEKDAY_NUMBERS.items():
        if number == local_weekday:
            return weekday
    return None


def occurrence_hour_bucket(now: datetime) -> str:
    """Return the absolute hour bucket that coalesces duplicate maintenance ticks.

    Singapore has no daylight saving, so a UTC hour maps one-to-one onto the
    schedule's local hour. The bucket is derived from the absolute clock only,
    never from delivery time measured against a stored deadline.
    """
    _require_aware(now)
    return now.astimezone(UTC).strftime("%Y%m%dT%H")


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduled policy requires timezone-aware timestamps")
