"""Restart-safe split deal and activity connector traversal tests."""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime

from src.connectors.bitrix_crm.activity_connector import BitrixCrmActivityConnector
from src.connectors.bitrix_crm.deal_connector import BitrixCrmDealConnector
from src.connectors.bitrix_openlines.models import (
    CrmActivity,
    CrmActivityCapabilityPage,
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

    def get_deal(self, deal_id: int) -> CrmDeal:
        return CrmDeal(
            id=str(deal_id),
            title="Deal",
            category_id="2",
            stage_id="C2:NEW",
            observed_at=datetime(2026, 8, 8, tzinfo=UTC),
            primary_contact=None,
            contacts=(),
            contact_count=0,
            has_ambiguous_contacts=False,
            raw_payload={"ID": str(deal_id), "CATEGORY_ID": "2", "STAGE_ID": "C2:NEW"},
        )

    def close(self) -> None:
        pass


class _ActivityClient:
    def __init__(self, *, is_call: bool = False) -> None:
        self.lower_bounds: list[int | None] = []
        self.is_call = is_call

    def list_crm_activity_capability_page(
        self,
        *,
        greater_than_id: int | None,
        less_than_or_equal_to_id: int,
        order_direction: str = "ASC",
    ) -> CrmActivityCapabilityPage:
        assert less_than_or_equal_to_id == 11
        assert order_direction == "ASC"
        self.lower_bounds.append(greater_than_id)
        activity = CrmActivity(
            id="11",
            owner_type="2",
            owner_id="9",
            history_kind="call" if self.is_call else "email",
            subject=None,
            observed_at=datetime(2026, 8, 8, tzinfo=UTC),
            start_at=None,
            end_at=None,
            duration_seconds=None,
            direction=None,
            outcome=None,
            is_call=self.is_call,
            raw_payload={"ID": "11", "OWNER_ID": "9"},
        )
        return CrmActivityCapabilityPage((activity,), 1, None, None)

    def close(self) -> None:
        pass


def test_deal_connector_resumes_exclusive_keyset_cursor() -> None:
    client = _DealClient()
    config = BitrixOpenLinesConfig(
        included_crm_category_ids=["2"],
        entity_by_crm_category_id={"2": "eko"},
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


def test_activity_connector_resumes_exclusive_keyset_cursor() -> None:
    client = _ActivityClient()
    connector = BitrixCrmActivityConnector(
        client,
        upper_activity_id=11,
        last_activity_id=10,
    )

    records = list(connector.fetch_records())

    assert client.lower_bounds == [10]
    assert records[0]["source_record_id"] == "bitrix-crm-history-11"


def test_call_activity_marks_history_as_non_terminal_until_call_commits() -> None:
    connector = BitrixCrmActivityConnector(
        _ActivityClient(is_call=True),
        upper_activity_id=11,
        last_activity_id=10,
    )

    records = list(connector.fetch_records())

    assert [record["source_record_id"] for record in records] == [
        "bitrix-crm-history-11",
        "bitrix-call-11",
    ]
    raw_payload = records[0]["raw_payload"]
    assert isinstance(raw_payload, dict)
    assert raw_payload["has_call_record"] is True
