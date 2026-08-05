"""Bitemporal lifecycle classification for CRM-WON discovery."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime


def availability_status(
    row: Mapping[str, object], cutoff: datetime, report_cutoff: datetime
) -> str:
    """Classify whether one record was usable at the historical cutoff."""
    lifecycle_fields = ("rejected_at", "link_failed_at", "superseded_at")
    if any(_invalid_timestamp(row.get(field)) for field in lifecycle_fields):
        return "invalid_lifecycle_timestamp"
    observed = _timestamp(row.get("observed_at"))
    ingested = _timestamp(row.get("ingested_at"))
    activated = _timestamp(row.get("activated_at"))
    if observed is None or ingested is None or activated is None:
        return "historical_availability_unreconstructable"
    if observed > report_cutoff or ingested > report_cutoff or activated > report_cutoff:
        return "after_report_cutoff"
    rejected = _timestamp(row.get("rejected_at"))
    link_failed = _timestamp(row.get("link_failed_at"))
    superseded = _timestamp(row.get("superseded_at"))
    if rejected is not None and rejected <= cutoff:
        return "rejected_by_as_of"
    if link_failed is not None and link_failed <= cutoff:
        return "link_failed_by_as_of"
    if superseded is not None and superseded <= cutoff:
        return "superseded_by_as_of"
    if observed > cutoff:
        return "source_event_after_as_of"
    if ingested > cutoff or activated > cutoff:
        return "late_arriving_after_as_of"
    if (rejected is not None and rejected <= report_cutoff) or (
        link_failed is not None and link_failed <= report_cutoff
    ):
        return "available_as_of_later_invalidated"
    if superseded is not None and superseded <= report_cutoff:
        return "available_as_of_later_superseded"
    return "available_as_of"


def datetime_from_iso(value: str) -> datetime:
    """Parse one timezone-aware discovery cutoff."""
    parsed = _timestamp(value)
    if parsed is None:
        raise ValueError("discovery cutoff must be a timezone-aware ISO timestamp")
    return parsed


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _invalid_timestamp(value: object) -> bool:
    return value is not None and value != "" and _timestamp(value) is None
