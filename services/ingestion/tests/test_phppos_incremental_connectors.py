"""Unit tests for the PHPPOS IncrementalConnector wrappers (#434)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from src.connectors.base import SourceConnector
from src.connectors.phppos_api.connectors import (
    EkoApiConnector,
    EkoSalesApiConnector,
    SpeedZoneApiConnector,
    SpeedZoneSalesApiConnector,
)
from src.connectors.phppos_api.models import (
    CustomerPage,
    CustomerRow,
    Pagination,
    SaleRow,
    SalesPage,
)
from src.incremental_connector import IncrementalConnector
from src.models import JsonValue

_ALL_CONNECTOR_TYPES: list[type[SourceConnector]] = [
    EkoApiConnector,
    SpeedZoneApiConnector,
    EkoSalesApiConnector,
    SpeedZoneSalesApiConnector,
]


def _customer_row(person_id: int, last_modified: str = "2026-01-01T00:00:00") -> CustomerRow:
    return CustomerRow.model_validate(
        {
            "person_id": person_id,
            "full_name": "Ada Rider",
            "email": "ada@example.com",
            "phone_number": "81234567",
            "phone_code": "65",
            "country": "Singapore",
            "create_date": "2026-01-01T00:00:00",
            "last_modified": last_modified,
            "custom_field_1_value": "S1234567A",
            "custom_field_2_value": "BX-7",
            "custom_field_9_value": "1990-01-02",
        }
    )


def _sale_row(sale_id: int, sale_time: str = "2026-02-01T10:00:00") -> SaleRow:
    return SaleRow.model_validate(
        {
            "sale_id": sale_id,
            "sale_time": sale_time,
            "customer_id": 7,
            "customer_email": "ada@example.com",
            "customer_phone": "81234567",
            "customer_nric": "S1234567A",
            "customer_custom_field_8": "BX-7",
            "lines": [
                {
                    "sale_id": sale_id,
                    "line": 0,
                    "item_id": 3,
                    "quantity_purchased": "2",
                    "item_unit_price": "12.50",
                    "discount": "1.00",
                    "item_name": "Helmet",
                    "item_number": "H-1",
                    "category_id": 4,
                    "category_name": "Accessories",
                }
            ],
        }
    )


def _customer_page(
    rows: list[CustomerRow],
    *,
    has_more: bool = False,
    next_cursor: str | None = None,
) -> CustomerPage:
    pagination = Pagination(next_cursor=next_cursor, has_more=has_more)
    return CustomerPage(data=rows, pagination=pagination)


def _sales_page(
    rows: list[SaleRow],
    *,
    has_more: bool = False,
    next_cursor: str | None = None,
) -> SalesPage:
    pagination = Pagination(next_cursor=next_cursor, has_more=has_more)
    return SalesPage(data=rows, pagination=pagination)


class StubPageClient:
    """ApiClient stand-in serving canned PHPPOS pages and recording calls."""

    def __init__(
        self,
        customer_pages: list[CustomerPage] | None = None,
        sales_pages: list[SalesPage] | None = None,
    ) -> None:
        self._customer_pages = list(customer_pages or [])
        self._sales_pages = list(sales_pages or [])
        self.customer_calls: list[tuple[str | None, str | None]] = []
        self.sales_calls: list[tuple[str | None, str | None]] = []
        self.closed = False

    def fetch_customer_page(self, cursor: str | None, updated_since: str | None) -> CustomerPage:
        self.customer_calls.append((cursor, updated_since))
        return self._customer_pages[len(self.customer_calls) - 1]

    def fetch_sales_page(self, cursor: str | None, updated_since: str | None) -> SalesPage:
        self.sales_calls.append((cursor, updated_since))
        return self._sales_pages[len(self.sales_calls) - 1]

    def iter_customers(self, *, updated_since: str | None = None) -> Iterator[CustomerRow]:
        return iter(())

    def iter_sales(self, *, updated_since: str | None = None) -> Iterator[SaleRow]:
        return iter(())

    def close(self) -> None:
        self.closed = True


class FakeRedis:
    """Minimal in-memory Redis stand-in."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, name: str) -> bytes | str | None:
        return self._store.get(name)

    def set(self, name: str, value: str | bytes, **kwargs: object) -> object:
        self._store[name] = value.decode("utf-8") if isinstance(value, bytes) else value
        return True

    def delete(self, *names: str) -> object:
        for n in names:
            self._store.pop(n, None)
        return len(names)


