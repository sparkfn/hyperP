"""Bounded PHPPOS transport: frozen pages, bounded budgets, sanitised failures."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from _phppos_bounded_fixture import (
    OBSERVED_AT,
    attempt_context,
    changes,
    client,
    page_payload,
    scripted_handler,
    tenant_for,
    tombstone,
    window,
    window_payload,
)
from src.bounded_ingestion_models import AttemptContext, SourceBackoffError
from src.connectors.phppos_api.client import (
    BoundedPageResult,
    BoundedRequestBudget,
    PhpposApiClient,
    PhpposBoundedTransportError,
)

OPAQUE_CURSOR = "opaque-cursor-that-must-never-be-echoed"
SALES_SOURCE_KEY = "speedzone_phppos:sales"


def _budget(
    *,
    requests: int = 6,
    rows: int = 500,
    bytes_limit: int = 2_000_000,
) -> BoundedRequestBudget:
    return BoundedRequestBudget(max_requests=requests, max_rows=rows, max_bytes=bytes_limit)


def _fetch(
    api: PhpposApiClient,
    *,
    resource: str = "customers",
    cursor: str | None = None,
    budget: BoundedRequestBudget | None = None,
    context: AttemptContext | None = None,
) -> BoundedPageResult:
    return api.fetch_bounded_page(
        resource,
        cursor=cursor,
        window=window("eko_phppos").window,
        context=context or attempt_context(),
        budget=budget or _budget(),
    )


def test_page_fetch_sends_the_frozen_window_and_no_legacy_parameters() -> None:
    requests: list[httpx.Request] = []
    api = client(scripted_handler([page_payload("customers")], requests))

    result = _fetch(api, cursor=OPAQUE_CURSOR)

    assert result.page.tenant_id == "eko-tenant"
    assert result.page.resource == "customers"
    assert len(result.page.data) == len(changes("customers"))
    assert result.usage.pages == 1
    assert result.usage.records == len(changes("customers"))

    data_requests = _data_requests(requests)
    assert len(data_requests) == 1
    query = data_requests[0].url.params
    assert query["snapshot_id"] == "snap-2026-09-17"
    assert query["upper_change_version"] == "4127"
    assert query["cursor"] == OPAQUE_CURSOR
    assert "updated_since" not in query
    assert data_requests[0].headers["x-pos-tenant-id"] == "eko-tenant"


def test_tombstones_and_upserts_share_one_validated_page() -> None:
    payload = page_payload(
        "customers",
        data=[*changes("customers", limit=1), tombstone("4099")],
    )

    result = _fetch(client(scripted_handler([payload], [])))

    assert [change.kind for change in result.page.data] == ["upsert", "tombstone"]


def test_response_that_crosses_tenant_resource_or_window_fails_closed() -> None:
    crossed_tenant = page_payload("customers", tenant_id="speedzone-tenant")
    crossed_resource = page_payload("customers")
    crossed_resource["resource"] = "sales"
    moved_window = page_payload(
        "customers",
        window_body=window_payload(snapshot_id="snap-has-moved"),
    )

    expectations = (
        (crossed_tenant, "crossed tenant or resource"),
        (crossed_resource, "crossed tenant or resource"),
        (moved_window, "changed its frozen window"),
    )
    for payload, message in expectations:
        api = client(scripted_handler([payload], []))
        with pytest.raises(PhpposBoundedTransportError, match=message):
            _fetch(api)


def test_malformed_and_contract_violating_responses_fail_closed() -> None:
    def respond_with(body: bytes):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth/token"):
                return httpx.Response(
                    200,
                    json={"access_token": "token", "expires_in": 3600},
                    request=request,
                )
            return httpx.Response(200, content=body, request=request)

        return handler

    bodies = (b"not-json", b"[]", b'{"contract_version": "phppos-bounded-v1"}')
    for body in bodies:
        with pytest.raises(PhpposBoundedTransportError) as error:
            _fetch(client(respond_with(body)))
        assert "malformed" in error.value.args[0] or "contract" in error.value.args[0]


def test_missing_independent_tenant_principal_is_refused_before_any_request() -> None:
    requests: list[httpx.Request] = []
    api = client(
        scripted_handler([page_payload("customers")], requests),
        principal_tenant_id="shared-principal",
    )

    with pytest.raises(PhpposBoundedTransportError, match="independent PHPPOS tenant principal"):
        _fetch(api)

    assert requests == []


def test_request_allowance_bounds_oauth_and_page_requests() -> None:
    requests: list[httpx.Request] = []
    api = client(scripted_handler([page_payload("customers")], requests))

    with pytest.raises(PhpposBoundedTransportError, match="request allowance exhausted"):
        _fetch(api, budget=_budget(requests=1))

    assert len(requests) == 1


def test_row_and_byte_allowances_bound_the_page_read() -> None:
    with pytest.raises(PhpposBoundedTransportError, match="page exceeds adapter limits"):
        _fetch(
            client(scripted_handler([page_payload("customers")], [])),
            budget=_budget(rows=1),
        )

    with pytest.raises(PhpposBoundedTransportError, match="byte allowance"):
        _fetch(
            client(scripted_handler([page_payload("customers")], [])),
            budget=_budget(bytes_limit=64),
        )


def test_oversized_streaming_body_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(
                200,
                json={"access_token": "token", "expires_in": 3600},
                request=request,
            )
        return httpx.Response(200, json={"padding": "x" * 5000}, request=request)

    with pytest.raises(PhpposBoundedTransportError, match="byte allowance"):
        _fetch(client(handler), budget=_budget(bytes_limit=512))


def test_rate_limit_becomes_a_sanitised_source_backoff() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(
                200,
                json={"access_token": "token", "expires_in": 3600},
                request=request,
            )
        return httpx.Response(
            429,
            headers={"retry-after": "120"},
            json={"detail": OPAQUE_CURSOR},
            request=request,
        )

    with pytest.raises(SourceBackoffError) as error:
        _fetch(client(handler))

    assert error.value.retry_at == OBSERVED_AT + timedelta(seconds=120)
    assert error.value.safe_message == "bounded PHPPOS source requested backoff"
    assert OPAQUE_CURSOR not in str(error.value)


def test_server_errors_retry_then_fail_closed_without_echoing_the_cursor() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(
                200,
                json={"access_token": "token", "expires_in": 3600},
                request=request,
            )
        return httpx.Response(503, json={"detail": OPAQUE_CURSOR}, request=request)

    with pytest.raises(PhpposBoundedTransportError) as error:
        _fetch(client(handler), cursor=OPAQUE_CURSOR)

    assert error.value.args[0] == "bounded PHPPOS source is unavailable"
    assert OPAQUE_CURSOR not in str(error.value)
    assert len(_data_requests(requests)) == 3


def test_one_unauthorized_retry_reauthorsises_then_succeeds() -> None:
    requests: list[httpx.Request] = []
    rejected = {"once": False}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(
                200,
                json={"access_token": "token", "expires_in": 3600},
                request=request,
            )
        if not rejected["once"]:
            rejected["once"] = True
            return httpx.Response(401, json={"detail": "stale token"}, request=request)
        return httpx.Response(200, json=page_payload("customers"), request=request)

    result = _fetch(client(handler))

    assert result.page.resource == "customers"
    assert len(requests) - len(_data_requests(requests)) == 2
    assert len(_data_requests(requests)) == 2


def test_repeated_unauthorized_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(
                200,
                json={"access_token": "token", "expires_in": 3600},
                request=request,
            )
        return httpx.Response(401, json={"detail": OPAQUE_CURSOR}, request=request)

    with pytest.raises(PhpposBoundedTransportError, match="authorization was rejected"):
        _fetch(client(handler), cursor=OPAQUE_CURSOR)


def test_transient_transport_failure_retries_within_the_request_allowance() -> None:
    requests: list[httpx.Request] = []
    dropped = {"once": False}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(
                200,
                json={"access_token": "token", "expires_in": 3600},
                request=request,
            )
        if not dropped["once"]:
            dropped["once"] = True
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json=page_payload("customers"), request=request)

    assert _fetch(client(handler)).page.resource == "customers"
    assert len(_data_requests(requests)) == 2


def test_cancelled_attempt_stops_before_reading() -> None:
    requests: list[httpx.Request] = []
    api = client(scripted_handler([page_payload("customers")], requests))

    with pytest.raises(PhpposBoundedTransportError, match="cancelled"):
        _fetch(api, context=attempt_context(cancelled=True))

    assert requests == []


def test_attempt_past_its_deadline_stops_before_reading() -> None:
    requests: list[httpx.Request] = []
    api = client(scripted_handler([page_payload("customers")], requests))
    expired = replace(
        attempt_context(),
        operation_deadline_at=OBSERVED_AT - timedelta(seconds=1),
    )

    with pytest.raises(PhpposBoundedTransportError, match="deadline"):
        _fetch(api, context=expired)

    assert _data_requests(requests) == []


def test_budget_accounting_is_monotonic_across_pages() -> None:
    payloads = [
        page_payload("customers", data=changes("customers", limit=1), next_cursor="next"),
        page_payload("customers", data=changes("customers", limit=1)),
    ]
    budget = _budget()
    api = client(scripted_handler(payloads, []))

    first = _fetch(api, budget=budget)
    second = _fetch(api, cursor="next", budget=budget)

    assert budget.usage.pages == 2
    assert budget.usage.records == 2
    # One OAuth request plus one request per page: OAuth requests are counted in
    # the same allowance as data requests.
    assert budget.usage.source_requests == 3
    assert second.usage.bytes_read >= first.usage.bytes_read
    assert budget.remaining_requests() == 3


def test_sales_resource_is_fetched_from_the_sales_endpoint() -> None:
    requests: list[httpx.Request] = []
    api = client(
        scripted_handler([page_payload("sales", source_key=SALES_SOURCE_KEY)], requests),
        tenant_id=tenant_for(SALES_SOURCE_KEY),
    )

    result = api.fetch_bounded_page(
        "sales",
        cursor=None,
        window=window(SALES_SOURCE_KEY).window,
        context=attempt_context(SALES_SOURCE_KEY),
        budget=_budget(),
    )

    assert result.page.resource == "sales"
    assert [request.url.path for request in _data_requests(requests)] == [
        "/api/v1/custom/hyperp/sales"
    ]


def test_unsupported_resource_is_rejected_without_a_request() -> None:
    requests: list[httpx.Request] = []
    api = client(scripted_handler([page_payload("customers")], requests))

    with pytest.raises(PhpposBoundedTransportError, match="resource is unsupported"):
        _fetch(api, resource="invoices")

    assert requests == []


def _data_requests(requests: list[httpx.Request]) -> list[httpx.Request]:
    return [request for request in requests if not request.url.path.endswith("/oauth/token")]
