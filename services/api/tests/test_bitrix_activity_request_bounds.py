"""Behavioral coverage for bounded request-time Bitrix activity reads."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine

import httpx
import pytest
from src.config import AppConfig
from src.repositories.bitrix.activity import BitrixCrmActivityRepository
from src.types_crm import BitrixDealScope

ResponseHandler = Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]


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
        "BITRIX_ACTIVITY_MAX_REQUESTS": 10,
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


def _scope(*ids: str, exhausted: bool = False) -> BitrixDealScope:
    return BitrixDealScope(
        canonical_person_id="canonical-person",
        deal_ids=tuple(ids),
        resolved_deal_count=len(ids) + (1 if exhausted else 0),
        deal_limit_exhausted=exhausted,
    )


def _activity(
    identifier: str,
    owner: str = "10",
    *,
    timestamp: str = "2026-09-01T08:00:00Z",
    kind: str = "2",
    direction: str = "inbound",
    completed: str = "Y",
) -> dict[str, object]:
    return {
        "ID": identifier,
        "OWNER_TYPE_ID": 2,
        "OWNER_ID": owner,
        "TYPE_ID": kind,
        "START_TIME": timestamp,
        "CREATED": "2026-08-01T08:00:00Z",
        "LAST_UPDATED": "2026-09-02T08:00:00Z",
        "DIRECTION": direction,
        "COMPLETED": completed,
    }


def _client(handler: ResponseHandler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _request_json(request: httpx.Request) -> dict[str, object]:
    payload = json.loads(request.content)
    assert isinstance(payload, dict)
    return payload


@pytest.mark.anyio
async def test_empty_scope_is_complete_zero_without_network_io() -> None:
    async def no_network(_: httpx.Request) -> httpx.Response:
        pytest.fail("network")

    repository = BitrixCrmActivityRepository(_config(), _client(no_network))

    result = await repository.get_person_crm_activity_metrics(_scope())

    assert result.status == "complete"
    assert result.activity_count == result.call_count == 0
    assert result.request_count == result.page_count == result.row_count == 0


@pytest.mark.anyio
async def test_request_is_metadata_only_and_strictly_owner_scoped() -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(_request_json(request))
        return httpx.Response(200, json={"result": [_activity("a", "10")]})

    async with _client(handler) as client:
        repository = BitrixCrmActivityRepository(_config(), client)
        result = await repository.get_person_crm_activity_metrics(_scope("10", "11"))

    assert result.status == "complete"
    assert len(requests) == 1
    assert requests[0]["filter"] == {"OWNER_TYPE_ID": 2, "@OWNER_ID": ["10", "11"]}
    selected = requests[0]["select"]
    assert isinstance(selected, list)
    assert "DESCRIPTION" not in selected and "SUBJECT" not in selected


@pytest.mark.anyio
async def test_complete_dedupes_rows_and_uses_timestamp_precedence() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": [
                    _activity("a", timestamp="2026-09-01T08:00:00Z"),
                    _activity("a", timestamp="2026-09-02T08:00:00Z"),
                    _activity("b", kind="email", timestamp="2026-09-03T08:00:00Z"),
                ]
            },
        )

    async with _client(handler) as client:
        repository = BitrixCrmActivityRepository(_config(), client)
        result = await repository.get_person_crm_activity_metrics(_scope("10"))

    assert result.status == "complete"
    assert result.activity_count == 2
    assert result.call_count == 1
    assert result.row_count == 3
    assert result.last_activity_at == "2026-09-03T08:00:00+00:00"
    assert result.call_classification_breakdown[0].classification == "inbound_completed"


@pytest.mark.anyio
async def test_malformed_or_wrong_owner_is_unavailable_before_safe_lower_bound() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": [_activity("a", "other")]})

    async with _client(handler) as client:
        repository = BitrixCrmActivityRepository(_config(), client)
        result = await repository.get_person_crm_activity_metrics(_scope("10"))

    assert result.status == "unavailable"
    assert result.failure_reason == "malformed_response"
    assert result.row_count == 1


@pytest.mark.anyio
async def test_failure_after_accepted_rows_is_partial_lower_bound() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        start = _request_json(request)["start"]
        if start == 0:
            return httpx.Response(200, json={"result": [_activity("a")], "next": 50})
        return httpx.Response(200, json={"result": [{"ID": "bad"}]})

    async with _client(handler) as client:
        repository = BitrixCrmActivityRepository(_config(), client)
        result = await repository.get_person_crm_activity_metrics(_scope("10"))

    assert result.status == "partial"
    assert result.truncated is True
    assert result.activity_count == 1
    assert result.failure_reason == "malformed_response"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("config_updates", "responses", "reason"),
    [
        ({"bitrix_activity_max_requests": 1}, 2, "request_limit"),
        ({"bitrix_activity_max_pages": 1}, 2, "page_limit"),
        ({"bitrix_activity_max_rows": 1}, 1, "row_limit"),
    ],
)
async def test_finite_request_page_and_row_ceilings(
    config_updates: dict[str, int], responses: int, reason: str
) -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "result": [_activity(str(calls * 2)), _activity(str(calls * 2 + 1))],
                "next": 50,
            },
        )

    async with _client(handler) as client:
        repository = BitrixCrmActivityRepository(_config(**config_updates), client)
        result = await repository.get_person_crm_activity_metrics(_scope("10"))

    assert result.status == "partial"
    assert result.failure_reason == reason
    assert calls <= responses


@pytest.mark.anyio
async def test_deal_limit_and_rate_limit_return_unavailable_without_unsafe_zero() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={})

    async with _client(handler) as client:
        repository = BitrixCrmActivityRepository(_config(), client)
        limited = await repository.get_person_crm_activity_metrics(_scope("10", exhausted=True))
        rate_limited = await repository.get_person_crm_activity_metrics(_scope("10"))

    assert limited.status == "unavailable" and limited.failure_reason == "deal_limit"
    assert rate_limited.status == "unavailable" and rate_limited.failure_reason == "rate_limited"
    assert calls == 1


@pytest.mark.anyio
async def test_timeout_retries_and_each_attempt_consumes_request_budget() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow")

    async with _client(handler) as client:
        result = await BitrixCrmActivityRepository(
            _config(bitrix_activity_max_attempts=2, bitrix_activity_max_requests=2), client
        ).get_person_crm_activity_metrics(_scope("10"))

    assert result.status == "unavailable"
    assert result.failure_reason == "timeout"
    assert result.request_count == calls == 2


@pytest.mark.anyio
async def test_nonadvancing_cursor_and_malformed_body_are_unavailable() -> None:
    responses = iter(({"result": [], "next": 0}, {"result": "not-a-list"}))

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    async with _client(handler) as client:
        repository = BitrixCrmActivityRepository(_config(), client)
        first = await repository.get_person_crm_activity_metrics(_scope("10"))
        second = await repository.get_person_crm_activity_metrics(_scope("11"))

    assert first.status == "unavailable" and first.failure_reason == "non_advancing_pagination"
    assert second.status == "unavailable" and second.failure_reason == "malformed_response"


@pytest.mark.anyio
async def test_complete_only_cache_retains_fetched_at_and_is_bounded() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        owner = "10" if calls != 2 else "11"
        return httpx.Response(200, json={"result": [_activity(str(calls), owner)]})

    async with _client(handler) as client:
        repository = BitrixCrmActivityRepository(
            _config(BITRIX_ACTIVITY_CACHE_MAX_ENTRIES=1), client
        )
        first = await repository.get_person_crm_activity_metrics(_scope("10"))
        await asyncio.sleep(0)
        cached = await repository.get_person_crm_activity_metrics(_scope("10"))
        await repository.get_person_crm_activity_metrics(_scope("11"))
        await asyncio.sleep(0)
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

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return httpx.Response(200, json={"result": [_activity("a")]})

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
