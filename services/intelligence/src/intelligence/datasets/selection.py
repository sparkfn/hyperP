"""Strict point-in-time source-version selection for the fixed dataset definition."""

from __future__ import annotations

from dataclasses import dataclass

from intelligence.crm_deal_refs.models import DealReference
from intelligence.datasets.models import parse_utc


@dataclass(frozen=True)
class Selection:
    """One unambiguous latest eligible source version or its censor reason."""

    record: DealReference | None
    reason: str | None


def select_version(versions: tuple[DealReference, ...], cutoff: str) -> Selection:
    """Select latest valid observed source state without PK tie-breaking or fallback."""
    candidates = tuple(item for item in versions if _eligible(item, cutoff))
    if not candidates:
        return Selection(None, "no_eligible_version")
    order = max(_order(item) for item in candidates)
    leading = tuple(item for item in candidates if _order(item) == order)
    if len(leading) != 1:
        return Selection(None, "ambiguous_authoritative_version")
    return Selection(leading[0], None)


def _eligible(record: DealReference, cutoff: str) -> bool:
    required = (
        record.first_known_at,
        record.available_at,
        record.observed_at,
        record.source_event_at,
        record.source_effective_at,
    )
    if any(value is None for value in required):
        return False
    values = tuple(value for value in required if value is not None)
    if record.source_close_date is not None:
        values += (record.source_close_date,)
    try:
        cutoff_value = parse_utc(cutoff, "cutoff")
        return all(parse_utc(value, "deal temporal evidence") <= cutoff_value for value in values)
    except ValueError:
        return False


def _order(record: DealReference) -> tuple[object, ...]:
    effective = record.source_effective_at
    observed = record.observed_at
    available = record.available_at
    if effective is None or observed is None or available is None:
        raise ValueError("ineligible deal version cannot be ordered")
    return (
        parse_utc(effective, "source effective"),
        parse_utc(observed, "observed"),
        parse_utc(available, "available"),
        record.key.source_record_version,
    )