class FakeNeo4jTx:
    """Stub for ManagedTransaction."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **kwargs: object) -> FakeNeo4jTx:
        self.calls.append((query, kwargs))
        return self

    def single(self) -> dict[str, object] | None:
        return {"ingest_run_id": "run-001", "status": "started", "created": True}


class FakeNeo4jSession:
    """Stub for Neo4j Session."""

    def __init__(self) -> None:
        self.tx = FakeNeo4jTx()

    def execute_write(self, fn: object) -> object:
        assert isinstance(fn, Callable)
        return fn(self.tx)

    def __enter__(self) -> FakeNeo4jSession:
        return self

    def __exit__(self, *args: object) -> None:
        pass


class FakeNeo4jClient:
    """Stub for Neo4jClient."""

    def __init__(self) -> None:
        self._session = FakeNeo4jSession()

    def session(self) -> FakeNeo4jSession:
        return self._session


def _stopping_after_first_check() -> Callable[[], bool]:
    checks: list[int] = []

    def _signal() -> bool:
        checks.append(1)
        return len(checks) > 1

    return _signal


@pytest.mark.parametrize("connector_type", _ALL_CONNECTOR_TYPES)
def test_protocol_satisfaction(connector_type: type[SourceConnector]) -> None:
    assert isinstance(connector_type(StubPageClient()), IncrementalConnector)


def test_single_page_customer_fetch_eko() -> None:
    client = StubPageClient(customer_pages=[_customer_page([_customer_row(7)])])
    connector = EkoApiConnector(client)
    connector.open_query(None)

    page = connector.fetch_next_page()

    assert page.has_more is False
    assert client.customer_calls == [(None, None)]
    record = page.records[0]
    assert record["source_record_id"] == "eko_phppos-customer-7"
    raw = record["raw_payload"]
    assert isinstance(raw, dict)
    assert set(raw) == {"person", "loyalty"}


def test_single_page_customer_fetch_speedzone() -> None:
    client = StubPageClient(customer_pages=[_customer_page([_customer_row(7)])])
    connector = SpeedZoneApiConnector(client)
    connector.open_query(None)

    page = connector.fetch_next_page()

    record = page.records[0]
    assert record["source_record_id"] == "speedzone_phppos-customer-7"
    identifiers = record["identifiers"]
    assert isinstance(identifiers, list)
    assert any(item["type"] == "external:bitrix" for item in identifiers)


def test_single_page_sales_fetch() -> None:
    client = StubPageClient(sales_pages=[_sales_page([_sale_row(9)])])
    connector = EkoSalesApiConnector(client)
    connector.open_query(None)

    page = connector.fetch_next_page()

    assert page.has_more is False
    assert page.records[0]["source_record_id"] == "eko_phppos-sale-9"


def test_multi_page_pagination_customer() -> None:
    pages = [
        _customer_page([_customer_row(7)], has_more=True, next_cursor="cursor-2"),
        _customer_page([_customer_row(8)]),
    ]
    client = StubPageClient(customer_pages=pages)
    connector = EkoApiConnector(client)
    connector.open_query(None)

    first = connector.fetch_next_page()
    second = connector.fetch_next_page()

    assert first.has_more is True
    assert second.has_more is False
    assert [call[0] for call in client.customer_calls] == [None, "cursor-2"]
    assert [record["source_record_id"] for record in first.records + second.records] == [
        "eko_phppos-customer-7",
        "eko_phppos-customer-8",
    ]


def test_updated_since_passthrough() -> None:
    client = StubPageClient(customer_pages=[_customer_page([_customer_row(7)])])
    connector = EkoApiConnector(client)

    connector.open_query(datetime(2026, 8, 2, 1, 0, 0, tzinfo=UTC))
    connector.fetch_next_page()

    assert client.customer_calls == [(None, "2026-08-02T01:00:00+00:00")]


def test_max_updated_at_tracking() -> None:
    rows = [_customer_row(7, "2026-01-01T00:00:00"), _customer_row(8, "2026-03-01T00:00:00")]
    client = StubPageClient(customer_pages=[_customer_page(rows)])
    connector = SpeedZoneApiConnector(client)
    connector.open_query(None)

    page = connector.fetch_next_page()

    assert page.max_updated_at == datetime(2026, 3, 1, tzinfo=UTC)


def test_empty_page() -> None:
    client = StubPageClient(customer_pages=[_customer_page([])])
    connector = EkoApiConnector(client)
    connector.open_query(None)

    page = connector.fetch_next_page()

    assert page.records == ()
    assert page.has_more is False
    assert page.max_updated_at == datetime.min.replace(tzinfo=UTC)


@pytest.mark.parametrize("connector_type", _ALL_CONNECTOR_TYPES)
def test_close_delegation(connector_type: type[SourceConnector]) -> None:
    client = StubPageClient()

    connector_type(client).close()

    assert client.closed is True


@pytest.mark.parametrize(
    ("connector_type", "source_key"),
    [
        (EkoApiConnector, "eko_phppos"),
        (SpeedZoneApiConnector, "speedzone_phppos"),
        (EkoSalesApiConnector, "eko_phppos:sales"),
        (SpeedZoneSalesApiConnector, "speedzone_phppos:sales"),
    ],
)
def test_get_source_key_all(connector_type: type[SourceConnector], source_key: str) -> None:
    assert connector_type(StubPageClient()).get_source_key() == source_key


def test_sales_mapping_eko() -> None:
    client = StubPageClient(sales_pages=[_sales_page([_sale_row(9)])])
    connector = EkoSalesApiConnector(client)
    connector.open_query(None)

    record = connector.fetch_next_page().records[0]

    raw = record["raw_payload"]
    assert isinstance(raw, dict)
    assert raw["customer_link"]["identity_source_record_id"] == "eko_phppos-customer-7"
    line_items = raw["line_items"]
    assert isinstance(line_items, list)
    assert line_items[0]["line_total"] == 24.0
    assert line_items[0]["metadata"]["lta_tag"] is None


def test_sales_mapping_speedzone() -> None:
    client = StubPageClient(sales_pages=[_sales_page([_sale_row(9)])])
    connector = SpeedZoneSalesApiConnector(client)
    connector.open_query(None)

    record = connector.fetch_next_page().records[0]

    assert record["source_record_id"] == "speedzone_phppos-sale-9"
    raw = record["raw_payload"]
    assert isinstance(raw, dict)
    line_items = raw["line_items"]
    assert isinstance(line_items, list)
    assert line_items[0]["metadata"]["lta_tag"] == "BX-7"


def test_fetch_records_backward_compat() -> None:
    pages = [
        _customer_page([_customer_row(7)], has_more=True, next_cursor="cursor-2"),
        _customer_page([_customer_row(8)]),
    ]
    drain_client = StubPageClient(customer_pages=list(pages))
    drain_connector = EkoApiConnector(drain_client)
    drain_connector.open_query(None)
    drained: list[dict[str, JsonValue]] = []
    while True:
        page = drain_connector.fetch_next_page()
        drained.extend(page.records)
        if not page.has_more:
            break

    fetch_client = StubPageClient(customer_pages=list(pages))
    fetched = list(EkoApiConnector(fetch_client).fetch_records())

    assert fetched == drained
    assert fetch_client.closed is True


def test_registry_keys() -> None:
    from src.tasks import INCREMENTAL_CONNECTORS

    assert set(INCREMENTAL_CONNECTORS) == {
        "eko_phppos",
        "eko_phppos:sales",
        "speedzone_phppos",
        "speedzone_phppos:sales",
    }
    assert all(callable(factory) for factory in INCREMENTAL_CONNECTORS.values())


@patch("src.watermark_runner._load_exclusion_context")
@patch("src.watermark_runner.IngestPipeline")
@patch("src.watermark_runner._process_page_records", return_value=(1, 0))
def test_runner_drains_pages_and_advances_watermark(
    mock_process: MagicMock,
    mock_pipeline: MagicMock,
    mock_exclusion: MagicMock,
) -> None:
    from src.watermark_runner import run_incremental
    from src.watermark_store import load_watermark

    redis = FakeRedis()
    pages = [
        _customer_page([_customer_row(7)], has_more=True, next_cursor="cursor-2"),
        _customer_page([_customer_row(8, "2026-09-02T10:00:00")]),
    ]
    client = StubPageClient(customer_pages=pages)
    connector = EkoApiConnector(client)

    result = run_incremental(
        connector,
        redis,  # type: ignore[arg-type]
        FakeNeo4jClient(),  # type: ignore[arg-type]
        shutdown_signal=lambda: False,
        time_window_closing=lambda: False,
    )

    assert result["status"] == "caught_up"
    assert result["pages_processed"] == 2
    assert result["records_processed"] == 2
    assert result["watermark_start"] is None
    expected_end = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
    assert result["watermark_end"] == expected_end.isoformat()

    assert load_watermark(redis, "eko_phppos").updated_at == expected_end
    assert client.closed is True


@patch("src.watermark_runner._load_exclusion_context")
@patch("src.watermark_runner.IngestPipeline")
@patch("src.watermark_runner._process_page_records", return_value=(1, 0))
def test_runner_safe_stop_does_not_advance_watermark(
    mock_process: MagicMock,
    mock_pipeline: MagicMock,
    mock_exclusion: MagicMock,
) -> None:
    from src.watermark_runner import run_incremental
    from src.watermark_store import load_watermark

    redis = FakeRedis()
    pages = [
        _customer_page([_customer_row(7)], has_more=True, next_cursor="cursor-2"),
        _customer_page([_customer_row(8)]),
    ]
    client = StubPageClient(customer_pages=pages)
    connector = EkoApiConnector(client)

    result = run_incremental(
        connector,
        redis,  # type: ignore[arg-type]
        FakeNeo4jClient(),  # type: ignore[arg-type]
        shutdown_signal=_stopping_after_first_check(),
        time_window_closing=lambda: False,
    )

    assert result["status"] == "yielded"
    assert result["pages_processed"] == 1
    assert result["watermark_end"] is None
    assert load_watermark(redis, "eko_phppos").updated_at is None
    assert client.closed is True


@patch("src.watermark_runner._load_exclusion_context")
@patch("src.watermark_runner.IngestPipeline")
@patch("src.watermark_runner._process_page_records", return_value=(1, 0))
def test_runner_time_window_closing_does_not_advance_watermark(
    mock_process: MagicMock,
    mock_pipeline: MagicMock,
    mock_exclusion: MagicMock,
) -> None:
    from src.watermark_runner import run_incremental
    from src.watermark_store import load_watermark

    redis = FakeRedis()
    pages = [
        _customer_page([_customer_row(7)], has_more=True, next_cursor="cursor-2"),
        _customer_page([_customer_row(8)]),
    ]
    client = StubPageClient(customer_pages=pages)
    connector = EkoApiConnector(client)

    result = run_incremental(
        connector,
        redis,  # type: ignore[arg-type]
        FakeNeo4jClient(),  # type: ignore[arg-type]
        shutdown_signal=lambda: False,
        time_window_closing=_stopping_after_first_check(),
    )

    assert result["status"] == "yielded"
    assert result["pages_processed"] == 1
    assert result["watermark_end"] is None
    assert load_watermark(redis, "eko_phppos").updated_at is None
    assert client.closed is True
