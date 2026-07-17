from __future__ import annotations

from collections.abc import Iterator

import pytest
from src.connectors.phppos_api.connectors import (
    EkoApiConnector,
    EkoSalesApiConnector,
    SpeedZoneApiConnector,
)
from src.connectors.phppos_api.models import CustomerRow, SaleRow
from src.main import get_connector, run_ingestion


class StubClient:
    def __init__(self) -> None:
        self.closed = False

    def iter_customers(self) -> Iterator[CustomerRow]:
        yield CustomerRow.model_validate(
            {
                "person_id": 7,
                "full_name": "Ada Rider",
                "email": "ada@example.com",
                "phone_number": "81234567",
                "phone_code": "65",
                "country": "Singapore",
                "create_date": "2026-01-01T00:00:00",
                "last_modified": "2026-02-01T00:00:00",
                "custom_field_1_value": "S1234567A",
                "custom_field_2_value": "BX-7",
                "custom_field_9_value": "1990-01-02",
            }
        )

    def iter_sales(self) -> Iterator[SaleRow]:
        return iter(())

    def close(self) -> None:
        self.closed = True


class StubSalesClient(StubClient):
    def iter_sales(self) -> Iterator[SaleRow]:
        yield SaleRow.model_validate(
            {
                "sale_id": 9,
                "sale_time": "2026-02-01T10:00:00",
                "customer_id": 7,
                "customer_email": "ada@example.com",
                "customer_nric": "S1234567A",
                "lines": [
                    {
                        "sale_id": 9,
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


def test_eko_api_connector_reuses_canonical_customer_mapping() -> None:
    record = list(EkoApiConnector(StubClient()).fetch_records())[0]
    assert record["source_record_id"] == "eko_phppos-customer-7"
    assert record["attributes"]["dob"] == "1990-01-02"  # type: ignore[index]
    assert record["identifiers"][0]["type"] == "nric"  # type: ignore[index]


def test_speedzone_api_connector_preserves_bitrix_identifier() -> None:
    record = list(SpeedZoneApiConnector(StubClient()).fetch_records())[0]
    assert record["source_record_id"] == "speedzone_phppos-customer-7"
    identifiers = record["identifiers"]
    assert any(item["type"] == "external:bitrix" for item in identifiers)  # type: ignore[union-attr]


def test_api_mode_supports_pos_and_whatsapp_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = StubClient()
    whatsapp_connector = EkoApiConnector(sentinel)
    monkeypatch.setattr("src.main.create_phppos_api_client", lambda _source: sentinel)
    monkeypatch.setattr(
        "src.main.create_whatsadmin_api_connector",
        lambda: whatsapp_connector,
        raising=False,
    )
    assert isinstance(get_connector("eko_phppos", mode="api"), EkoApiConnector)
    assert isinstance(get_connector("speedzone_phppos", mode="api"), SpeedZoneApiConnector)
    assert get_connector("whatsapp_chat", mode="api") is whatsapp_connector


def test_sales_api_connector_builds_existing_sales_envelope() -> None:
    client = StubSalesClient()
    record = list(EkoSalesApiConnector(client).fetch_records())[0]
    assert record["source_record_id"] == "eko_phppos-sale-9"
    raw = record["raw_payload"]
    assert raw["customer_link"]["identity_source_record_id"] == "eko_phppos-customer-7"  # type: ignore[index]
    assert raw["line_items"][0]["line_total"] == 24.0  # type: ignore[index]
    assert raw["line_items"][0]["product"]["category"] == "Accessories"  # type: ignore[index]
    assert client.closed is True


class MinimalCustomerClient(StubClient):
    def iter_customers(self) -> Iterator[CustomerRow]:
        yield CustomerRow.model_validate({"person_id": 8})


def test_customer_api_connector_treats_omitted_optional_fields_as_null() -> None:
    client = MinimalCustomerClient()
    record = list(EkoApiConnector(client).fetch_records())[0]
    assert record["source_record_id"] == "eko_phppos-customer-8"
    assert "dob" not in record["attributes"]  # type: ignore[operator]
    assert client.closed is True


def test_run_ingestion_closes_api_connector_when_run_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_client = StubClient()
    connector = EkoApiConnector(api_client)

    class GraphClient:
        def verify_connectivity(self) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr("src.main.get_settings", lambda: object())
    monkeypatch.setattr("src.main.Neo4jClient", lambda _settings: GraphClient())
    monkeypatch.setattr("src.main.IngestPipeline", lambda _client: object())
    monkeypatch.setattr("src.main.get_connector", lambda *_args, **_kwargs: connector)

    def fail_create_run(*_args: object) -> str:
        raise RuntimeError("run creation failed")

    monkeypatch.setattr("src.main._create_ingest_run", fail_create_run)

    with pytest.raises(RuntimeError, match="run creation failed"):
        run_ingestion("eko_phppos", mode="api", initialize_graph=False)

    assert api_client.closed is True
