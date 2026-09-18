"""Unit tests for the incremental Open Lines connector wrapper."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from src.connectors.bitrix_openlines.incremental import (
    BitrixOpenLinesIncrementalConnector,
    _StubWatermarkStore,
)
from src.incremental_connector import IncrementalConnector
from src.ingestion_config import BitrixOpenLinesConfig
from src.models import JsonValue

_INNER_TARGET = "src.connectors.bitrix_openlines.incremental.BitrixOpenLinesConnector"
_BOUNDARY = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


class _FakeClient:
    """Stand-in for the Bitrix Open Lines client (only ``close`` is used)."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@contextmanager
def _patched_inner(
    records: list[dict[str, JsonValue]],
    pending: datetime | None,
) -> Iterator[tuple[MagicMock, MagicMock]]:
    """Patch the wrapped connector with canned records and a watermark."""
    inner = MagicMock()
    inner.fetch_records.return_value = iter(records)
    inner._pending_watermark = pending
    with patch(_INNER_TARGET, return_value=inner) as patched_class:
        yield patched_class, inner


def _connector(client: _FakeClient) -> BitrixOpenLinesIncrementalConnector:
    return BitrixOpenLinesIncrementalConnector(
        client,
        BitrixOpenLinesConfig(
            included_crm_category_ids=["1"],
            entity_by_crm_category_id={"1": "test_entity"},
        ),
    )


def test_stub_watermark_returns_value() -> None:
    assert _StubWatermarkStore(_BOUNDARY).get(overlap_seconds=0) == _BOUNDARY


def test_stub_watermark_none() -> None:
    assert _StubWatermarkStore(None).get(overlap_seconds=0) is None


def test_stub_watermark_set_is_noop() -> None:
    store = _StubWatermarkStore(_BOUNDARY)
    store.set(datetime(2026, 7, 1, tzinfo=UTC))
    assert store.get(overlap_seconds=0) == _BOUNDARY


def test_stub_watermark_close_is_noop() -> None:
    _StubWatermarkStore(_BOUNDARY).close()


def test_protocol_satisfied() -> None:
    assert isinstance(_connector(_FakeClient()), IncrementalConnector)


def test_source_key() -> None:
    assert _connector(_FakeClient()).get_source_key() == "bitrix_chat"


def test_include_crm_records_false() -> None:
    with _patched_inner([], None) as (patched_class, _inner):
        _connector(_FakeClient()).open_query(_BOUNDARY)
    kwargs = patched_class.call_args.kwargs
    assert kwargs["mode"] == "api"
    assert kwargs["incremental"] is True
    assert kwargs["include_crm_records"] is False
    stub = patched_class.call_args.args[1]
    assert isinstance(stub, _StubWatermarkStore)
    assert stub.get(overlap_seconds=0) == _BOUNDARY


def test_empty_source() -> None:
    with _patched_inner([], None):
        connector = _connector(_FakeClient())
        connector.open_query(_BOUNDARY)
        page = connector.fetch_next_page()
    assert page.records == ()
    assert page.has_more is False
    assert page.max_updated_at == _BOUNDARY


def test_page_batching() -> None:
    records: list[dict[str, JsonValue]] = [{"index": index} for index in range(75)]
    with _patched_inner(records, _BOUNDARY):
        connector = _connector(_FakeClient())
        connector.open_query(_BOUNDARY)
        first = connector.fetch_next_page()
        second = connector.fetch_next_page()
    assert len(first.records) == 50
    assert first.has_more is True
    assert first.records[0] == {"index": 0}
    assert len(second.records) == 25
    assert second.has_more is False
    assert second.records[-1] == {"index": 74}


def test_watermark_from_pending() -> None:
    pending = datetime(2026, 6, 2, 8, 30, tzinfo=UTC)
    with _patched_inner([{"index": 0}], pending):
        connector = _connector(_FakeClient())
        connector.open_query(_BOUNDARY)
        page = connector.fetch_next_page()
    assert page.max_updated_at == pending


def test_close_closes_client() -> None:
    client = _FakeClient()
    connector = _connector(client)
    connector.close()
    assert client.closed is True
