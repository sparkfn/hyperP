"""Unit tests for the Fundbox incremental (watermark) connector."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from src.connectors.fundbox_api.incremental import FundboxIncrementalConnector
from src.connectors.fundbox_api.models import PageMeta
from src.incremental_connector import IncrementalConnector
from src.models import JsonValue
from src.tasks import INCREMENTAL_CONNECTORS
from src.watermark_store import load_watermark

_TS1 = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC)
_TS1_ISO = "2026-09-01T10:00:00+00:00"
_TS2 = datetime(2026, 9, 2, 10, 0, 0, tzinfo=UTC)
_TS2_ISO = "2026-09-02T10:00:00+00:00"
_TS_MID = "2026-09-01T12:00:00+00:00"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRedis:
    """Minimal in-memory Redis stand-in."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, name: str) -> bytes | str | None:
        return self._store.get(name)

    def set(self, name: str, value: str | bytes, **kwargs: object) -> object:
        self._store[name] = value.decode("utf-8") if isinstance(value, bytes) else value
        return True

    def delete(self, *names: str) -> object:
        for n in names:
            self._store.pop(n, None)
        return len(names)


class FakeNeo4jTx:
    """Stub for ManagedTransaction."""

    def __init__(self, run_result: object = None) -> None:
        self._run_result = run_result
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **kwargs: object) -> FakeNeo4jTx:
        self.calls.append((query, kwargs))
        return self

    def single(self) -> dict[str, object] | None:
        return {"ingest_run_id": "run-001", "status": "started", "created": True}


class FakeNeo4jSession:
    """Stub for Neo4j Session."""

    def __init__(self) -> None:
        self.tx = FakeNeo4jTx()

    def execute_write(self, fn: object) -> object:
        assert isinstance(fn, Callable)
        return fn(self.tx)

    def __enter__(self) -> FakeNeo4jSession:
        return self

    def __exit__(self, *args: object) -> None:
        pass


class FakeNeo4jClient:
    """Stub for Neo4jClient."""

    def __init__(self) -> None:
        self._session = FakeNeo4jSession()

    def session(self) -> FakeNeo4jSession:
        return self._session


class StubFundboxClient:
    """In-memory FundboxApiClient stand-in returning canned pages."""

    def __init__(
        self, pages: list[tuple[list[dict[str, JsonValue]], PageMeta]]
    ) -> None:
        self._pages = pages
        self._index = 0
        self.closed = False
        self.fetch_calls: list[dict[str, str | None]] = []

    def fetch_page(
        self,
        resource: str,
        *,
        cursor: str | None = None,
        updated_since: str | None = None,
    ) -> tuple[list[dict[str, JsonValue]], PageMeta]:
        self.fetch_calls.append(
            {"resource": resource, "cursor": cursor, "updated_since": updated_since}
        )
        result = self._pages[self._index]
        self._index += 1
        return result

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(ts: str) -> dict[str, JsonValue]:
    return {"effective_updated_at": ts}


def _page(
    records: list[dict[str, JsonValue]],
    *,
    has_more: bool,
    next_cursor: str | None = None,
) -> tuple[list[dict[str, JsonValue]], PageMeta]:
    return records, PageMeta(next_cursor=next_cursor, has_more=has_more)


def _connector(
    client: StubFundboxClient,
    *,
    resource: str = "users",
    source_key: str = "fundbox",
) -> FundboxIncrementalConnector:
    return FundboxIncrementalConnector(
        client,  # type: ignore[arg-type]
        resource,
        source_key,
        lambda rec: rec,
    )


_SOURCE_RESOURCES = (
    ("fundbox", "users"),
    ("fundbox:contacts", "contacts"),
    ("fundbox:sales", "sales"),
)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_protocol_satisfaction() -> None:
    for source_key, resource in _SOURCE_RESOURCES:
        connector = _connector(
            StubFundboxClient([]),
            resource=resource,
            source_key=source_key,
        )
        assert isinstance(connector, IncrementalConnector)


def test_single_page_drain() -> None:
    client = StubFundboxClient([_page([_record(_TS1_ISO)], has_more=False)])
    connector = _connector(client)
    connector.open_query(None)

    page = connector.fetch_next_page()

    assert page.has_more is False
    assert page.max_updated_at == _TS1
    assert len(page.records) == 1


def test_multi_page_drain() -> None:
    client = StubFundboxClient(
        [
            _page([_record(_TS1_ISO)], has_more=True, next_cursor="c1"),
            _page([_record(_TS2_ISO)], has_more=False),
        ]
    )
    connector = _connector(client)
    connector.open_query(None)

    first = connector.fetch_next_page()
    second = connector.fetch_next_page()

    assert first.has_more is True
    assert second.has_more is False
    assert [call["cursor"] for call in client.fetch_calls] == [None, "c1"]
    assert client.fetch_calls[0]["resource"] == "users"
    assert client.fetch_calls[0]["updated_since"] is None
    assert client.fetch_calls[1]["updated_since"] is None


def test_repeated_cursor_raises() -> None:
    client = StubFundboxClient(
        [
            _page([_record(_TS1_ISO)], has_more=True, next_cursor="c1"),
            _page([_record(_TS2_ISO)], has_more=True, next_cursor="c1"),
        ]
    )
    connector = _connector(client)
    connector.open_query(None)
    connector.fetch_next_page()

    with pytest.raises(ValueError, match="repeated cursor"):
        connector.fetch_next_page()


