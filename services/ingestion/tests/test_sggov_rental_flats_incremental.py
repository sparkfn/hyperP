from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import patch

import fakeredis
import httpx
import pytest
from src.connectors.sggov.rental_flats_incremental import (
    SGGovernmentRentalFlatsIncrementalConnector,
)
from src.incremental_connector import IncrementalConnector
from src.watermark_store import load_watermark


def _make_item(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "id": 1,
        "block_no": "123",
        "street_name": "ANG MO KIO AVE 3",
        "postal_code": "560123",
        "flat_type": "3-Room",
        "first_seen_at": "2026-01-01T00:00:00Z",
        "last_seen_at": "2026-01-02T00:00:00Z",
        "is_active": True,
        "town": {
            "id": 1,
            "name": "ANG MO KIO",
            "map_id": "AMKT",
            "map_zone": None,
        },
    }
    defaults.update(overrides)
    return defaults


def _connector(
    handler: Any,
    *,
    sleeper: Any = None,
) -> SGGovernmentRentalFlatsIncrementalConnector:
    kwargs: dict[str, Any] = {
        "base_url": "https://rentalflats.test",
        "api_key": "secret",
        "page_size": 10,
        "http": httpx.Client(
            transport=httpx.MockTransport(handler),
        ),
    }
    if sleeper is not None:
        kwargs["sleeper"] = sleeper
    return SGGovernmentRentalFlatsIncrementalConnector(**kwargs)


def test_incremental_connector_satisfies_protocol() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"items": [], "next_cursor": None},
        )

    connector = _connector(handler)
    assert isinstance(connector, IncrementalConnector)


def test_incremental_fetch_all_pages_bootstrap() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        assert "updated_since" not in str(request.url)
        if call_count == 1:
            return httpx.Response(
                200,
                json={
                    "items": [
                        _make_item(
                            id=1,
                            last_seen_at="2026-07-01T12:00:00Z",
                        ),
                    ],
                    "next_cursor": "c2",
                },
            )
        if call_count == 2:
            return httpx.Response(
                200,
                json={
                    "items": [
                        _make_item(
                            id=2,
                            last_seen_at="2026-07-03T12:00:00Z",
                        ),
                    ],
                    "next_cursor": "c3",
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    _make_item(
                        id=3,
                        last_seen_at="2026-07-02T12:00:00Z",
                    ),
                ],
                "next_cursor": None,
            },
        )

    connector = _connector(handler)
    connector.open_query(updated_since=None)

    page1 = connector.fetch_next_page()
    assert page1.has_more is True
    assert len(page1.records) == 1

    page2 = connector.fetch_next_page()
    assert page2.has_more is True
    assert len(page2.records) == 1

    page3 = connector.fetch_next_page()
    assert page3.has_more is False
    assert len(page3.records) == 1

    assert page2.max_updated_at == datetime(
        2026, 7, 3, 12, 0, 0, tzinfo=UTC,
    )
    assert call_count == 3


def test_incremental_fetch_with_updated_since_delta() -> None:
    sent_params: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_params.append(
            request.url.params.get("updated_since"),
        )
        return httpx.Response(
            200,
            json={
                "items": [
                    _make_item(
                        last_seen_at="2026-07-05T12:00:00Z",
                    ),
                ],
                "next_cursor": None,
            },
        )

    connector = _connector(handler)
    since = datetime(2026, 7, 1, tzinfo=UTC)
    connector.open_query(updated_since=since)
    page = connector.fetch_next_page()

    assert sent_params == ["2026-07-01T00:00:00+00:00"]
    assert page.has_more is False
    assert len(page.records) == 1


def test_incremental_updated_at_fallback_to_last_seen_at() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    _make_item(
                        last_seen_at="2026-07-05T12:00:00Z",
                    ),
                ],
                "next_cursor": None,
            },
        )

    connector = _connector(handler)
    connector.open_query(updated_since=None)
    page = connector.fetch_next_page()
    assert page.max_updated_at == datetime(
        2026, 7, 5, 12, 0, 0, tzinfo=UTC,
    )


def test_incremental_empty_page_returns_no_records() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"items": [], "next_cursor": None},
        )

    connector = _connector(handler)
    connector.open_query(updated_since=None)
    page = connector.fetch_next_page()
    assert page.records == ()
    assert page.has_more is False


def test_incremental_cursor_stall_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"items": [], "next_cursor": "stuck"},
        )

    connector = _connector(handler)
    connector.open_query(updated_since=None)
    connector.fetch_next_page()
    with pytest.raises(RuntimeError, match="did not advance"):
        connector.fetch_next_page()


def test_incremental_retries_server_errors() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            return httpx.Response(
                503, json={"error": "unavailable"},
            )
        return httpx.Response(
            200, json={"items": [], "next_cursor": None},
        )

    connector = _connector(handler, sleeper=lambda _: None)
    connector.open_query(updated_since=None)
    page = connector.fetch_next_page()
    assert page.records == ()
    assert attempts == 2


def test_incremental_retries_transport_errors() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError(
                "offline", request=request,
            )
        return httpx.Response(
            200, json={"items": [], "next_cursor": None},
        )

    connector = _connector(handler, sleeper=lambda _: None)
    connector.open_query(updated_since=None)
    page = connector.fetch_next_page()
    assert page.records == ()
    assert attempts == 2


