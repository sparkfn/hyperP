from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError
from src.connectors.fundbox_api.client import FundboxApiClient, FundboxApiCredentials
from src.connectors.fundbox_api.models import SalesOrderItem, validate_source_records


def test_iter_source_uses_basic_auth_watermark_and_cursor() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "effective_updated_at": "2026-07-17T00:00:00Z",
                            "contact": {"id": 1, "user_id": 2},
                        }
                    ],
                    "meta": {"next_cursor": "next", "has_more": True},
                },
            )
        return httpx.Response(
            200,
            json={"data": [], "meta": {"next_cursor": None, "has_more": False}},
        )

    client = FundboxApiClient(
        FundboxApiCredentials(
            base_url="https://fundbox.test/api/v1",
            username="hyperp",
            password="secret",
            page_size=100,
        ),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    records = list(client.iter_source("contacts", updated_since="2026-07-01T00:00:00Z"))

    assert records[0]["contact"]["id"] == 1
    assert requests[0].url.params["updated_since"] == "2026-07-01T00:00:00Z"
    assert requests[1].url.params["cursor"] == "next"
    assert requests[0].headers["Authorization"].startswith("Basic ")


def test_client_retries_transient_server_failure() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            json={"data": [], "meta": {"next_cursor": None, "has_more": False}},
        )

    client = FundboxApiClient(
        FundboxApiCredentials("https://fundbox.test/api/v1", "u", "p", 50),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=2,
        sleeper=lambda _seconds: None,
    )

    assert list(client.iter_source("users")) == []
    assert attempts == 2


def test_client_retries_rate_limit_using_retry_after() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, request=request, headers={"Retry-After": "3"})
        return httpx.Response(
            200,
            request=request,
            json={"data": [], "meta": {"next_cursor": None, "has_more": False}},
        )

    client = FundboxApiClient(
        FundboxApiCredentials("https://fundbox.test/api/v1", "u", "p", 50),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=2,
        sleeper=sleeps.append,
    )

    assert list(client.iter_source("sales")) == []
    assert attempts == 2
    assert sleeps == [3.0]


@pytest.mark.parametrize(
    ("retry_after", "expected_delay"),
    [("999999", 60.0), ("inf", 1.0), ("invalid", 1.0)],
)
def test_client_bounds_invalid_or_oversized_retry_after(
    retry_after: str,
    expected_delay: float,
) -> None:
    response = httpx.Response(429, headers={"Retry-After": retry_after})

    assert FundboxApiClient._retry_delay(response, attempt=1) == expected_delay


def test_client_caps_exponential_retry_delay() -> None:
    assert FundboxApiClient._exponential_delay(attempt=10) == 60.0


def test_client_credentials_reject_plaintext_http() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        FundboxApiCredentials("http://fundbox.test/api/v1", "u", "p", 50)


def test_client_rejects_inconsistent_pagination_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={"data": [], "meta": {"next_cursor": None, "has_more": True}},
        )

    client = FundboxApiClient(
        FundboxApiCredentials("https://fundbox.test/api/v1", "u", "p", 50),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ValidationError, match="next_cursor"):
        list(client.iter_source("sales"))


def test_client_rejects_repeated_pagination_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={"data": [], "meta": {"next_cursor": "same", "has_more": True}},
        )

    client = FundboxApiClient(
        FundboxApiCredentials("https://fundbox.test/api/v1", "u", "p", 50),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ValueError, match="repeated cursor"):
        list(client.iter_source("users"))


def test_client_validates_every_record_before_yielding_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "data": [
                    {
                        "effective_updated_at": "2026-07-17T00:00:00Z",
                        "contact": {"id": 1, "user_id": 2},
                    },
                    {
                        "effective_updated_at": "2026-07-17T00:00:01Z",
                        "contact": {"id": "invalid", "user_id": 2},
                    },
                ],
                "meta": {"next_cursor": None, "has_more": False},
            },
        )

    client = FundboxApiClient(
        FundboxApiCredentials("https://fundbox.test/api/v1", "u", "p", 50),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    records = client.iter_source("contacts")
    with pytest.raises(ValidationError, match="contact.id"):
        next(records)


def test_sales_validation_requires_every_mapper_field() -> None:
    with pytest.raises(ValidationError, match="merchant_id"):
        validate_source_records(
            "sales",
            [
                {
                    "effective_updated_at": "2026-07-17T00:00:00Z",
                    "order": {"id": 1, "user_id": 2},
                    "merchant": None,
                    "items": [],
                    "customer": None,
                }
            ],
        )


def test_sales_order_item_contract_includes_parent_order_id() -> None:
    item = SalesOrderItem.model_validate(
        {
            "id": 1,
            "order_id": 10,
            "merchant_product_id": None,
            "quantity": 1,
            "price": "12.00",
            "lta_tag": None,
            "serial_no": None,
            "created_at": None,
            "updated_at": None,
        }
    )

    assert item.order_id == 10


def test_source_validation_rejects_timezone_naive_effective_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        validate_source_records(
            "contacts",
            [
                {
                    "effective_updated_at": "2026-07-17 00:00:00",
                    "contact": {"id": 1, "user_id": 2},
                }
            ],
        )


def test_source_validation_rejects_object_valued_identity_field() -> None:
    with pytest.raises(ValidationError, match="basic_profile.nric"):
        validate_source_records(
            "users",
            [
                {
                    "effective_updated_at": "2026-07-17T00:00:00Z",
                    "user": {"id": 1},
                    "basic_profile": {"nric": {"unexpected": "object"}},
                    "basic_plus_profile": None,
                    "addresses": [],
                    "social_accounts": [],
                    "device_ids": [],
                    "last_login": None,
                }
            ],
        )


@pytest.mark.parametrize("invalid_id", ["1", True])
def test_source_validation_rejects_coercive_ids(invalid_id: object) -> None:
    with pytest.raises(ValidationError, match="contact.id"):
        validate_source_records(
            "contacts",
            [
                {
                    "effective_updated_at": "2026-07-17T00:00:00Z",
                    "contact": {"id": invalid_id, "user_id": 2},
                }
            ],
        )


def test_source_validation_rejects_uncontracted_nested_fields() -> None:
    with pytest.raises(ValidationError, match="contact.secret_note"):
        validate_source_records(
            "contacts",
            [
                {
                    "effective_updated_at": "2026-07-17T00:00:00Z",
                    "contact": {"id": 1, "user_id": 2, "secret_note": "do not persist"},
                }
            ],
        )


def test_source_validation_rejects_malformed_source_timestamp() -> None:
    with pytest.raises(ValidationError, match="contact.updated_at"):
        validate_source_records(
            "contacts",
            [
                {
                    "effective_updated_at": "2026-07-17T00:00:00Z",
                    "contact": {"id": 1, "user_id": 2, "updated_at": "not-a-timestamp"},
                }
            ],
        )
