"""HTTP-failure tests for the bounded Bitrix proxy client (issue #432).

The client is the only path that can reach the bounded proxy, so every failure
mode must refuse loudly: a backoff signal for 429, and a contract error for a
declined status, an oversized body, invalid JSON, or an unapproved endpoint.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from src.bounded_ingestion_models import SourceBackoffError
from src.connectors.bitrix_openlines.bounded_client import BitrixBoundedClient
from src.connectors.bitrix_openlines.bounded_contract import (
    BitrixBoundedContractError,
    ChangedDealsPage,
)

_SOURCE_SCOPE = "bitrix-scope-1"
_SNAPSHOT_ID = "snapshot-1"


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_response_bytes: int = 1024 * 1024,
) -> BitrixBoundedClient:
    return BitrixBoundedClient(
        base_url="https://bounded.test",
        timeout_seconds=5.0,
        max_response_bytes=max_response_bytes,
        http=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://bounded.test",
        ),
    )


def _constant(response: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    """Return a transport handler that always replies with one response."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return response

    return handler


def _request_deals(client: BitrixBoundedClient) -> ChangedDealsPage:
    """Issue one approved changed-deal call; every case here refuses it."""
    return client.changed_deals(
        source_scope=_SOURCE_SCOPE,
        snapshot_id=_SNAPSHOT_ID,
        cursor=None,
        max_changes=10,
    )


def test_429_raises_source_backoff_carrying_retry_after() -> None:
    client = _client(_constant(httpx.Response(429, headers={"Retry-After": "120"})))

    observed_at = datetime.now(UTC)
    with pytest.raises(SourceBackoffError) as raised:
        _request_deals(client)

    retry_at = raised.value.retry_at
    assert retry_at.tzinfo is not None
    assert observed_at + timedelta(seconds=110) <= retry_at <= observed_at + timedelta(seconds=130)


def test_429_without_retry_after_backs_off_immediately() -> None:
    client = _client(_constant(httpx.Response(429)))

    observed_at = datetime.now(UTC)
    with pytest.raises(SourceBackoffError) as raised:
        _request_deals(client)

    assert observed_at - timedelta(seconds=5) <= raised.value.retry_at
    assert raised.value.retry_at <= observed_at + timedelta(seconds=5)


def test_declared_content_length_over_the_byte_cap_is_refused() -> None:
    """An oversized declared body is refused before the body is read."""
    client = _client(
        _constant(httpx.Response(200, headers={"Content-Length": "4096"})),
        max_response_bytes=16,
    )

    with pytest.raises(BitrixBoundedContractError, match="byte limit"):
        _request_deals(client)


def test_streamed_body_over_the_byte_cap_is_refused() -> None:
    """An undeclared oversized body is refused while it is being read."""
    client = _client(
        _constant(httpx.Response(200, stream=httpx.ByteStream(b"x" * 64))),
        max_response_bytes=16,
    )

    with pytest.raises(BitrixBoundedContractError, match="byte limit"):
        _request_deals(client)


@pytest.mark.parametrize("status_code", [400, 404, 500, 503])
def test_declined_status_is_refused(status_code: int) -> None:
    client = _client(_constant(httpx.Response(status_code, json={"error": "declined"})))

    with pytest.raises(BitrixBoundedContractError, match="request failed"):
        _request_deals(client)


def test_malformed_json_is_refused() -> None:
    client = _client(_constant(httpx.Response(200, content=b"not-json")))

    with pytest.raises(BitrixBoundedContractError, match="not valid JSON"):
        _request_deals(client)


def test_unapproved_endpoint_is_refused_before_request_accounting() -> None:
    """Only the three frozen endpoints may ever be called."""
    client = _client(_constant(httpx.Response(200, json={"changes": []})))

    with pytest.raises(BitrixBoundedContractError, match="endpoint is not approved"):
        client._post("hyperp.deals.deleted.list", {})

    assert client.request_count == 0
