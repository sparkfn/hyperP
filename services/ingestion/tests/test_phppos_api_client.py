from __future__ import annotations

import time
from collections.abc import Callable
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import ValidationError
from src.connectors.phppos_api.client import ApiCredentials, PhpposApiClient
from src.connectors.phppos_api.models import CustomerPage, SalesPage


def _credentials() -> ApiCredentials:
    return ApiCredentials(
        base_url="https://pos.example",
        client_id="client",
        client_secret="secret",
        tenant_id="tenant",
        page_size=500,
        scopes=("customers:read", "sales:read"),
    )


def _empty_page() -> dict[str, object]:
    return {
        "data": [],
        "pagination": {"next_cursor": None, "has_more": False},
    }


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    clock: Callable[[], float] | None = None,
    sleeps: list[float] | None = None,
    max_attempts: int = 3,
) -> PhpposApiClient:
    sleeper: Callable[[float], None]
    sleeper = (lambda seconds: sleeps.append(seconds)) if sleeps is not None else lambda _: None
    return PhpposApiClient(
        _credentials(),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=sleeper,
        clock=clock or time.time,
        max_attempts=max_attempts,
    )


def test_customer_page_requires_consistent_pagination() -> None:
    page = CustomerPage.model_validate(
        {
            "data": [{"person_id": 7, "full_name": "Ada"}],
            "pagination": {"next_cursor": "opaque", "has_more": True},
        }
    )
    assert page.data[0].person_id == 7
    with pytest.raises(ValidationError):
        CustomerPage.model_validate(
            {"data": [], "pagination": {"next_cursor": None, "has_more": True}}
        )


def test_sales_page_requires_nested_lines() -> None:
    page = SalesPage.model_validate(
        {
            "data": [{"sale_id": 4, "sale_time": "2026-01-01 00:00:00", "lines": []}],
            "pagination": {"next_cursor": None, "has_more": False},
        }
    )
    assert page.data[0].sale_id == 4
    with pytest.raises(ValidationError):
        SalesPage.model_validate(
            {
                "data": [{"sale_id": 4, "sale_time": "2026-01-01", "lines": "bad"}],
                "pagination": {"next_cursor": None, "has_more": False},
            }
        )


def test_token_request_uses_client_credentials_basic_auth_and_explicit_scope() -> None:
    token_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_request
        if request.url.path == "/oauth/token":
            token_request = request
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        return httpx.Response(200, json=_empty_page())

    assert list(_client(handler).iter_customers()) == []
    assert token_request is not None
    form = parse_qs(token_request.content.decode())
    assert form == {
        "grant_type": ["client_credentials"],
        "scope": ["customers:read sales:read"],
    }
    assert "refresh_token" not in form
    assert "idempotency_key" not in form
    assert token_request.headers["authorization"] == "Basic Y2xpZW50OnNlY3JldA=="


def test_customer_pages_reuse_process_local_token() -> None:
    token_calls = 0
    api_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if request.url.path == "/oauth/token":
            token_calls += 1
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        api_requests.append(request)
        cursor = request.url.params.get("cursor")
        return httpx.Response(
            200,
            json={
                "data": [{"person_id": 1 if cursor is None else 2}],
                "pagination": {
                    "next_cursor": "next" if cursor is None else None,
                    "has_more": cursor is None,
                },
            },
        )

    client = _client(handler)
    assert [row.person_id for row in client.iter_customers()] == [1, 2]
    assert list(client.iter_customers())[0].person_id == 1
    assert token_calls == 1
    assert len(api_requests) == 4
    assert api_requests[0].url.params["limit"] == "500"
    assert api_requests[1].url.params["cursor"] == "next"
    assert api_requests[0].headers["x-pos-tenant-id"] == "tenant"
    assert api_requests[0].headers["authorization"] == "Bearer access"


def test_token_is_renewed_at_expiry_skew_boundary() -> None:
    now = 100.0
    token_calls = 0

    def clock() -> float:
        return now

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if request.url.path == "/oauth/token":
            token_calls += 1
            return httpx.Response(
                200,
                json={"access_token": f"access-{token_calls}", "expires_in": 60},
            )
        return httpx.Response(200, json=_empty_page())

    client = _client(handler, clock=clock)
    assert list(client.iter_customers()) == []
    now = 129.0
    assert list(client.iter_customers()) == []
    now = 130.0
    assert list(client.iter_customers()) == []
    assert token_calls == 2


