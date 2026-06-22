"""Presentation helpers that format raw values into display strings.

Centralized so the API can hand the frontend ready-to-render text (dates,
percentages) and the frontend does no locale/number formatting. Output matches
the existing frontend2 en-SG style: "02 Apr 2026" and "02 Apr 2026, 03:14 AM".

Date/time display follows the ``TZ`` environment variable (fallback ``UTC``).
The variable is set in ``docker-compose.yml`` from ``.env`` — the frontend
mirrors the same value via ``NEXT_PUBLIC_TZ`` so backend and UI agree.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_MONTHS: tuple[str, ...] = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _display_tz() -> timezone:
    """Return the timezone used for display, sourced from the ``TZ`` env var.

    Falls back to UTC when ``TZ`` is unset, empty, or unknown so the API
    stays usable in environments that don't pin a timezone.
    """
    name = os.environ.get("TZ", "").strip()
    if not name:
        return UTC
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return UTC


def _parse_utc(value: str) -> datetime | None:
    """Parse an ISO-8601 string to a naive datetime in the display timezone, or None.

    The value is assumed to be UTC (the on-disk format from Neo4j / FastAPI).
    We strip the tzinfo after converting to the display zone so the existing
    ``_format_date_part`` / ``hour12`` logic keeps working unchanged.
    """
    if not value:
        return None
    text = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(_display_tz()).replace(tzinfo=None)


def _format_date_part(dt: datetime) -> str:
    """Render the shared 'DD Mon YYYY' portion used by every date formatter."""
    return f"{dt.day:02d} {_MONTHS[dt.month - 1]} {dt.year}"


def format_display_date(value: str) -> str:
    """Format an ISO string as 'DD Mon YYYY' in UTC; '' if unparseable/empty."""
    dt = _parse_utc(value)
    if dt is None:
        return ""
    return _format_date_part(dt)


def format_display_datetime(value: str) -> str:
    """Format an ISO string as 'DD Mon YYYY, hh:mm AM/PM' in UTC; '' if invalid."""
    dt = _parse_utc(value)
    if dt is None:
        return ""
    hour12 = dt.hour % 12 or 12
    meridiem = "AM" if dt.hour < 12 else "PM"
    return f"{_format_date_part(dt)}, {hour12:02d}:{dt.minute:02d} {meridiem}"


_MAX_PLAUSIBLE_DOB_AGE_YEARS = 130


def format_display_dob(value: str | None) -> tuple[str, bool]:
    """Format a date-of-birth for display and flag implausible values.

    Returns ``(display, is_invalid)`` so the frontend renders verbatim:
      - empty / ``None`` -> ``("—", False)`` — no DOB on file, not an error.
      - unparseable, in the future, or older than ~130 years ->
        ``(raw_value, True)`` — show the offending value so reviewers can spot it.
      - otherwise -> ``("DD Mon YYYY", False)`` (in the display timezone,
        matching ``format_display_date``).

    Unlike a year-only guard, this validates the full date, so malformed values
    such as ``"1990-13-45"`` are flagged rather than rendered as "Invalid Date".
    """
    if not value:
        return ("—", False)
    dt = _parse_utc(value)
    if dt is None:
        return (value, True)
    today = datetime.now(_display_tz())
    if dt.date() > today.date() or dt.year < today.year - _MAX_PLAUSIBLE_DOB_AGE_YEARS:
        return (value, True)
    return (_format_date_part(dt), False)


def format_confidence_pct(value: float | None) -> str | None:
    """Format a 0..1 confidence as an integer percent string, or None."""
    if value is None:
        return None
    return f"{round(value * 100)}%"
