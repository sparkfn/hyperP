from __future__ import annotations

from collections.abc import Iterator

import pytest
from src.connectors.fundbox_api.connectors import (
    FundboxContactsApiConnector,
    FundboxSalesApiConnector,
    FundboxUsersApiConnector,
)
from src.main import get_connector
from src.models import JsonValue


class StubClient:
    def __init__(self, records: list[dict[str, JsonValue]]) -> None:
        self.records = records
        self.closed = False
        self.calls: list[tuple[str, str | None]] = []

    def iter_source(
        self, resource: str, *, updated_since: str | None = None
    ) -> Iterator[dict[str, JsonValue]]:
        self.calls.append((resource, updated_since))
        yield from self.records

    def close(self) -> None:
        self.closed = True


def test_users_api_connector_preserves_user_envelope_contract() -> None:
    client = StubClient(
        [
            {
                "effective_updated_at": "2026-07-17T00:00:00Z",
                "user": {
                    "id": 7,
                    "email": "ada@example.com",
                    "mobile_number": "81234567",
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-07-17T00:00:00Z",
                },
                "basic_profile": {
                    "nric": "S1234567A",
                    "full_name": "Ada Rider",
                    "date_of_birth": "1990-01-02",
                    "gender": "female",
                    "nationality": "SG",
                    "race": "MALAY",
                    "email": None,
                    "mobile_number": None,
                },
                "basic_plus_profile": {"whatsapp_phone": None, "facebook_id": None},
                "addresses": [],
                "social_accounts": [],
                "device_ids": [],
                "last_login": None,
            }
        ]
    )
    record = list(FundboxUsersApiConnector(client).fetch_records())[0]
    assert record["source_record_id"] == "fundbox_consumer_backend-user-7"
    assert record["attributes"]["full_name"] == "Ada Rider"  # type: ignore[index]
    assert record["identifiers"][0]["type"] == "nric"  # type: ignore[index]
    assert client.closed is True


def test_contacts_api_connector_preserves_relationship_link() -> None:
    client = StubClient(
        [
            {
                "effective_updated_at": "2026-07-17T00:00:00Z",
                "contact": {
                    "id": 9,
                    "user_id": 7,
                    "mobile_number": "81112222",
                    "full_name": "Grace",
                    "relationship": "sister",
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-07-17T00:00:00Z",
                },
            }
        ]
    )
    record = list(FundboxContactsApiConnector(client).fetch_records())[0]
    assert record["record_type"] == "relationship"
    assert record["raw_payload"]["linked_to_source_record_id"] == "fundbox_consumer_backend-user-7"  # type: ignore[index]


def test_sales_api_connector_preserves_sales_customer_link() -> None:
    client = StubClient(
        [
            {
                "effective_updated_at": "2026-07-17T00:00:00Z",
                "order": {
                    "id": 11,
                    "user_id": 7,
                    "merchant_id": 3,
                    "merchant_staff_id": None,
                    "order_no": "FB-11",
                    "status": "completed",
                    "total_amount": "100.00",
                    "total_items": 1,
                    "transaction_reference": "tx",
                    "release_date": None,
                    "expiry_at": None,
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-07-17T00:00:00Z",
                },
                "merchant": {"id": 3, "name": "Cycles", "official_name": None},
                "items": [
                    {
                        "order_item": {
                            "id": 4,
                            "merchant_product_id": 8,
                            "quantity": 1,
                            "price": "100.00",
                            "lta_tag": None,
                            "serial_no": "S1",
                        },
                        "merchant_product": {"id": 8, "product_variant_id": 6},
                        "product_variant": {
                            "id": 6,
                            "product_id": 5,
                            "sku": "BIKE",
                            "name": "Bike",
                            "active": 1,
                            "attributes": '{"colour":"red"}',
                        },
                        "product": {
                            "id": 5,
                            "name": "Road Bike",
                            "category": "bicycles",
                            "sub_category": None,
                            "make": "Ada",
                            "model": "R1",
                            "type": None,
                            "sub_type": None,
                            "has_serial_number": 1,
                            "has_lta_tag": 0,
                        },
                    }
                ],
                "customer": {
                    "user": {"id": 7, "email": "ada@example.com", "mobile_number": "81234567"},
                    "basic_profile": {"email": None, "mobile_number": None, "nric": "S1234567A"},
                },
            }
        ]
    )
    record = list(FundboxSalesApiConnector(client).fetch_records())[0]
    assert record["source_record_id"] == "fundbox_consumer_backend-order-11"
    assert record["raw_payload"]["order"]["total_amount"] == 100.0  # type: ignore[index]
    assert record["raw_payload"]["line_items"][0]["unit_price"] == 100.0  # type: ignore[index]
    assert record["raw_payload"]["line_items"][0]["line_total"] == 100.0  # type: ignore[index]
    assert record["raw_payload"]["line_items"][0]["product"]["attributes"][  # type: ignore[index]
        "variant_attributes"
    ] == {"colour": "red"}
    assert (
        record["raw_payload"]["customer_link"]["identity_source_record_id"]
        == "fundbox_consumer_backend-user-7"
    )  # type: ignore[index]
    assert record["raw_payload"]["line_items"][0]["product"]["model"] == "R1"  # type: ignore[index]


