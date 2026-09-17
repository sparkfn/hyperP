"""Host-timezone-independent scheduled-occurrence helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from src.bounded_ingestion_models import SCHEDULE_TIMEZONE, OccurrenceContext

_SINGAPORE = ZoneInfo(SCHEDULE_TIMEZONE)
_REQUIRED_KEYS = frozenset(
    {
        "occurrence_id",
        "starts_at",
        "drain_starts_at",
        "cutoff_at",
        "next_eligible_at",
        "timezone",
    }
)
_OPTIONAL_KEYS = frozenset({"scheduled"})


def next_weekly_opening(starts_at: datetime) -> datetime:
    local = starts_at.astimezone(_SINGAPORE)
    next_local = local + timedelta(days=7)
    return next_local.astimezone(UTC)


def occurrence_from_payload(value: dict[str, str]) -> OccurrenceContext:
    keys = set(value)
    if not _REQUIRED_KEYS <= keys or keys - _REQUIRED_KEYS - _OPTIONAL_KEYS:
        raise ValueError("bounded occurrence payload has an invalid shape")
    scheduled_text = value.get("scheduled", "true").lower()
    if scheduled_text not in {"true", "false"}:
        raise ValueError("bounded occurrence scheduled flag is invalid")
    occurrence = OccurrenceContext(
        occurrence_id=value["occurrence_id"],
        starts_at=_parse(value["starts_at"]),
        drain_starts_at=_parse(value["drain_starts_at"]),
        cutoff_at=_parse(value["cutoff_at"]),
        next_eligible_at=_parse(value["next_eligible_at"]),
        timezone=value["timezone"],
        scheduled=scheduled_text == "true",
    )
    if occurrence.scheduled and occurrence.next_eligible_at != next_weekly_opening(
        occurrence.starts_at
    ):
        raise ValueError("scheduled continuation must use the next weekly opening")
    return occurrence


def occurrence_to_payload(occurrence: OccurrenceContext) -> dict[str, str]:
    return {
        "occurrence_id": occurrence.occurrence_id,
        "starts_at": occurrence.starts_at.astimezone(UTC).isoformat(),
        "drain_starts_at": occurrence.drain_starts_at.astimezone(UTC).isoformat(),
        "cutoff_at": occurrence.cutoff_at.astimezone(UTC).isoformat(),
        "next_eligible_at": occurrence.next_eligible_at.astimezone(UTC).isoformat(),
        "timezone": occurrence.timezone,
        "scheduled": "true" if occurrence.scheduled else "false",
    }


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("bounded occurrence timestamp must be timezone-aware")
    return parsed.astimezone(UTC)
