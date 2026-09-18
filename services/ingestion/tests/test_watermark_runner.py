"""Unit tests for the watermark runner (all in-memory fakes)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from src.incremental_connector import IncrementalPage
from src.watermark_store import IngestionWatermark

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


class FakeConnector:
    """In-memory IncrementalConnector with canned pages."""

    def __init__(self, pages: list[IncrementalPage]) -> None:
        self._pages = list(pages)
        self._index = 0
        self.opened_with: datetime | None = None
        self.closed = False

    def open_query(self, updated_since: datetime | None) -> None:
        self.opened_with = updated_since

    def fetch_next_page(self) -> IncrementalPage:
        page = self._pages[self._index]
        self._index += 1
        return page

    def get_source_key(self) -> str:
        return "test_source"

    def close(self) -> None:
        self.closed = True


class ErrorConnector(FakeConnector):
    """Connector that raises on fetch_next_page."""

    def __init__(self, error: Exception) -> None:
        super().__init__([])
        self._error = error

    def fetch_next_page(self) -> IncrementalPage:
        raise self._error


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
        from collections.abc import Callable

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_page(
    n_records: int,
    max_ts: datetime,
    has_more: bool,
) -> IncrementalPage:
    records = tuple(
        {
            "source_record_id": f"rec-{i}",
            "name": f"Test {i}",
            "identifiers": [],
            "raw_payload": {},
        }
        for i in range(n_records)
    )
    return IncrementalPage(records=records, has_more=has_more, max_updated_at=max_ts)


_TS1 = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC)
_TS2 = datetime(2026, 9, 2, 10, 0, 0, tzinfo=UTC)
_TS3 = datetime(2026, 9, 3, 10, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@patch("src.watermark_runner._load_exclusion_context")
@patch("src.watermark_runner.IngestPipeline")
@patch("src.watermark_runner._process_page_records", return_value=(3, 0))
def test_bootstrap_drains_all_pages_and_advances_watermark(
    mock_process: MagicMock,
    mock_pipeline: MagicMock,
    mock_exclusion: MagicMock,
) -> None:
    from src.watermark_runner import run_incremental
    from src.watermark_store import load_watermark

    redis = FakeRedis()
    client = FakeNeo4jClient()

    pages = [
        _make_page(3, _TS1, has_more=True),
        _make_page(3, _TS2, has_more=True),
        _make_page(3, _TS3, has_more=False),
    ]
    connector = FakeConnector(pages)

    result = run_incremental(
        connector,
        redis,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        shutdown_signal=lambda: False,
        time_window_closing=lambda: False,
    )
    assert result["status"] == "caught_up"
    assert result["pages_processed"] == 3
    assert result["records_processed"] == 9
    assert result["watermark_end"] == _TS3.isoformat()

    wm = load_watermark(redis, "test_source")
    assert wm.updated_at == _TS3
    assert connector.closed


@patch("src.watermark_runner._load_exclusion_context")
@patch("src.watermark_runner.IngestPipeline")
@patch("src.watermark_runner._process_page_records", return_value=(2, 0))
def test_delta_passes_updated_since_to_connector(
    mock_process: MagicMock,
    mock_pipeline: MagicMock,
    mock_exclusion: MagicMock,
) -> None:
    from src.watermark_runner import run_incremental
    from src.watermark_store import save_watermark

    redis = FakeRedis()
    client = FakeNeo4jClient()

    save_watermark(
        redis,
        IngestionWatermark(updated_at=_TS1, source_key="test_source", entity_key=None),
    )

    pages = [_make_page(2, _TS2, has_more=False)]
    connector = FakeConnector(pages)

    result = run_incremental(
        connector,
        redis,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        shutdown_signal=lambda: False,
        time_window_closing=lambda: False,
    )
    assert result["status"] == "caught_up"
    assert result["watermark_start"] == _TS1.isoformat()
    assert connector.opened_with == _TS1


@patch("src.watermark_runner._load_exclusion_context")
@patch("src.watermark_runner.IngestPipeline")
@patch("src.watermark_runner._process_page_records", return_value=(3, 0))
def test_window_closing_yields_without_advancing_watermark(
    mock_process: MagicMock,
    mock_pipeline: MagicMock,
    mock_exclusion: MagicMock,
) -> None:
    from src.watermark_runner import run_incremental
    from src.watermark_store import load_watermark

    redis = FakeRedis()
    client = FakeNeo4jClient()

    pages = [_make_page(3, _TS1, has_more=True)]
    connector = FakeConnector(pages)

    result = run_incremental(
        connector,
        redis,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        shutdown_signal=lambda: False,
        time_window_closing=lambda: True,
    )
    assert result["status"] == "yielded"

    wm = load_watermark(redis, "test_source")
    assert wm.updated_at is None
    assert connector.closed


@patch("src.watermark_runner._load_exclusion_context")
@patch("src.watermark_runner.IngestPipeline")
@patch("src.watermark_runner._process_page_records", return_value=(3, 0))
def test_shutdown_signal_yields(
    mock_process: MagicMock,
    mock_pipeline: MagicMock,
    mock_exclusion: MagicMock,
) -> None:
    from src.watermark_runner import run_incremental

    redis = FakeRedis()
    client = FakeNeo4jClient()

    pages = [_make_page(3, _TS1, has_more=True)]
    connector = FakeConnector(pages)

    result = run_incremental(
        connector,
        redis,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        shutdown_signal=lambda: True,
        time_window_closing=lambda: False,
    )
    assert result["status"] == "yielded"
    assert result["watermark_end"] is None
    assert connector.closed


@patch("src.watermark_runner._load_exclusion_context")
@patch("src.watermark_runner.IngestPipeline")
def test_exception_in_fetch_marks_run_failed(
    mock_pipeline: MagicMock,
    mock_exclusion: MagicMock,
) -> None:
    from src.watermark_runner import run_incremental
    from src.watermark_store import load_watermark

    redis = FakeRedis()
    client = FakeNeo4jClient()
    connector = ErrorConnector(RuntimeError("connection lost"))

    with pytest.raises(RuntimeError, match="connection lost"):
        run_incremental(
            connector,
            redis,  # type: ignore[arg-type]
            client,  # type: ignore[arg-type]
            shutdown_signal=lambda: False,
            time_window_closing=lambda: False,
        )
    wm = load_watermark(redis, "test_source")
    assert wm.updated_at is None
    assert connector.closed


@patch("src.watermark_runner._load_exclusion_context")
@patch("src.watermark_runner.IngestPipeline")
@patch("src.watermark_runner._process_page_records", return_value=(0, 0))
def test_empty_first_page_is_caught_up(
    mock_process: MagicMock,
    mock_pipeline: MagicMock,
    mock_exclusion: MagicMock,
) -> None:
    from src.watermark_runner import run_incremental

    redis = FakeRedis()
    client = FakeNeo4jClient()

    ts = datetime(2026, 9, 1, tzinfo=UTC)
    pages = [_make_page(0, ts, has_more=False)]
    connector = FakeConnector(pages)

    result = run_incremental(
        connector,
        redis,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        shutdown_signal=lambda: False,
        time_window_closing=lambda: False,
    )
    assert result["status"] == "caught_up"
    assert result["pages_processed"] == 1


@patch("src.watermark_runner._load_exclusion_context")
@patch("src.watermark_runner.IngestPipeline")
@patch("src.watermark_runner._process_page_records", return_value=(1, 0))
def test_max_updated_at_tracks_across_pages(
    mock_process: MagicMock,
    mock_pipeline: MagicMock,
    mock_exclusion: MagicMock,
) -> None:
    from src.watermark_runner import run_incremental
    from src.watermark_store import load_watermark

    redis = FakeRedis()
    client = FakeNeo4jClient()

    pages = [
        _make_page(1, _TS3, has_more=True),
        _make_page(1, _TS1, has_more=False),
    ]
    connector = FakeConnector(pages)

    result = run_incremental(
        connector,
        redis,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        shutdown_signal=lambda: False,
        time_window_closing=lambda: False,
    )
    assert result["watermark_end"] == _TS3.isoformat()

    wm = load_watermark(redis, "test_source")
    assert wm.updated_at == _TS3