def test_api_mode_routes_only_scheduled_fundbox_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StubClient([])
    monkeypatch.setattr("src.main.create_fundbox_api_client", lambda: client)
    monkeypatch.setattr("src.main.load_watermark", lambda *_args: None)
    monkeypatch.setattr("src.main.load_source_ids", lambda *_args: None)

    assert isinstance(
        get_connector("fundbox_consumer_backend", mode="api"), FundboxUsersApiConnector
    )
    assert isinstance(
        get_connector("fundbox_consumer_backend:contacts", mode="api"),
        FundboxContactsApiConnector,
    )
    assert isinstance(
        get_connector("fundbox_consumer_backend:sales", mode="api"),
        FundboxSalesApiConnector,
    )

    with pytest.raises(ValueError, match="API mode"):
        get_connector("fundbox_consumer_backend:legacy", mode="api")


def test_connector_reconciles_missing_source_ids_after_full_snapshot() -> None:
    client = StubClient(
        [
            {
                "effective_updated_at": "2026-07-17T00:00:00Z",
                "contact": {"id": 2, "user_id": 7},
            }
        ]
    )
    connector = FundboxContactsApiConnector(
        client,
        updated_since="2026-07-16T00:00:00Z",
        previous_source_ids={1, 2},
    )

    records = list(connector.fetch_records())

    assert [record["source_record_id"] for record in records[:-1]] == [
        "fundbox_consumer_backend-contact-2"
    ]
    assert records[-1]["_retire_source_record_id"] == "fundbox_consumer_backend-contact-1"
    assert client.calls == [
        ("contacts", "2026-07-16T00:00:00Z"),
        ("contacts", None),
    ]
    assert connector.current_source_ids == {2}
    assert connector.reconciliation_completed is True


def test_first_snapshot_does_not_retire_unobserved_source_ids() -> None:
    client = StubClient([])
    connector = FundboxContactsApiConnector(client, previous_source_ids=None)

    assert list(connector.fetch_records()) == []
    assert connector.current_source_ids == set()
    assert connector.reconciliation_completed is True


@pytest.mark.parametrize(
    ("connector_type", "expected_source_record_id"),
    [
        (FundboxUsersApiConnector, "fundbox_consumer_backend-user-8"),
        (FundboxContactsApiConnector, "fundbox_consumer_backend-contact-8"),
        (FundboxSalesApiConnector, "fundbox_consumer_backend-order-8"),
    ],
)
def test_all_scheduled_sources_retire_roots_missing_from_full_snapshot(
    connector_type: type[
        FundboxUsersApiConnector | FundboxContactsApiConnector | FundboxSalesApiConnector
    ],
    expected_source_record_id: str,
) -> None:
    connector = connector_type(StubClient([]), previous_source_ids={8})

    records = list(connector.fetch_records())

    assert records[0]["_retire_source_record_id"] == expected_source_record_id


def test_full_snapshot_reprocesses_records_absent_from_incremental_pass() -> None:
    class TwoPassClient(StubClient):
        def iter_source(
            self, resource: str, *, updated_since: str | None = None
        ) -> Iterator[dict[str, JsonValue]]:
            self.calls.append((resource, updated_since))
            if updated_since is None:
                yield {
                    "effective_updated_at": "2026-07-01T00:00:00Z",
                    "contact": {"id": 4, "user_id": 7},
                }

    client = TwoPassClient([])
    connector = FundboxContactsApiConnector(
        client,
        updated_since="2026-07-16T00:00:00Z",
        previous_source_ids=set(),
    )

    records = list(connector.fetch_records())

    assert [record["source_record_id"] for record in records] == [
        "fundbox_consumer_backend-contact-4"
    ]
    assert connector.current_source_ids == {4}


def test_connector_close_releases_client_before_iteration() -> None:
    client = StubClient([])
    connector = FundboxContactsApiConnector(client)

    connector.close()
    connector.close()

    assert client.closed is True
    assert client.calls == []
