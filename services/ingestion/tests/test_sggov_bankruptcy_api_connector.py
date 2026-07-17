from collections.abc import Iterator

import httpx
import pytest
from pydantic import ValidationError
from src.config import Settings

from src.connectors.sggov.bankruptcy_api import SGGovernmentBankruptcyApiConnector


def test_api_connector_fetches_all_pages_and_builds_envelopes() -> None:
    requested_cursors: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        requested_cursors.append(cursor)
        item = {
            "case_id": 1 if cursor is None else 2,
            "case_number": "B-1" if cursor is None else "B-2",
            "identification_number": "S1234567A",
            "person_name": "Ada Lovelace",
            "latest_document_type": "bankruptcy_order",
            "latest_document_date": "2026-07-01",
            "event_id": 11,
            "event_type": "bankruptcy_order",
            "event_date": "2026-07-01",
            "trustee_name": "Jane Trustee",
            "trustee_firm": "Trustee LLP",
            "source_document_id": 21,
            "source_url": "https://example.test/order.pdf",
            "document_type": "bankruptcy_order",
            "document_date": "2026-07-01",
            "first_seen_at": "2026-07-01T12:00:00Z",
            "last_seen_at": "2026-07-02T12:00:00Z",
        }
        return httpx.Response(
            200,
            json={"items": [item], "next_cursor": "next" if cursor is None else None},
        )

    connector = SGGovernmentBankruptcyApiConnector(
        base_url="https://bankruptcy.test",
        api_key="secret",
        page_size=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    records = list(connector.fetch_records())

    assert requested_cursors == [None, "next"]
    assert [record["source_record_id"] for record in records] == [
        "bankruptcy_case:1",
        "bankruptcy_case:2",
    ]
    assert records[0]["observed_at"] == "2026-07-02T12:00:00+00:00"
    assert records[0]["identifiers"] == [
        {"type": "nric", "value": "S1234567A", "is_verified": True}
    ]
    assert records[0]["attributes"]["bankruptcy_trustee_firm"] == "Trustee LLP"
    assert records[0]["raw_payload"]["case"]["case_number"] == "B-1"


def test_api_connector_sends_bearer_key() -> None:
    authorization: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorization.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    connector = SGGovernmentBankruptcyApiConnector(
        base_url="https://bankruptcy.test",
        api_key="secret",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert list(connector.fetch_records()) == []
    assert authorization == ["Bearer secret"]


def test_api_connector_rejects_repeated_cursor() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [], "next_cursor": "stuck"})

    connector = SGGovernmentBankruptcyApiConnector(
        base_url="https://bankruptcy.test",
        api_key="secret",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RuntimeError, match="did not advance"):
        list(connector.fetch_records())


def test_api_connector_propagates_http_failure() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, json={"error": "unauthorized"})

    connector = SGGovernmentBankruptcyApiConnector(
        base_url="https://bankruptcy.test",
        api_key="secret",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(httpx.HTTPStatusError):
        list(connector.fetch_records())
    assert attempts == 1


def test_api_connector_retries_retryable_statuses() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    connector = SGGovernmentBankruptcyApiConnector(
        base_url="https://bankruptcy.test",
        api_key="secret",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=3,
        sleeper=delays.append,
    )

    assert list(connector.fetch_records()) == []
    assert attempts == 3
    assert delays == [1.0, 2.0]


def test_api_connector_retries_transport_errors() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    connector = SGGovernmentBankruptcyApiConnector(
        base_url="https://bankruptcy.test",
        api_key="secret",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=2,
        sleeper=lambda _seconds: None,
    )

    assert list(connector.fetch_records()) == []
    assert attempts == 2


def test_api_connector_rejects_malformed_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"case_id": 1}]})

    connector = SGGovernmentBankruptcyApiConnector(
        base_url="https://bankruptcy.test",
        api_key="secret",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ValidationError):
        list(connector.fetch_records())


@pytest.mark.parametrize("base_url,api_key", [("", "secret"), ("https://x", "")])
def test_api_connector_requires_configuration(base_url: str, api_key: str) -> None:
    with pytest.raises(ValueError, match="URL and key are required"):
        SGGovernmentBankruptcyApiConnector(base_url=base_url, api_key=api_key)


@pytest.mark.parametrize(
    "field,value",
    [
        ("sgbankruptcy_api_page_size", 0),
        ("sgbankruptcy_api_page_size", 1001),
        ("sgbankruptcy_api_timeout_seconds", 0),
        ("sgbankruptcy_api_max_attempts", 0),
    ],
)
def test_api_settings_reject_invalid_bounds(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(neo4j_password="test", _env_file=None, **{field: value})


def test_api_connector_maps_eventless_case_like_dump_mode() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "case_id": 1,
                        "case_number": "B-1",
                        "identification_number": "S1234567A",
                        "person_name": "Ada Lovelace",
                        "latest_document_type": None,
                        "latest_document_date": None,
                        "event_id": None,
                        "event_type": None,
                        "event_date": None,
                        "trustee_name": None,
                        "trustee_firm": None,
                        "source_document_id": None,
                        "source_url": None,
                        "document_type": None,
                        "document_date": None,
                        "first_seen_at": "2026-07-01T12:00:00Z",
                        "last_seen_at": "2026-07-02T12:00:00Z",
                    }
                ],
                "next_cursor": None,
            },
        )

    connector = SGGovernmentBankruptcyApiConnector(
        base_url="https://bankruptcy.test",
        api_key="secret",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    record = next(connector.fetch_records())

    assert record["source_record_id"] == "bankruptcy_case:1"
    assert "bankruptcy_event_type" not in record["attributes"]
    assert record["raw_payload"]["event"] == {}
    assert record["raw_payload"]["source_document"] == {}
