"""Private deal-field classification for CRM-WON discovery."""

from __future__ import annotations

import math
from datetime import datetime

from src.sales_prediction_discovery_payload import (
    first_value,
    nested_object,
    parse_payload,
    payload_status,
)
from src.sales_prediction_discovery_taxonomy import (
    category_status,
    currency_status,
    currency_taxonomy,
    scoped_entity,
    source_taxonomy,
    stage_taxonomy_for_entity,
)
from src.sales_prediction_discovery_temporal import availability_status

type DiscoveryScalar = str | int | float | bool | None
type DiscoveryRow = dict[str, DiscoveryScalar]


def deal_aggregate_key(
    row: DiscoveryRow,
    cutoff: datetime,
    report_cutoff: datetime,
    entity_keys: tuple[str, ...],
    stage_catalog: dict[str, frozenset[str]],
) -> tuple[DiscoveryScalar, ...]:
    """Build one privacy-safe aggregate key from a private CRM deal row."""
    payload = parse_payload(row)
    nested = nested_object(payload, "deal")
    entity = scoped_entity(row.get("entity_key"), entity_keys)
    raw_stage = first_value(payload, nested, "stage_id", "STAGE_ID")
    raw_currency = first_value(payload, nested, "currency", "currency_id", "CURRENCY_ID")
    contact_count = _integer(first_value(payload, nested, "contact_count"))
    ambiguous = _boolean(first_value(payload, nested, "crm_contact_resolution_required"))
    return (
        entity,
        source_taxonomy(row.get("source_key")),
        category_status(first_value(payload, nested, "category_id", "CATEGORY_ID")),
        stage_taxonomy_for_entity(raw_stage, entity, stage_catalog),
        availability_status(row, cutoff, report_cutoff),
        _number_state(first_value(payload, nested, "amount", "opportunity", "OPPORTUNITY")),
        currency_status(raw_currency),
        currency_taxonomy(raw_currency),
        _probability_state(first_value(payload, nested, "probability", "PROBABILITY")),
        _assignment_state(first_value(payload, nested, "assigned_by_id", "ASSIGNED_BY_ID")),
        _count_band(_integer(row.get("linked_person_count"))),
        "current_graph_projection",
        _count_band(contact_count),
        ambiguous if ambiguous is not None else "unknown",
        _revision_band(row),
        _integer(row.get("crm_history_child_count")) or 0,
        payload_status(row),
    )


def _number_state(value: object) -> str:
    number = _number(value)
    if value is None or value == "":
        return "missing"
    if number is None or not math.isfinite(number):
        return "invalid"
    if number < 0:
        return "negative"
    return "zero" if number == 0 else "non_zero"


def _probability_state(value: object) -> str:
    number = _number(value)
    if value is None or value == "":
        return "missing"
    if number is None or not math.isfinite(number):
        return "invalid"
    return "present" if 0 <= number <= 100 else "out_of_range"


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _boolean(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _assignment_state(value: object) -> str:
    if value is None or value == "" or value == 0 or value == "0":
        return "unassigned"
    return "assigned" if isinstance(value, int | str) and not isinstance(value, bool) else "invalid"


def _count_band(value: int | None) -> str:
    if value is None or value < 0:
        return "unknown_or_invalid"
    return "0" if value == 0 else "1" if value == 1 else "2_plus"


def _revision_band(row: DiscoveryRow) -> str:
    version = _integer(row.get("source_record_version")) or 0
    return "multiple_snapshots_possible" if version > 1 else "single_snapshot"
