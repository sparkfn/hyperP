"""Typed source-filter helpers for Bitrix CRM deal list requests."""

from __future__ import annotations

import math
from collections.abc import Collection
from dataclasses import dataclass

from src.connectors.bitrix_openlines.models import (
    CrmDeal,
    CrmDealCapabilityItem,
    CrmDealCapabilityPage,
)
from src.models import JsonValue


@dataclass(frozen=True)
class CrmDealPage:
    """One filtered Bitrix CRM deal-list page after duplicate suppression."""

    deals: tuple[CrmDeal, ...]
    returned_count: int


def normalize_crm_category_ids(category_ids: Collection[str]) -> tuple[str, ...]:
    """Return a stable, validated CRM category allowlist without duplicates."""
    normalized: list[str] = []
    seen: set[str] = set()
    for category_id in category_ids:
        if not isinstance(category_id, str):
            raise ValueError("Bitrix CRM category IDs must be non-empty numeric strings")
        value = category_id.strip()
        if not value.isdigit():
            raise ValueError("Bitrix CRM category IDs must be non-empty numeric strings")
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    return tuple(normalized)


def crm_deal_category_filter(category_ids: Collection[str]) -> dict[str, JsonValue]:
    """Build the documented multi-category Bitrix deal-list filter."""
    normalized = normalize_crm_category_ids(category_ids)
    if not normalized:
        raise ValueError("Bitrix CRM deal source filter requires at least one category")
    return {"@CATEGORY_ID": list(normalized)}


def crm_deal_capability_filter(
    category_ids: Collection[str],
    *,
    greater_than_id: int | None,
    less_than_or_equal_to_id: int | None,
) -> dict[str, JsonValue]:
    """Build a bounded keyset filter for a read-only deal capability page."""
    filters = crm_deal_category_filter(category_ids)
    lower_bound = _optional_positive_id(greater_than_id, "greater_than_id")
    upper_bound = _optional_positive_id(less_than_or_equal_to_id, "less_than_or_equal_to_id")
    if lower_bound is not None:
        filters[">ID"] = lower_bound
    if upper_bound is not None:
        filters["<=ID"] = upper_bound
    if lower_bound is not None and upper_bound is not None and lower_bound >= upper_bound:
        raise ValueError("Bitrix CRM deal capability bounds must be strictly increasing")
    return filters


def parse_crm_deal_capability_page(payload: dict[str, JsonValue]) -> CrmDealCapabilityPage:
    """Parse one minimal ``crm.deal.list`` response without enrichment data."""
    raw_items = payload.get("result")
    if not isinstance(raw_items, list):
        raise RuntimeError("Bitrix CRM deal capability returned an invalid result")
    items = tuple(_parse_capability_item(item) for item in raw_items)
    timing_value = payload.get("time")
    if "time" in payload and timing_value is not None and not isinstance(timing_value, dict):
        raise RuntimeError("Bitrix CRM deal capability returned an invalid time")
    timing = timing_value if isinstance(timing_value, dict) else {}
    return CrmDealCapabilityPage(
        items=items,
        next_start=_optional_non_negative_int(payload, "next"),
        total=_optional_non_negative_int(payload, "total"),
        operating=_optional_finite_number(timing, "operating"),
        operating_reset_at=_optional_finite_number(timing, "operating_reset_at"),
    )


def _parse_capability_item(raw: JsonValue) -> CrmDealCapabilityItem:
    if not isinstance(raw, dict):
        raise RuntimeError("Bitrix CRM deal capability contained an invalid item")
    return CrmDealCapabilityItem(
        deal_id=_required_numeric_source_id(raw, "ID", positive=True),
        category_id=_required_numeric_source_id(raw, "CATEGORY_ID", positive=False),
        stage_id=_optional_source_text(raw, "STAGE_ID"),
    )


def _required_numeric_source_id(
    payload: dict[str, JsonValue],
    field_name: str,
    *,
    positive: bool,
) -> str:
    if field_name not in payload:
        raise RuntimeError(f"Bitrix CRM deal capability omitted {field_name}")
    value = payload[field_name]
    if isinstance(value, bool):
        raise RuntimeError(f"Bitrix CRM deal capability contained an invalid {field_name}")
    if isinstance(value, int):
        parsed = str(value)
    elif isinstance(value, str) and value.isdigit():
        parsed = value
    else:
        raise RuntimeError(f"Bitrix CRM deal capability contained an invalid {field_name}")
    numeric_value = int(parsed)
    if numeric_value < 0 or (positive and numeric_value == 0):
        raise RuntimeError(f"Bitrix CRM deal capability contained an invalid {field_name}")
    return parsed


def _optional_source_text(payload: dict[str, JsonValue], field_name: str) -> str | None:
    if field_name not in payload or payload[field_name] is None:
        return None
    value = payload[field_name]
    if isinstance(value, bool):
        raise RuntimeError(f"Bitrix CRM deal capability contained an invalid {field_name}")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value
    raise RuntimeError(f"Bitrix CRM deal capability contained an invalid {field_name}")


def _optional_non_negative_int(payload: dict[str, JsonValue], field_name: str) -> int | None:
    if field_name not in payload or payload[field_name] is None:
        return None
    value = payload[field_name]
    if isinstance(value, bool):
        raise RuntimeError(f"Bitrix CRM deal capability returned an invalid {field_name}")
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise RuntimeError(f"Bitrix CRM deal capability returned an invalid {field_name}")


def _optional_finite_number(payload: dict[str, JsonValue], field_name: str) -> float | None:
    if field_name not in payload or payload[field_name] is None:
        return None
    value = payload[field_name]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RuntimeError(f"Bitrix CRM deal capability returned an invalid time.{field_name}")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise RuntimeError(f"Bitrix CRM deal capability returned an invalid time.{field_name}")
    return parsed


def _optional_positive_id(value: int | None, argument_name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"Bitrix CRM deal capability {argument_name} must be positive")
    return value
