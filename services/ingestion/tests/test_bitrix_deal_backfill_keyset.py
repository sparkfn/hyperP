"""Restart-safe split deal connector traversal tests."""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime

from src.connectors.bitrix_crm.deal_connector import BitrixCrmDealConnector
from src.connectors.bitrix_openlines.models import (
    CrmContact,
    CrmDeal,
    CrmDealCapabilityItem,
    CrmDealCapabilityPage,
)
from src.ingestion_config import BitrixOpenLinesConfig


class _DealClient:
    def __init__(self) -> None:
        self.lower_bounds: list[int | None] = []

    def list_crm_deal_capability_page(
        self,
        *,
        category_ids: Collection[str],
        greater_than_id: int | None = None,
        less_than_or_equal_to_id: int | None = None,
        order_direction: str = "ASC",
    ) -> CrmDealCapabilityPage:
        assert tuple(category_ids) == ("2",)
        assert less_than_or_equal_to_id == 9
        assert order_direction == "ASC"
        self.lower_bounds.append(greater_than_id)
        return CrmDealCapabilityPage(
            (CrmDealCapabilityItem("9", "2", "C2:NEW"),),
            None,
            1,
            None,
            None,
        )

    def get_deals(self, deal_ids: Collection[int]) -> list[CrmDeal]:
        return [
            CrmDeal(
                id=str(deal_id),
                title="Deal",
                category_id="2",
                stage_id="C2:NEW",
                observed_at=datetime(2026, 8, 8, tzinfo=UTC),
                primary_contact=CrmContact(
                    id="123",
                    full_name="Ada Lovelace",
                    phones=(),
                    emails=(),
                ),
                contacts=(
                    CrmContact(
                        id="123",
                        full_name="Ada Lovelace",
                        phones=(),
                        emails=(),
                    ),
                ),
                contact_count=1,
                has_ambiguous_contacts=False,
                raw_payload={
                    "ID": str(deal_id),
                    "CATEGORY_ID": "2",
                    "STAGE_ID": "C2:NEW",
                },
            )
            for deal_id in deal_ids
        ]

    def close(self) -> None:
        pass


def test_deal_connector_resumes_exclusive_keyset_cursor() -> None:
    client = _DealClient()
    config = BitrixOpenLinesConfig(
        included_crm_category_ids=["2"],
        entity_by_crm_category_id={"2": "eko"},
        source_instance_id="bitrix-primary",
    )
    connector = BitrixCrmDealConnector(
        client,
        config,
        upper_deal_id=9,
        last_deal_id=8,
    )

    records = list(connector.fetch_records())

    assert client.lower_bounds == [8]
    assert records[0]["source_record_id"] == "bitrix-crm-deal-9"
    assert records[0]["identifiers"][0]["source_instance_id"] == "bitrix-primary"
    raw_payload = records[0]["raw_payload"]
    assert isinstance(raw_payload, dict)
    assert raw_payload["category_id"] == "2"
    assert raw_payload["stage_id"] == "C2:NEW"
    assert "CATEGORY_ID" not in raw_payload
    assert "STAGE_ID" not in raw_payload


def test_deal_connector_returns_empty_for_an_exhausted_frozen_window() -> None:
    client = _DealClient()
    config = BitrixOpenLinesConfig(
        included_crm_category_ids=["2"],
        entity_by_crm_category_id={"2": "eko"},
    )
    connector = BitrixCrmDealConnector(
        client,
        config,
        upper_deal_id=9,
        last_deal_id=9,
    )

    assert list(connector.fetch_records()) == []
    assert client.lower_bounds == []
