"""Behavioral coverage for bounded request-time Bitrix activity reads."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

import httpx
import pytest
from pydantic import ValidationError
from src.config import AppConfig
from src.repositories.bitrix.activity import BitrixCrmActivityRepository
from src.types_crm import BitrixDealScope

ResponseHandler = Callable[[dict[str, object]], Awaitable[httpx.Response]]


def _config(**updates: object) -> AppConfig:
    values: dict[str, object] = {
        "NEO4J_PASSWORD": "test-password",
        "BITRIX_ACTIVITY_API_URL": "https://bitrix.test/rest/activity.list",
        "BITRIX_ACTIVITY_SOURCE_INSTANCE": "bitrix-primary",
        "BITRIX_ACTIVITY_TIMEOUT_SECONDS": 1.0,
        "BITRIX_ACTIVITY_ELAPSED_SECONDS": 5.0,
        "BITRIX_ACTIVITY_DEAL_LIMIT": 10,
        "BITRIX_ACTIVITY_OWNER_BATCH_SIZE": 2,
        "BITRIX_ACTIVITY_MAX_ATTEMPTS": 2,
        "BITRIX_ACTIVITY_MAX_REQUESTS": 12,
        "BITRIX_ACTIVITY_MAX_PAGES": 10,
        "BITRIX_ACTIVITY_MAX_ROWS": 20,
        "BITRIX_ACTIVITY_MAX_CONCURRENCY": 2,
        "BITRIX_ACTIVITY_CACHE_TTL_SECONDS": 30,
        "BITRIX_ACTIVITY_CACHE_MAX_ENTRIES": 2,
    }
    aliases = {
        "bitrix_activity_max_attempts": "BITRIX_ACTIVITY_MAX_ATTEMPTS",
        "bitrix_activity_max_pages": "BITRIX_ACTIVITY_MAX_PAGES",
        "bitrix_activity_max_requests": "BITRIX_ACTIVITY_MAX_REQUESTS",
        "bitrix_activity_max_rows": "BITRIX_ACTIVITY_MAX_ROWS",
    }
    values.update({aliases.get(key, key): value for key, value in updates.items()})
    return AppConfig.model_validate(values)


def _scope(
    *ids: str,
    exhausted: bool = False,
    source_authorized: bool = True,
    scope_valid: bool = True,
) -> BitrixDealScope:
    return BitrixDealScope(
        canonical_person_id="canonical-person",
        deal_ids=tuple(ids),
        resolved_deal_count=len(ids) + (1 if exhausted else 0),
        deal_limit_exhausted=exhausted,
        source_authorized=source_authorized,
        scope_valid=scope_valid,
    )


def _activity(
    identifier: str,
    owner: str = "10",
    *,
    timestamp: str = "2026-09-01T08:00:00Z",
    kind: str = "2",
    provider_id: str | None = None,
    provider_type_id: str | None = None,
) -> dict[str, object]:
    return {
        "ID": identifier,
        "OWNER_TYPE_ID": 2,
        "OWNER_ID": owner,
        "TYPE_ID": kind,
        "PROVIDER_ID": provider_id,
        "PROVIDER_TYPE_ID": provider_type_id,
        "START_TIME": timestamp,
        "CREATED": "2026-08-01T08:00:00Z",
        "LAST_UPDATED": "2026-09-02T08:00:00Z",
        "DIRECTION": "inbound",
        "COMPLETED": "Y",
    }


def _client(handler: ResponseHandler) -> httpx.AsyncClient:
    async def transport(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert isinstance(body, dict)
        return await handler(body)

    return httpx.AsyncClient(transport=httpx.MockTransport(transport))


def _is_freeze(body: dict[str, object]) -> bool:
    return body.get("select") == ["ID"] and body.get("order") == {"ID": "DESC"}


def _assert_owner_scope(body: dict[str, object], owners: list[str]) -> None:
    filters = body.get("filter")
    assert isinstance(filters, dict)
    assert filters.get("OWNER_TYPE_ID") == 2
    assert filters.get("@OWNER_ID") == owners


def test_source_instance_configuration_rejects_noncanonical_or_secret_like_values() -> None:
    for value in ("UPPER", "bitrix_primary", " bitrix-primary", "token-like-value-"):
        with pytest.raises(ValidationError):
            _config(BITRIX_ACTIVITY_SOURCE_INSTANCE=value)


@pytest.mark.anyio
async def test_empty_valid_scope_is_complete_zero_without_authority_or_network_io() -> None:
    async def no_network(_: dict[str, object]) -> httpx.Response:
        pytest.fail("network")

    repository = BitrixCrmActivityRepository(_config(), _client(no_network))
    result = await repository.get_person_crm_activity_metrics(_scope(source_authorized=False))

    assert result.status == "complete"
    assert result.activity_count == result.call_count == 0
    assert result.request_count == result.page_count == result.row_count == 0


@pytest.mark.anyio
async def test_owner_scoped_empty_portal_result_is_confirmed_complete_zero() -> None:
    calls = 0

    async def handler(body: dict[str, object]) -> httpx.Response:
        nonlocal calls
        calls += 1
        _assert_owner_scope(body, ["10"])
        assert _is_freeze(body)
        return httpx.Response(200, json={"result": []})

    async with _client(handler) as client:
        result = await BitrixCrmActivityRepository(
            _config(), client
        ).get_person_crm_activity_metrics(_scope("10"))

    assert result.status == "complete"
    assert result.activity_count == result.call_count == 0
    assert result.request_count == calls == 1
    assert result.page_count == result.row_count == 0


@pytest.mark.anyio
async def test_requests_are_metadata_only_owner_scoped_and_keyset_ordered() -> None:
    requests: list[dict[str, object]] = []

    async def handler(body: dict[str, object]) -> httpx.Response:
        requests.append(body)
        _assert_owner_scope(body, ["10", "11"])
        if _is_freeze(body):
            return httpx.Response(200, json={"result": [{"ID": "3"}]})
        return httpx.Response(200, json={"result": [_activity("1", "10")]})

    async with _client(handler) as client:
        result = await BitrixCrmActivityRepository(
            _config(), client
        ).get_person_crm_activity_metrics(_scope("10", "11"))

    assert result.status == "complete"
    assert len(requests) == 2
    page = requests[1]
    assert page["order"] == {"ID": "ASC"}
    assert "start" not in page
    assert page["filter"] == {"OWNER_TYPE_ID": 2, "@OWNER_ID": ["10", "11"], "<=ID": "3"}
    selected = page["select"]
    assert isinstance(selected, list)
    assert "DESCRIPTION" not in selected and "SUBJECT" not in selected


@pytest.mark.anyio
async def test_complete_dedupes_overlapping_keyset_rows_and_normalizes_kinds() -> None:
    pages = iter(
        (
            {"result": [_activity("1"), _activity("1"), _activity("2", kind="3")], "next": "x"},
            {
                "result": [
                    _activity("2", kind="3"),
                    _activity("3", kind="email", provider_id="voximplant_call"),
                ]
            },
        )
    )

    async def handler(body: dict[str, object]) -> httpx.Response:
        if _is_freeze(body):
            return httpx.Response(200, json={"result": [{"ID": "3"}]})
        return httpx.Response(200, json=next(pages))

    async with _client(handler) as client:
        result = await BitrixCrmActivityRepository(
            _config(), client
        ).get_person_crm_activity_metrics(_scope("10"))

    assert result.status == "complete"
    assert result.activity_count == 3
    assert result.call_count == 2
    assert result.row_count == 5
    assert [item.history_kind for item in result.activity_kind_breakdown] == [
        "activity_type_3",
        "call",
    ]


@pytest.mark.anyio
async def test_non_call_provider_normalization_precedes_type_id() -> None:
    async def handler(body: dict[str, object]) -> httpx.Response:
        if _is_freeze(body):
            return httpx.Response(200, json={"result": [{"ID": "3"}]})
        return httpx.Response(
            200,
            json={
                "result": [
                    _activity("1", kind="3", provider_type_id="TASKS"),
                    _activity("2", kind="3", provider_id="IMOPENLINES_SESSION"),
                    _activity("3", kind="3"),
                ]
            },
        )

    async with _client(handler) as client:
        result = await BitrixCrmActivityRepository(
            _config(), client
        ).get_person_crm_activity_metrics(_scope("10"))

    assert result.status == "complete"
    assert [item.history_kind for item in result.activity_kind_breakdown] == [
        "activity_type_3",
        "openlines_session",
        "tasks",
    ]


@pytest.mark.anyio
async def test_malformed_or_wrong_owner_is_unavailable_before_safe_lower_bound() -> None:
    async def handler(body: dict[str, object]) -> httpx.Response:
        if _is_freeze(body):
            return httpx.Response(200, json={"result": [{"ID": "1"}]})
        return httpx.Response(200, json={"result": [_activity("a", "other")]})

    async with _client(handler) as client:
        result = await BitrixCrmActivityRepository(
            _config(), client
        ).get_person_crm_activity_metrics(_scope("10"))

    assert result.status == "unavailable"
    assert result.failure_reason == "malformed_response"
    assert result.row_count == 1


@pytest.mark.anyio
async def test_failure_after_accepted_rows_is_partial_lower_bound() -> None:
    async def handler(body: dict[str, object]) -> httpx.Response:
        if _is_freeze(body):
            return httpx.Response(200, json={"result": [{"ID": "2"}]})
        filters = body["filter"]
        assert isinstance(filters, dict)
        if ">ID" not in filters:
            return httpx.Response(200, json={"result": [_activity("1")], "next": "ignored"})
        return httpx.Response(200, json={"result": [{"ID": "bad"}]})

    async with _client(handler) as client:
        result = await BitrixCrmActivityRepository(
            _config(), client
        ).get_person_crm_activity_metrics(_scope("10"))

    assert result.status == "partial"
    assert result.truncated is True
    assert result.activity_count == 1
    assert result.failure_reason == "malformed_response"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("config_updates", "reason", "expected_count"),
    [
        ({"bitrix_activity_max_requests": 2}, "request_limit", 2),
        ({"bitrix_activity_max_pages": 1}, "page_limit", 2),
        ({"bitrix_activity_max_rows": 1}, "row_limit", 1),
    ],
)
async def test_finite_request_page_and_row_ceilings(
    config_updates: dict[str, int], reason: str, expected_count: int
) -> None:
    async def handler(body: dict[str, object]) -> httpx.Response:
        if _is_freeze(body):
            return httpx.Response(200, json={"result": [{"ID": "3"}]})
        return httpx.Response(
            200,
            json={"result": [_activity("1"), _activity("2")], "next": "ignored"},
        )

    async with _client(handler) as client:
        result = await BitrixCrmActivityRepository(
            _config(**config_updates), client
        ).get_person_crm_activity_metrics(_scope("10"))

    assert result.status == "partial"
    assert result.failure_reason == reason
    assert result.activity_count == expected_count


@pytest.mark.anyio
async def test_deal_limit_is_unavailable_without_network_io() -> None:
    async def no_network(_: dict[str, object]) -> httpx.Response:
        pytest.fail("network")

    result = await BitrixCrmActivityRepository(
        _config(), _client(no_network)
    ).get_person_crm_activity_metrics(_scope("10", exhausted=True))

    assert result.status == "unavailable"
    assert result.failure_reason == "deal_limit"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("scope", "reason"),
    [
        (_scope("10", source_authorized=False), "source_unavailable"),
        (_scope("10", scope_valid=False), "malformed_response"),
    ],
)
async def test_invalid_graph_scope_fails_closed_without_bitrix_io(
    scope: BitrixDealScope, reason: str
) -> None:
    async def no_network(_: dict[str, object]) -> httpx.Response:
        pytest.fail("network")

    result = await BitrixCrmActivityRepository(
        _config(), _client(no_network)
    ).get_person_crm_activity_metrics(scope)

    assert result.status == "unavailable"
    assert result.failure_reason == reason


@pytest.mark.anyio
async def test_http_200_throttle_and_transient_5xx_retry_within_request_budget() -> None:
    responses = iter(
        (
            httpx.Response(200, json={"result": [{"ID": "1"}]}),
            httpx.Response(200, json={"error": "QUERY_LIMIT_EXCEEDED"}),
            httpx.Response(503, json={}),
            httpx.Response(200, json={"result": [_activity("1")]}),
        )
    )

    async def handler(_: dict[str, object]) -> httpx.Response:
        return next(responses)

    async with _client(handler) as client:
        result = await BitrixCrmActivityRepository(
            _config(bitrix_activity_max_attempts=3, bitrix_activity_max_requests=4), client
        ).get_person_crm_activity_metrics(_scope("10"))

    assert result.status == "complete"
    assert result.request_count == 4


@pytest.mark.anyio
async def test_http_429_retries_and_remains_rate_limited_after_attempt_limit() -> None:
    attempts = 0

    async def handler(body: dict[str, object]) -> httpx.Response:
        nonlocal attempts
        if _is_freeze(body):
            return httpx.Response(200, json={"result": [{"ID": "1"}]})
        attempts += 1
        return httpx.Response(429, json={})

    async with _client(handler) as client:
        result = await BitrixCrmActivityRepository(
            _config(), client
        ).get_person_crm_activity_metrics(_scope("10"))

    assert result.status == "unavailable"
    assert result.failure_reason == "rate_limited"
    assert attempts == 2
    assert result.request_count == 3


@pytest.mark.anyio
async def test_non_retryable_error_envelope_is_unavailable_and_reason_is_safe() -> None:
    calls = 0

    async def handler(body: dict[str, object]) -> httpx.Response:
        nonlocal calls
        calls += 1
        if _is_freeze(body):
            return httpx.Response(200, json={"result": [{"ID": "1"}]})
        return httpx.Response(200, json={"error": "ACCESS_DENIED", "error_description": "secret"})

    async with _client(handler) as client:
        result = await BitrixCrmActivityRepository(
            _config(), client
        ).get_person_crm_activity_metrics(_scope("10"))

    assert result.status == "unavailable"
    assert result.failure_reason == "upstream_error"
    assert result.request_count == calls == 2


@pytest.mark.anyio
async def test_timeout_retries_and_each_attempt_consumes_request_budget() -> None:
    calls = 0

    async def handler(body: dict[str, object]) -> httpx.Response:
        nonlocal calls
        if _is_freeze(body):
            return httpx.Response(200, json={"result": [{"ID": "3"}]})
        calls += 1
        raise httpx.ReadTimeout("slow")

    async with _client(handler) as client:
        result = await BitrixCrmActivityRepository(
            _config(bitrix_activity_max_attempts=2, bitrix_activity_max_requests=3), client
        ).get_person_crm_activity_metrics(_scope("10"))

    assert result.status == "unavailable"
    assert result.failure_reason == "timeout"
    assert result.request_count == 3
    assert calls == 2


@pytest.mark.anyio
async def test_overall_deadline_wins_when_it_cancels_single_attempt_io() -> None:
    async def handler(body: dict[str, object]) -> httpx.Response:
        if _is_freeze(body):
            return httpx.Response(200, json={"result": [{"ID": "1"}]})
        await asyncio.sleep(0.6)
        return httpx.Response(200, json={"result": [_activity("1")]})

    async with _client(handler) as client:
        result = await BitrixCrmActivityRepository(
            _config(
                BITRIX_ACTIVITY_MAX_ATTEMPTS=1,
                BITRIX_ACTIVITY_TIMEOUT_SECONDS=1.0,
                BITRIX_ACTIVITY_ELAPSED_SECONDS=0.5,
            ),
            client,
        ).get_person_crm_activity_metrics(_scope("10"))

    assert result.status == "unavailable"
    assert result.failure_reason == "elapsed_limit"
    assert result.request_count == 2


@pytest.mark.anyio
async def test_request_timeout_remains_timeout_when_it_fires_before_deadline() -> None:
    async def handler(body: dict[str, object]) -> httpx.Response:
        if _is_freeze(body):
            return httpx.Response(200, json={"result": [{"ID": "1"}]})
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={"result": [_activity("1")]})

    async with _client(handler) as client:
        result = await BitrixCrmActivityRepository(
            _config(
                BITRIX_ACTIVITY_MAX_ATTEMPTS=1,
                BITRIX_ACTIVITY_TIMEOUT_SECONDS=0.1,
                BITRIX_ACTIVITY_ELAPSED_SECONDS=0.5,
            ),
            client,
        ).get_person_crm_activity_metrics(_scope("10"))

    assert result.status == "unavailable"
    assert result.failure_reason == "timeout"


@pytest.mark.anyio
async def test_keyset_rejects_rows_below_cursor_and_never_uses_offset_pagination() -> None:
    bodies: list[dict[str, object]] = []

    async def handler(body: dict[str, object]) -> httpx.Response:
        bodies.append(body)
        if _is_freeze(body):
            return httpx.Response(200, json={"result": [{"ID": "3"}]})
        filters = body["filter"]
        assert isinstance(filters, dict)
        if ">ID" not in filters:
            return httpx.Response(200, json={"result": [_activity("2")], "next": "ignored"})
        return httpx.Response(200, json={"result": [_activity("1"), _activity("3")]})

    async with _client(handler) as client:
        result = await BitrixCrmActivityRepository(
            _config(), client
        ).get_person_crm_activity_metrics(_scope("10"))

    assert result.status == "partial"
    assert result.failure_reason == "non_advancing_pagination"
    assert all("start" not in body for body in bodies)
    assert bodies[2]["filter"] == {
        "OWNER_TYPE_ID": 2,
        "@OWNER_ID": ["10"],
        "<=ID": "3",
        ">ID": "2",
    }


@pytest.mark.anyio
async def test_elapsed_deadline_bounds_shared_limiter_wait_without_overrelease() -> None:
    repository = BitrixCrmActivityRepository(
        _config(BITRIX_ACTIVITY_MAX_CONCURRENCY=1, BITRIX_ACTIVITY_ELAPSED_SECONDS=0.5),
        _client(lambda _: asyncio.sleep(0, result=httpx.Response(200, json={"result": []}))),
    )
    await repository._limiter.acquire()
    try:
        result = await repository.get_person_crm_activity_metrics(_scope("10"))
    finally:
        repository._limiter.release()

    assert result.status == "unavailable"
    assert result.failure_reason == "elapsed_limit"
    assert await asyncio.wait_for(repository._limiter.acquire(), timeout=0.05)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(repository._limiter.acquire(), timeout=0.05)
    repository._limiter.release()


@pytest.mark.anyio
async def test_repository_wide_limiter_caps_parallel_owner_batches() -> None:
    active = 0
    maximum = 0

    async def handler(body: dict[str, object]) -> httpx.Response:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        if _is_freeze(body):
            return httpx.Response(200, json={"result": []})
        return httpx.Response(200, json={"result": []})

    async with _client(handler) as client:
        result = await BitrixCrmActivityRepository(
            _config(BITRIX_ACTIVITY_OWNER_BATCH_SIZE=1, BITRIX_ACTIVITY_MAX_CONCURRENCY=1), client
        ).get_person_crm_activity_metrics(_scope("10", "11"))

    assert result.status == "complete"
    assert maximum == 1


@pytest.mark.anyio
async def test_complete_only_cache_retains_fetched_at_and_is_bounded() -> None:
    calls = 0

    async def handler(body: dict[str, object]) -> httpx.Response:
        nonlocal calls
        if _is_freeze(body):
            return httpx.Response(200, json={"result": [{"ID": "3"}]})
        calls += 1
        owner = "10" if calls != 2 else "11"
        return httpx.Response(200, json={"result": [_activity(str(calls), owner)]})

    async with _client(handler) as client:
        repository = BitrixCrmActivityRepository(
            _config(BITRIX_ACTIVITY_CACHE_MAX_ENTRIES=1), client
        )
        first = await repository.get_person_crm_activity_metrics(_scope("10"))
        cached = await repository.get_person_crm_activity_metrics(_scope("10"))
        await repository.get_person_crm_activity_metrics(_scope("11"))
        evicted = await repository.get_person_crm_activity_metrics(_scope("10"))

    assert first.status == cached.status == evicted.status == "complete"
    assert cached.cache_disposition == "hit"
    assert cached.fetched_at == first.fetched_at
    assert calls == 3


@pytest.mark.anyio
async def test_single_flight_coalesces_and_cancellation_does_not_cancel_other_waiter() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(body: dict[str, object]) -> httpx.Response:
        nonlocal calls
        if _is_freeze(body):
            return httpx.Response(200, json={"result": [{"ID": "1"}]})
        calls += 1
        started.set()
        await release.wait()
        return httpx.Response(200, json={"result": [_activity("1")]})

    async with _client(handler) as client:
        repository = BitrixCrmActivityRepository(_config(), client)
        cancelled = asyncio.create_task(repository.get_person_crm_activity_metrics(_scope("10")))
        await started.wait()
        survivor = asyncio.create_task(repository.get_person_crm_activity_metrics(_scope("10")))
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        release.set()
        result = await survivor
        await asyncio.sleep(0)

    assert result.status == "complete"
    assert result.cache_disposition == "coalesced"
    assert calls == 1
