"""Primitive converters for the read projection (#491).

The projection must return the API documents verbatim, so these helpers
reproduce the API's ``src/graph/converters.py`` and display-formatting
semantics. The ingestion service is a separate wheel from the API service, so
the small converter set is repeated here rather than imported.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from typing import Protocol, runtime_checkable
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
_MAX_PLAUSIBLE_DOB_AGE_YEARS = 130


@runtime_checkable
class _NativeTemporal(Protocol):
    """A graph temporal value that can be converted to a native datetime."""

    def to_native(self) -> datetime: ...


def to_str(value: object, default: str = "") -> str:
    """Convert a graph value to ``str`` with a safe default."""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def to_optional_str(value: object) -> str | None:
    """Convert a graph value to ``str`` or ``None`` when it is null."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def to_int(value: object, default: int = 0) -> int:
    """Convert a graph value to ``int`` with a safe default."""
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value)
    return default


def to_float(value: object, default: float = 0.0) -> float:
    """Convert a graph value to ``float`` with a safe default."""
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        return float(value)
    return default


def to_optional_float(value: object) -> float | None:
    """Convert a graph value to ``float`` or ``None`` when it is null."""
    if value is None:
        return None
    return to_float(value)


def to_bool(value: object) -> bool:
    """Convert a graph value to ``bool`` (null is false)."""
    return bool(value)


def to_iso_or_none(value: object) -> str | None:
    """Convert a graph temporal value to an ISO 8601 string."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    native = _to_native_datetime(value)
    if native is not None:
        return native.isoformat()
    return str(value)


def to_iso_or_empty(value: object) -> str:
    """Convert a graph temporal value to ISO 8601, defaulting to an empty string."""
    return to_iso_or_none(value) or ""


def to_utc_datetime(value: object) -> datetime | None:
    """Convert a graph temporal value to an aware UTC datetime."""
    native = _to_native_datetime(value)
    if native is None:
        return None
    if native.tzinfo is None:
        return native.replace(tzinfo=UTC)
    return native.astimezone(UTC)


def to_iso_utc_z(value: object) -> str:
    """Convert a graph temporal value to the UTC ISO 8601 form pydantic emits.

    The identity-link revision contract carries ``effective_at`` as a string, so
    the value is normalised to UTC with a trailing ``Z``.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    native = _to_native_datetime(value)
    if native is None:
        return str(value)
    if native.tzinfo is None:
        native = native.replace(tzinfo=UTC)
    return native.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _to_native_datetime(value: object) -> datetime | None:
    """Return the native datetime of a graph temporal value, if it has one."""
    if isinstance(value, _NativeTemporal):
        native = value.to_native()
        if isinstance(native, datetime):
            return native
    if isinstance(value, datetime):
        return value
    return None


def display_timezone() -> ZoneInfo:
    """Return the timezone used for DOB display, sourced from ``TZ``."""
    name = os.environ.get("TZ", "").strip()
    if not name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def format_display_dob(value: str | None) -> tuple[str, bool]:
    """Format a DOB for display and flag implausible values.

    Mirrors the API's ``format_display_dob`` so the projected list payload
    renders identically: empty values render as an em dash, unparseable or
    implausible values are returned verbatim and flagged, and valid dates
    render as ``DD Mon YYYY``.
    """
    if not value:
        return ("—", False)
    today = datetime.now(display_timezone()).date()
    parsed_date = _parse_date(value)
    if parsed_date is not None:
        if parsed_date > today or parsed_date.year < today.year - _MAX_PLAUSIBLE_DOB_AGE_YEARS:
            return (value, True)
        return (_format_date_part(parsed_date), False)
    parsed_datetime = _parse_datetime(value)
    if parsed_datetime is None:
        return (value, True)
    local = parsed_datetime.astimezone(display_timezone())
    if local.date() > today or local.year < today.year - _MAX_PLAUSIBLE_DOB_AGE_YEARS:
        return (value, True)
    return (_format_date_part(local.date()), False)


def _parse_date(value: str) -> date | None:
    """Parse a date-only ISO value, or return ``None``."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_datetime(value: str) -> datetime | None:
    """Parse an ISO datetime value, treating a missing offset as UTC."""
    text = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _format_date_part(value: date) -> str:
    """Return ``DD Mon YYYY`` for a date."""
    return f"{value.day:02d} {_MONTHS[value.month - 1]} {value.year}"