def test_exhaustion_guard() -> None:
    client = StubFundboxClient([_page([_record(_TS1_ISO)], has_more=False)])
    connector = _connector(client)
    connector.open_query(None)
    connector.fetch_next_page()

    with pytest.raises(RuntimeError, match="exhausted"):
        connector.fetch_next_page()


def test_open_query_with_datetime() -> None:
    client = StubFundboxClient([_page([], has_more=False)])
    connector = _connector(client)
    connector.open_query(_TS1)

    connector.fetch_next_page()

    assert client.fetch_calls[0]["updated_since"] == _TS1_ISO


def test_open_query_with_none() -> None:
    client = StubFundboxClient([_page([_record(_TS1_ISO)], has_more=False)])
    connector = _connector(client)
    connector.open_query(None)

    connector.fetch_next_page()

    assert client.fetch_calls[0]["updated_since"] is None


def test_close_delegates() -> None:
    client = StubFundboxClient([])
    connector = _connector(client)

    connector.close()

    assert client.closed is True


def test_max_updated_at_tracks_latest() -> None:
    client = StubFundboxClient(
        [_page([_record(_TS1_ISO), _record(_TS_MID), _record(_TS2_ISO)], has_more=False)]
    )
    connector = _connector(client)
    connector.open_query(None)

    page = connector.fetch_next_page()

    assert page.max_updated_at == _TS2


def test_max_updated_at_timezone_aware() -> None:
    client = StubFundboxClient([_page([_record(_TS1_ISO)], has_more=False)])
    connector = _connector(client)
    connector.open_query(None)

    page = connector.fetch_next_page()

    assert page.max_updated_at.tzinfo is not None


def test_empty_page_fallback() -> None:
    client = StubFundboxClient([_page([], has_more=False)])
    connector = _connector(client)
    connector.open_query(_TS1)

    assert connector.fetch_next_page().max_updated_at == _TS1

    bootstrap_client = StubFundboxClient([_page([], has_more=False)])
    bootstrap_connector = _connector(bootstrap_client)
    bootstrap_connector.open_query(None)

    fallback = bootstrap_connector.fetch_next_page().max_updated_at

    assert fallback.tzinfo is not None
    assert abs((datetime.now(UTC) - fallback).total_seconds()) < 5


def test_get_source_key_per_resource() -> None:
    for source_key, resource in _SOURCE_RESOURCES:
        connector = _connector(
            StubFundboxClient([]),
            resource=resource,
            source_key=source_key,
        )
        assert connector.get_source_key() == source_key


def test_factory_registration() -> None:
    for source_key, _resource in _SOURCE_RESOURCES:
        assert source_key in INCREMENTAL_CONNECTORS
        assert callable(INCREMENTAL_CONNECTORS[source_key])


def test_records_are_tuple() -> None:
    client = StubFundboxClient([_page([_record(_TS1_ISO)], has_more=False)])
    connector = _connector(client)
    connector.open_query(None)

    page = connector.fetch_next_page()

    assert isinstance(page.records, tuple)
    assert len(page.records) == 1


# ---------------------------------------------------------------------------
# End-to-end runner integration
# ---------------------------------------------------------------------------


@patch("src.watermark_runner._load_exclusion_context")
@patch("src.watermark_runner.IngestPipeline")
@patch("src.watermark_runner._process_page_records", return_value=(1, 0))
def test_e2e_runner_integration(
    mock_process: MagicMock,
    mock_pipeline: MagicMock,
    mock_exclusion: MagicMock,
) -> None:
    from src.watermark_runner import run_incremental

    redis = FakeRedis()
    graph = FakeNeo4jClient()

    first_client = StubFundboxClient(
        [
            _page([_record(_TS1_ISO)], has_more=True, next_cursor="c1"),
            _page([_record(_TS2_ISO)], has_more=False),
        ]
    )
    first = _connector(first_client)

    summary = run_incremental(
        first,
        redis,  # type: ignore[arg-type]
        graph,  # type: ignore[arg-type]
        shutdown_signal=lambda: False,
        time_window_closing=lambda: False,
    )

    assert summary["status"] == "caught_up"
    assert summary["source_key"] == "fundbox"
    assert summary["pages_processed"] == 2
    assert summary["records_processed"] == 2
    assert summary["watermark_start"] is None
    assert summary["watermark_end"] == _TS2_ISO
    assert first_client.closed is True

    stored = load_watermark(redis, "fundbox")  # type: ignore[arg-type]
    assert stored.updated_at == _TS2

    second_client = StubFundboxClient([_page([_record(_TS2_ISO)], has_more=False)])
    second = _connector(second_client)

    second_summary = run_incremental(
        second,
        redis,  # type: ignore[arg-type]
        graph,  # type: ignore[arg-type]
        shutdown_signal=lambda: False,
        time_window_closing=lambda: False,
    )

    assert second_summary["status"] == "caught_up"
    assert second_summary["watermark_start"] == _TS2_ISO
    assert second_client.fetch_calls[0]["updated_since"] == _TS2_ISO
