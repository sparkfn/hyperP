"""Unit tests for the incremental Bitrix CRM deal connector."""

from __future__ import annotations

from collections.abc import Collection, Iterator
from datetime import UTC, datetime

import pytest
from src.connectors.bitrix_crm.incremental import BitrixCrmDealIncrementalConnector
from src.connectors.bitrix_openlines.connector import _CrmEntityMappingError
from src.connectors.bitrix_openlines.crm_deal_filter import CrmDealPage
from src.connectors.bitrix_openlines.models import CrmContact, CrmDeal
from src.incremental_connector import IncrementalConnector
from src.ingestion_config import BitrixOpenLinesConfig

_BOUNDARY = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


class _FakeDealClient:
    """Canned-page stand-in for the Bitrix Open Lines client."""

    def __init__(self, pages: list[CrmDealPage]) -> None:
        self._pages = list(pages)
        self.categories: list[tuple[str, ...]] = []
        self.modified_since: list[datetime | None] = []
        self.closed = False

    def iter_crm_deal_pages(
        self,
        category_ids: Collection[str],
        *,
        modified_since: datetime | None = None,
    ) -> Iterator[CrmDealPage]:
        self.categories.append(tuple(category_ids))
        self.modified_since.append(modified_since)
        yield from self._pages

    def close(self) -> None:
        self.closed = True


def _config() -> BitrixOpenLinesConfig:
    return BitrixOpenLinesConfig(
        included_crm_category_ids=["1"],
        entity_by_crm_category_id={"1": "test_entity"},
    )


def _contact() -> CrmContact:
    return CrmContact(id="10", full_name="Test Contact")


def _deal(
    deal_id: str,
    observed_at: datetime | None = None,
    category_id: str = "1",
) -> CrmDeal:
    contact = _contact()
    return CrmDeal(
        id=deal_id,
        title=f"Deal {deal_id}",
        category_id=category_id,
        stage_id="NEW",
        observed_at=observed_at,
        primary_contact=contact,
        contacts=(contact,),
        contact_count=1,
        has_ambiguous_contacts=False,
        raw_payload={"ID": deal_id},
    )


def _page(*deals: CrmDeal) -> CrmDealPage:
    return CrmDealPage(deals=deals, returned_count=len(deals))


def _connector(client: _FakeDealClient) -> BitrixCrmDealIncrementalConnector:
    return BitrixCrmDealIncrementalConnector(client, _config())


def test_protocol_satisfied() -> None:
    assert isinstance(_connector(_FakeDealClient([])), IncrementalConnector)


def test_source_key() -> None:
    assert _connector(_FakeDealClient([])).get_source_key() == "bitrix_chat"


def test_bootstrap_empty_source() -> None:
    connector = _connector(_FakeDealClient([]))
    before = datetime.now(UTC)
    connector.open_query(None)
    page = connector.fetch_next_page()
    after = datetime.now(UTC)
    assert page.records == ()
    assert page.has_more is False
    assert before <= page.max_updated_at <= after


def test_delta_single_page() -> None:
    observed = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    client = _FakeDealClient([_page(_deal("100", observed))])
    connector = _connector(client)
    connector.open_query(_BOUNDARY)
    page = connector.fetch_next_page()
    assert len(page.records) == 1
    assert page.records[0]["source_record_id"] == "bitrix-crm-deal-100"
    assert page.records[0]["entity_key"] == "test_entity"
    assert page.has_more is False
    assert page.max_updated_at == observed


def test_delta_multi_page_lookahead() -> None:
    client = _FakeDealClient([_page(_deal("100", _BOUNDARY)), _page(_deal("101", _BOUNDARY))])
    connector = _connector(client)
    connector.open_query(_BOUNDARY)
    first = connector.fetch_next_page()
    second = connector.fetch_next_page()
    assert len(first.records) == 1
    assert first.has_more is True
    assert len(second.records) == 1
    assert second.has_more is False


def test_modified_since_passed_to_client() -> None:
    client = _FakeDealClient([])
    connector = _connector(client)
    connector.open_query(_BOUNDARY)
    assert client.categories == [("1",)]
    assert client.modified_since == [_BOUNDARY]
    connector.open_query(None)
    assert client.modified_since[-1] is None


def test_close_closes_client() -> None:
    client = _FakeDealClient([])
    connector = _connector(client)
    connector.open_query(None)
    connector.close()
    assert client.closed is True


def test_unmapped_category_raises() -> None:
    client = _FakeDealClient([_page(_deal("100", _BOUNDARY, category_id="2"))])
    connector = _connector(client)
    connector.open_query(_BOUNDARY)
    with pytest.raises(_CrmEntityMappingError):
        connector.fetch_next_page()


def test_watermark_tracks_max_observed_at() -> None:
    older = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    newer = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    client = _FakeDealClient(
        [
            _page(_deal("100", older)),
            _page(_deal("101", newer), _deal("102", older)),
        ]
    )
    connector = _connector(client)
    connector.open_query(_BOUNDARY)
    first = connector.fetch_next_page()
    second = connector.fetch_next_page()
    assert first.max_updated_at == older
    assert second.max_updated_at == newer
