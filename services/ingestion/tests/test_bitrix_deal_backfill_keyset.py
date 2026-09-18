"""Retirement of the split deal connector's legacy keyset traversal.

The exclusive ``last_deal_id`` keyset path was replaced by the bounded adapter,
so ``fetch_records`` refuses for every frozen window, including an already
exhausted one.
"""

from __future__ import annotations

import re
from collections.abc import Collection

import pytest
from src.bitrix_ingestion_models import BITRIX_LEGACY_OPENLINES_RETIRED_REASON
from src.connectors.bitrix_crm.deal_connector import BitrixCrmDealConnector
from src.connectors.bitrix_openlines.models import CrmDeal, CrmDealCapabilityPage
from src.ingestion_config import BitrixOpenLinesConfig

_LEGACY_REFUSAL = re.escape(BITRIX_LEGACY_OPENLINES_RETIRED_REASON)


class _UntouchableDealClient:
    """A deal client that fails if the retired keyset path performs any read."""

    @property
    def request_count(self) -> int:
        return 0

    def list_crm_deal_capability_page(
        self,
        *,
        category_ids: Collection[str],
        greater_than_id: int | None = None,
        less_than_or_equal_to_id: int | None = None,
        order_direction: str = "ASC",
    ) -> CrmDealCapabilityPage:
        raise AssertionError("the retired keyset path must not read a capability page")

    def get_deals(self, deal_ids: Collection[int]) -> list[CrmDeal]:
        raise AssertionError("the retired keyset path must not hydrate deals")

    def close(self) -> None:
        return None


def _connector(*, last_deal_id: int | None = None) -> BitrixCrmDealConnector:
    return BitrixCrmDealConnector(
        _UntouchableDealClient(),
        BitrixOpenLinesConfig(
            included_crm_category_ids=["2"],
            entity_by_crm_category_id={"2": "eko"},
            source_instance_id="bitrix-primary",
        ),
        upper_deal_id=9,
        last_deal_id=last_deal_id,
    )


def test_legacy_keyset_traversal_is_refused() -> None:
    """A resumable frozen window refuses instead of resuming the keyset."""
    connector = _connector(last_deal_id=8)

    with pytest.raises(RuntimeError, match=_LEGACY_REFUSAL):
        list(connector.fetch_records())


def test_legacy_keyset_refuses_an_exhausted_frozen_window() -> None:
    """A window that used to short-circuit to no records is refused instead."""
    connector = _connector(last_deal_id=9)

    with pytest.raises(RuntimeError, match=_LEGACY_REFUSAL):
        list(connector.fetch_records())