def test_client_renews_token_once_after_unauthorized() -> None:
    token_calls = 0
    api_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, api_calls
        if request.url.path == "/oauth/token":
            token_calls += 1
            return httpx.Response(
                200,
                json={"access_token": f"access-{token_calls}", "expires_in": 3600},
            )
        api_calls += 1
        if api_calls == 1:
            return httpx.Response(401, json={"error": "expired"})
        if api_calls == 2:
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(200, json=_empty_page())

    assert list(_client(handler).iter_customers()) == []
    assert token_calls == 2
    assert api_calls == 3


def test_authentication_retry_is_independent_of_transient_attempt_budget() -> None:
    token_calls = 0
    api_calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, api_calls
        if request.url.path == "/oauth/token":
            token_calls += 1
            return httpx.Response(
                200,
                json={"access_token": f"access-{token_calls}", "expires_in": 3600},
            )
        api_calls += 1
        if api_calls <= 2:
            return httpx.Response(503, json={"error": "temporary"})
        if api_calls == 3:
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json=_empty_page())

    assert list(_client(handler, sleeps=sleeps, max_attempts=3).iter_customers()) == []
    assert token_calls == 2
    assert api_calls == 4
    assert sleeps == [1.0, 2.0]


def test_client_renews_after_unauthorized_with_one_transient_attempt() -> None:
    token_calls = 0
    api_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, api_calls
        if request.url.path == "/oauth/token":
            token_calls += 1
            return httpx.Response(
                200,
                json={"access_token": f"access-{token_calls}", "expires_in": 3600},
            )
        api_calls += 1
        if api_calls == 1:
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json=_empty_page())

    assert list(_client(handler, max_attempts=1).iter_customers()) == []
    assert token_calls == 2
    assert api_calls == 2


def test_second_unauthorized_response_fails_and_invalidates_rejected_token() -> None:
    token_calls = 0
    api_calls = 0
    api_authorizations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, api_calls
        if request.url.path == "/oauth/token":
            token_calls += 1
            return httpx.Response(
                200,
                json={"access_token": f"access-{token_calls}", "expires_in": 3600},
            )
        api_calls += 1
        api_authorizations.append(request.headers["authorization"])
        if api_calls <= 2:
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(200, json=_empty_page())

    client = _client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        list(client.iter_customers())
    assert token_calls == 2
    assert api_calls == 2
    assert list(client.iter_customers()) == []
    assert token_calls == 3
    assert api_authorizations == [
        "Bearer access-1",
        "Bearer access-2",
        "Bearer access-3",
    ]


def test_client_retries_transient_token_endpoint_failures() -> None:
    token_attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_attempts
        if request.url.path == "/oauth/token":
            token_attempts += 1
            if token_attempts == 1:
                raise httpx.ConnectError("temporary", request=request)
            if token_attempts == 2:
                return httpx.Response(429, json={"error": "rate_limited"})
            if token_attempts == 3:
                return httpx.Response(503, json={"error": "temporary"})
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        return httpx.Response(200, json=_empty_page())

    assert list(_client(handler, sleeps=sleeps, max_attempts=4).iter_customers()) == []
    assert token_attempts == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_token_endpoint_bad_request_fails_without_retry() -> None:
    token_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_attempts
        token_attempts += 1
        return httpx.Response(400, json={"error": "invalid_scope"})

    with pytest.raises(httpx.HTTPStatusError):
        list(_client(handler).iter_customers())
    assert token_attempts == 1


def test_client_retries_api_transport_rate_limit_and_server_errors() -> None:
    api_attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal api_attempts
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        api_attempts += 1
        if api_attempts == 1:
            raise httpx.ConnectError("temporary", request=request)
        if api_attempts == 2:
            return httpx.Response(429, json={"error": "rate_limited"})
        if api_attempts == 3:
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(200, json=_empty_page())

    assert list(_client(handler, sleeps=sleeps, max_attempts=4).iter_customers()) == []
    assert api_attempts == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_client_does_not_retry_api_bad_request() -> None:
    api_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal api_attempts
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        api_attempts += 1
        return httpx.Response(400, json={"error": "failure"})

    with pytest.raises(httpx.HTTPStatusError):
        list(_client(handler).iter_customers())
    assert api_attempts == 1


def test_incremental_watermark_is_forwarded_on_every_customer_page() -> None:
    api_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        api_requests.append(request)
        cursor = request.url.params.get("cursor")
        return httpx.Response(
            200,
            json={
                "data": [],
                "pagination": {
                    "next_cursor": "next" if cursor is None else None,
                    "has_more": cursor is None,
                },
            },
        )

    list(_client(handler).iter_customers(updated_since="2026-08-03T01:00:00+00:00"))

    assert len(api_requests) == 2
    assert all(
        request.url.params["updated_since"] == "2026-08-03T01:00:00+00:00"
        for request in api_requests
    )