def test_incremental_propagates_auth_failure() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            401, json={"error": "unauthorized"},
        )

    connector = _connector(handler)
    connector.open_query(updated_since=None)
    with pytest.raises(httpx.HTTPStatusError):
        connector.fetch_next_page()
    assert attempts == 1


def test_incremental_fetch_before_open_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"items": [], "next_cursor": None},
        )

    connector = _connector(handler)
    with pytest.raises(RuntimeError, match="open_query"):
        connector.fetch_next_page()


def test_incremental_get_source_key() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"items": [], "next_cursor": None},
        )

    connector = _connector(handler)
    assert connector.get_source_key() == "sgrentalflats"


def test_incremental_connector_registered_in_task_registry() -> None:
    from src.tasks import INCREMENTAL_CONNECTORS

    assert "sgrentalflats" in INCREMENTAL_CONNECTORS
    assert callable(INCREMENTAL_CONNECTORS["sgrentalflats"])


# ---------------------------------------------------------------------------
# Integration: watermark drain
# ---------------------------------------------------------------------------


class _Result:
    def __init__(
        self, data: dict[str, str] | None = None,
    ) -> None:
        self._data = data

    def single(self) -> dict[str, str] | None:
        return self._data


class _Transaction:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(
        self, query: str, **params: object,
    ) -> _Result:
        self.calls.append((query, params))
        if "CREATE_INGEST_RUN" in query or "CREATE" in query:
            return _Result({"ingest_run_id": "test-run-1"})
        return _Result(None)


class _Session:
    def __init__(self, tx: _Transaction) -> None:
        self._tx = tx

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def execute_write(
        self, work: Any, **_kwargs: object,
    ) -> Any:
        return work(cast(Any, self._tx))


class _FakeGraphClient:
    def __init__(self) -> None:
        self.tx = _Transaction()
        self._session = _Session(self.tx)

    def session(self) -> _Session:
        return self._session

    def close(self) -> None:
        pass


def test_incremental_rental_flats_run_advances_watermark_on_drain() -> None:
    from src.watermark_runner import run_incremental

    page_fetches = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal page_fetches
        page_fetches += 1
        return httpx.Response(
            200,
            json={
                "items": [
                    _make_item(
                        id=page_fetches,
                        last_seen_at="2026-08-01T10:00:00Z",
                        updated_at="2026-08-01T10:00:00Z",
                    ),
                ],
                "next_cursor": None,
            },
        )

    redis_client = fakeredis.FakeRedis()
    graph_client = cast(Any, _FakeGraphClient())

    connector = _connector(handler)

    with (
        patch(
            "src.watermark_runner._load_exclusion_context",
            return_value=cast(Any, None),
        ),
        patch(
            "src.watermark_runner._process_page_records",
            return_value=(1, 0),
        ),
    ):
        summary = run_incremental(
            connector,
            redis_client,
            graph_client,
            shutdown_signal=lambda: False,
            time_window_closing=lambda: False,
        )

    assert summary["status"] == "caught_up"
    watermark = load_watermark(redis_client, "sgrentalflats")
    assert watermark.updated_at == datetime(
        2026, 8, 1, 10, 0, 0, tzinfo=UTC,
    )

    # Scenario B: yielded — watermark not advanced
    page_fetches_b = 0

    def handler_b(_request: httpx.Request) -> httpx.Response:
        nonlocal page_fetches_b
        page_fetches_b += 1
        return httpx.Response(
            200,
            json={
                "items": [
                    _make_item(
                        id=100 + page_fetches_b,
                        last_seen_at="2026-09-01T10:00:00Z",
                        updated_at="2026-09-01T10:00:00Z",
                    ),
                ],
                "next_cursor": f"page{page_fetches_b + 1}",
            },
        )

    redis_client_b = fakeredis.FakeRedis()
    graph_client_b = cast(Any, _FakeGraphClient())
    connector_b = _connector(handler_b)

    shutdown_calls = 0

    def shutdown_after_first() -> bool:
        nonlocal shutdown_calls
        shutdown_calls += 1
        return shutdown_calls > 1

    with (
        patch(
            "src.watermark_runner._load_exclusion_context",
            return_value=cast(Any, None),
        ),
        patch(
            "src.watermark_runner._process_page_records",
            return_value=(1, 0),
        ),
    ):
        summary_b = run_incremental(
            connector_b,
            redis_client_b,
            graph_client_b,
            shutdown_signal=shutdown_after_first,
            time_window_closing=lambda: False,
        )

    assert summary_b["status"] == "yielded"
    watermark_b = load_watermark(
        redis_client_b, "sgrentalflats",
    )
    assert watermark_b.updated_at is None


def test_incremental_timestamps_normalized_to_utc() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    _make_item(
                        id=1,
                        last_seen_at="2026-07-01T12:00:00",
                        updated_at="2026-07-02T12:00:00+00:00",
                    ),
                    _make_item(
                        id=2,
                        last_seen_at="2026-07-03T12:00:00",
                    ),
                ],
                "next_cursor": None,
            },
        )

    connector = _connector(handler)
    connector.open_query(updated_since=None)
    page = connector.fetch_next_page()
    assert page.max_updated_at.tzinfo is not None
    assert page.max_updated_at == datetime(
        2026, 7, 3, 12, 0, 0, tzinfo=UTC,
    )
