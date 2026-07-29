from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from src.connectors.phppos_api.client import ApiCredentials
from src.connectors.phppos_api.connectors import (
    EkoApiConnector,
    EkoSalesApiConnector,
    SpeedZoneApiConnector,
)
from src.connectors.phppos_api.models import CustomerRow, SaleRow
from src.main import create_phppos_api_client, get_connector, run_ingestion


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


@pytest.mark.parametrize(
    ("source_key", "tenant_id", "scopes"),
    [
        ("eko_phppos", "eko-tenant", ("pos.customers.read",)),
        ("speedzone_phppos", "speedzone-tenant", ("pos.customers.read",)),
        (
            "eko_phppos:sales",
            "eko-tenant",
            ("pos.sales.read", "pos.items.read", "pos.customers.read"),
        ),
        (
            "speedzone_phppos:sales",
            "speedzone-tenant",
            ("pos.sales.read", "pos.items.read", "pos.customers.read"),
        ),
    ],
)
def test_create_phppos_api_client_uses_exact_source_scopes_without_token_store(
    monkeypatch: pytest.MonkeyPatch,
    source_key: str,
    tenant_id: str,
    scopes: tuple[str, ...],
) -> None:
    settings = SimpleNamespace(
        phppos_api_base_url="https://pos.example",
        phppos_api_client_id="client",
        phppos_api_client_secret=SimpleNamespace(get_secret_value=lambda: "secret"),
        phppos_api_page_size=250,
        phppos_api_timeout_seconds=12.5,
        phppos_api_max_attempts=4,
        eko_phppos_api_tenant_id="eko-tenant",
        speedzone_phppos_api_tenant_id="speedzone-tenant",
    )
    sentinel_client = object()
    sentinel_http = object()
    client_factory = Mock(return_value=sentinel_client)
    http_factory = Mock(return_value=sentinel_http)
    monkeypatch.setattr("src.main.get_settings", lambda: settings)
    monkeypatch.setattr("src.main.PhpposApiClient", client_factory)
    monkeypatch.setattr("src.main.httpx.Client", http_factory)

    assert create_phppos_api_client(source_key) is sentinel_client
    http_factory.assert_called_once_with(timeout=12.5)
    client_factory.assert_called_once_with(
        ApiCredentials(
            base_url="https://pos.example",
            client_id="client",
            client_secret="secret",
            tenant_id=tenant_id,
            page_size=250,
            scopes=scopes,
        ),
        http=sentinel_http,
        max_attempts=4,
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
        lambda *, incremental=True: whatsapp_connector,
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


def test_run_ingestion_marks_run_failed_when_connector_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The IngestRun is created before the connector is built, so a
    # connector-construction failure (e.g. a source dispatched before its env
    # is provisioned) is recorded as a failed run instead of vanishing from
    # the runs UI.
    class GraphClient:
        def verify_connectivity(self) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr("src.main.get_settings", lambda: object())
    monkeypatch.setattr("src.main.Neo4jClient", lambda _settings: GraphClient())
    monkeypatch.setattr("src.main.IngestPipeline", lambda _client: object())
    monkeypatch.setattr("src.main._create_ingest_run", lambda *_args: "run-1")

    failed_runs: list[str] = []

    def _capture_failed_run(_client: object, run_id: str, *_rest: object) -> None:
        failed_runs.append(run_id)

    monkeypatch.setattr("src.main._mark_run_failed", _capture_failed_run)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("connector failed")

    monkeypatch.setattr("src.main.get_connector", boom)

    with pytest.raises(RuntimeError, match="connector failed"):
        run_ingestion("eko_phppos", mode="api", initialize_graph=False)

    assert failed_runs == ["run-1"]


def test_run_ingestion_closes_api_connector_when_ingestion_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Connector resources are still released when ingestion fails *after* the
    # connector is built (the run-creation-fails path can no longer happen,
    # since the run is created before the connector).
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
    monkeypatch.setattr("src.main._create_ingest_run", lambda *_args: "run-1")
    monkeypatch.setattr("src.main._mark_run_failed", lambda *_args: None)
    monkeypatch.setattr("src.main.get_connector", lambda *_args, **_kwargs: connector)
    monkeypatch.setattr("src.main._load_exclusion_context", lambda: object())

    def boom(*_args: object, **_kwargs: object) -> tuple[int, int, int]:
        raise RuntimeError("ingestion failed")

    monkeypatch.setattr("src.main._ingest_all_records", boom)

    with pytest.raises(RuntimeError, match="ingestion failed"):
        run_ingestion("eko_phppos", mode="api", initialize_graph=False)

    assert api_client.closed is True
