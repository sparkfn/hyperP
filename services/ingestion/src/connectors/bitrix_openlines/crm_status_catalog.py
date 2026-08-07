"""Strict read-only parsing for Bitrix deal-stage status catalog pages."""

from __future__ import annotations

import math

from src.connectors.bitrix_openlines.models import (
    CrmDealStageCatalogItem,
    CrmDealStageCatalogPage,
)
from src.models import JsonValue


def deal_stage_status_entity_id(category_id: int) -> str:
    """Return the Bitrix status-directory identity for one deal category."""
    _validate_category_id(category_id)
    return "DEAL_STAGE" if category_id == 0 else f"DEAL_STAGE_{category_id}"


def parse_crm_deal_stage_catalog_page(
    payload: dict[str, JsonValue],
    *,
    category_id: int,
    current_start: int,
) -> CrmDealStageCatalogPage:
    """Parse one ``crm.status.list`` page for exactly one deal category."""
    _validate_category_id(category_id)
    _validate_start(current_start)
    result = payload.get("result")
    if not isinstance(result, list):
        raise RuntimeError("Bitrix CRM stage catalog returned an invalid result")
    expected_entity_id = deal_stage_status_entity_id(category_id)
    items = tuple(
        _parse_stage_catalog_item(
            raw, category_id=category_id, expected_entity_id=expected_entity_id
        )
        for raw in result
    )
    timing_value = payload.get("time")
    if "time" in payload and timing_value is not None and not isinstance(timing_value, dict):
        raise RuntimeError("Bitrix CRM stage catalog returned an invalid time")
    timing = timing_value if isinstance(timing_value, dict) else {}
    next_start = _optional_non_negative_int(payload, "next")
    if next_start is not None and next_start <= current_start:
        raise RuntimeError("Bitrix CRM stage catalog pagination did not advance")
    return CrmDealStageCatalogPage(
        items=items,
        next_start=next_start,
        total=_optional_non_negative_int(payload, "total"),
        operating=_optional_finite_number(timing, "operating"),
        operating_reset_at=_optional_finite_number(timing, "operating_reset_at"),
    )


def _parse_stage_catalog_item(
    raw: JsonValue,
    *,
    category_id: int,
    expected_entity_id: str,
) -> CrmDealStageCatalogItem:
    if not isinstance(raw, dict):
        raise RuntimeError("Bitrix CRM stage catalog contained an invalid item")
    entity_id = _required_text(raw, "ENTITY_ID")
    if entity_id != expected_entity_id:
        raise RuntimeError("Bitrix CRM stage catalog returned an unexpected ENTITY_ID")
    source_category_id = _optional_numeric_category_id(raw, "CATEGORY_ID")
    if source_category_id is not None and source_category_id != category_id:
        raise RuntimeError("Bitrix CRM stage catalog returned an unexpected CATEGORY_ID")
    top_level_semantic = _optional_text(raw, "SEMANTICS")
    extra_value = raw.get("EXTRA")
    if extra_value is not None and not isinstance(extra_value, dict):
        raise RuntimeError("Bitrix CRM stage catalog contained an invalid EXTRA")
    extra_semantic = _optional_text(extra_value, "SEMANTICS") if extra_value is not None else None
    if (
        top_level_semantic is not None
        and extra_semantic is not None
        and top_level_semantic != extra_semantic
    ):
        raise RuntimeError("Bitrix CRM stage catalog contained conflicting semantics")
    return CrmDealStageCatalogItem(
        category_id=str(category_id),
        stage_id=_required_text(raw, "STATUS_ID"),
        semantic_id=extra_semantic if extra_semantic is not None else top_level_semantic,
    )


def _validate_category_id(category_id: int) -> None:
    if isinstance(category_id, bool) or not isinstance(category_id, int) or category_id < 0:
        raise ValueError("Bitrix CRM stage catalog category_id must be non-negative")


def _validate_start(start: int) -> None:
    if isinstance(start, bool) or not isinstance(start, int) or start < 0:
        raise ValueError("Bitrix CRM stage catalog start must be non-negative")


def _required_text(payload: dict[str, JsonValue], field_name: str) -> str:
    value = _optional_text(payload, field_name)
    if value is None:
        raise RuntimeError(f"Bitrix CRM stage catalog omitted {field_name}")
    return value


def _optional_text(payload: dict[str, JsonValue] | None, field_name: str) -> str | None:
    if payload is None or field_name not in payload or payload[field_name] is None:
        return None
    value = payload[field_name]
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise RuntimeError(f"Bitrix CRM stage catalog contained an invalid {field_name}")
    return value


def _optional_numeric_category_id(payload: dict[str, JsonValue], field_name: str) -> int | None:
    if field_name not in payload or payload[field_name] is None:
        return None
    value = payload[field_name]
    if isinstance(value, bool):
        raise RuntimeError(f"Bitrix CRM stage catalog contained an invalid {field_name}")
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise RuntimeError(f"Bitrix CRM stage catalog contained an invalid {field_name}")


def _optional_non_negative_int(payload: dict[str, JsonValue], field_name: str) -> int | None:
    if field_name not in payload or payload[field_name] is None:
        return None
    value = payload[field_name]
    if isinstance(value, bool):
        raise RuntimeError(f"Bitrix CRM stage catalog returned an invalid {field_name}")
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise RuntimeError(f"Bitrix CRM stage catalog returned an invalid {field_name}")


def _optional_finite_number(payload: dict[str, JsonValue], field_name: str) -> float | None:
    if field_name not in payload or payload[field_name] is None:
        return None
    value = payload[field_name]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RuntimeError(f"Bitrix CRM stage catalog returned an invalid time.{field_name}")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise RuntimeError(f"Bitrix CRM stage catalog returned an invalid time.{field_name}")
    return parsed
