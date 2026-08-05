from __future__ import annotations

import pytest
from src.connectors.bitrix_openlines.crm_deal_filter import (
    crm_deal_category_filter,
    normalize_crm_category_ids,
)


def test_normalize_crm_category_ids_preserves_order_and_removes_duplicates() -> None:
    assert normalize_crm_category_ids(["2", "7", "2", "8"]) == ("2", "7", "8")


@pytest.mark.parametrize("category_ids", [[""], ["  "], ["two"], ["2", "x"]])
def test_normalize_crm_category_ids_rejects_invalid_values(category_ids: list[str]) -> None:
    with pytest.raises(ValueError, match="non-empty numeric"):
        normalize_crm_category_ids(category_ids)


def test_crm_deal_category_filter_uses_bitrix_membership_syntax() -> None:
    assert crm_deal_category_filter(["2", "7", "2"]) == {"@CATEGORY_ID": ["2", "7"]}


def test_crm_deal_category_filter_rejects_empty_allowlist() -> None:
    with pytest.raises(ValueError, match="requires at least one category"):
        crm_deal_category_filter([])
