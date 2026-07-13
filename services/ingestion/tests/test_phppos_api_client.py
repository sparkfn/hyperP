from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import ValidationError
from src.connectors.phppos_api.client import (
    ApiCredentials,
    CredentialBundle,
    PhpposApiClient,
    token_rotation_lock_seconds,
)
from src.connectors.phppos_api.models import CustomerPage, SalesPage


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


class MemoryTokens:
    def __init__(self) -> None:
        self.refresh: str | None = None
        self.bundle: CredentialBundle | None = None

    def get_refresh_token(self) -> str | None:
        return self.refresh

    def set_refresh_token(self, value: str) -> None:
        self.refresh = value

    def get_credential_bundle(self) -> CredentialBundle | None:
        return self.bundle

    def set_credential_bundle(self, value: CredentialBundle) -> None:
        self.bundle = value
        self.refresh = value.refresh_token

    def rotation_lock(self) -> AbstractContextManager[None]:
        return nullcontext()

    def close(self) -> None:
        return None


def test_client_rotates_token_and_traverses_customer_pages() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={"access_token": "access", "refresh_token": "rotated", "expires_in": 3600},
            )
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

    tokens = MemoryTokens()
    client = PhpposApiClient(
        ApiCredentials("https://pos.example", "client", "secret", "bootstrap", "tenant", 500),
        token_store=tokens,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _seconds: None,
    )
    assert [row.person_id for row in client.iter_customers()] == [1, 2]
    assert tokens.refresh == "rotated"
    assert calls[1].headers["x-pos-tenant-id"] == "tenant"
    assert calls[1].headers["authorization"] == "Bearer access"


def test_second_worker_reuses_shared_access_token_without_rotating() -> None:
    token_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if request.url.path == "/oauth/token":
            token_calls += 1
            return httpx.Response(
                200,
                json={"access_token": "shared", "refresh_token": "rotated", "expires_in": 3600},
            )
        return httpx.Response(
            200,
            json={"data": [], "pagination": {"next_cursor": None, "has_more": False}},
        )

    tokens = MemoryTokens()
    credentials = ApiCredentials("https://pos.example", "c", "s", "r0", "tenant", 500)
    first = PhpposApiClient(
        credentials,
        token_store=tokens,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    second = PhpposApiClient(
        credentials,
        token_store=tokens,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert list(first.iter_customers()) == []
    assert list(second.iter_customers()) == []
    assert token_calls == 1


def test_client_retries_server_errors_but_not_bad_requests() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200, json={"access_token": "a", "refresh_token": "r", "expires_in": 3600}
            )
        attempts += 1
        return httpx.Response(503 if attempts < 3 else 400, json={"error": "failure"})

    client = PhpposApiClient(
        ApiCredentials("https://pos.example", "c", "s", "r0", "tenant", 500),
        token_store=MemoryTokens(),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _seconds: None,
        max_attempts=3,
    )
    with pytest.raises(httpx.HTTPStatusError):
        list(client.iter_customers())
    assert attempts == 3


def test_client_refreshes_only_once_after_unauthorized() -> None:
    token_calls = 0
    api_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, api_calls
        if request.url.path == "/oauth/token":
            token_calls += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"a{token_calls}",
                    "refresh_token": f"r{token_calls}",
                    "expires_in": 3600,
                },
            )
        api_calls += 1
        if api_calls == 1:
            return httpx.Response(401, json={"error": "expired"})
        if api_calls == 2:
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(
            200,
            json={"data": [], "pagination": {"next_cursor": None, "has_more": False}},
        )

    client = PhpposApiClient(
        ApiCredentials("https://pos.example", "c", "s", "r0", "tenant", 500),
        token_store=MemoryTokens(),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _seconds: None,
    )
    assert list(client.iter_customers()) == []
    assert token_calls == 2


def test_client_retries_transient_token_endpoint_failures() -> None:
    token_attempts = 0
    idempotency_keys: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_attempts
        if request.url.path == "/oauth/token":
            token_attempts += 1
            form = parse_qs(request.content.decode())
            idempotency_keys.append(form.get("idempotency_key", [None])[0])
            if token_attempts == 1:
                raise httpx.ConnectError("temporary", request=request)
            if token_attempts == 2:
                return httpx.Response(503, json={"error": "temporary"})
            return httpx.Response(
                200,
                json={"access_token": "a", "refresh_token": "r", "expires_in": 3600},
            )
        return httpx.Response(
            200,
            json={"data": [], "pagination": {"next_cursor": None, "has_more": False}},
        )

    client = PhpposApiClient(
        ApiCredentials("https://pos.example", "c", "s", "r0", "tenant", 500),
        token_store=MemoryTokens(),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _seconds: None,
        max_attempts=3,
    )
    assert list(client.iter_customers()) == []
    assert token_attempts == 3
    assert len(set(idempotency_keys)) == 1
    assert idempotency_keys[0]


def test_refresh_lock_lease_covers_every_attempt_and_backoff() -> None:
    lease = token_rotation_lock_seconds(timeout_seconds=30.0, max_attempts=3)
    assert lease >= (30 * 4 * 3) + 1 + 2 + 30


def test_pending_rotation_reuses_durable_idempotency_key() -> None:
    seen_key: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_key
        if request.url.path == "/oauth/token":
            seen_key = parse_qs(request.content.decode())["idempotency_key"][0]
            return httpx.Response(
                200,
                json={"access_token": "a", "refresh_token": "r1", "expires_in": 3600},
            )
        return httpx.Response(
            200,
            json={"data": [], "pagination": {"next_cursor": None, "has_more": False}},
        )

    tokens = MemoryTokens()
    tokens.bundle = CredentialBundle(None, 0, "r0", "durable-request-id")
    client = PhpposApiClient(
        ApiCredentials("https://pos.example", "c", "s", "bootstrap", "tenant", 500),
        token_store=tokens,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert list(client.iter_customers()) == []
    assert seen_key == "durable-request-id"
