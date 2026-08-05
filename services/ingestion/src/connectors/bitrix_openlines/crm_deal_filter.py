"""Typed source-filter helpers for Bitrix CRM deal list requests."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from src.connectors.bitrix_openlines.models import CrmDeal
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
