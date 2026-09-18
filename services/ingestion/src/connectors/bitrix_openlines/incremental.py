"""Incremental Open Lines connector wrapping the existing SourceConnector."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime

from src.connectors.bitrix_openlines.connector import (
    BitrixOpenLinesConnector,
    OpenLinesClient,
)
from src.incremental_connector import IncrementalPage
from src.ingestion_config import BitrixOpenLinesConfig
from src.models import JsonValue

logger = logging.getLogger(__name__)

_DEFAULT_PAGE_SIZE = 50


class _StubWatermarkStore:
    """Seed the inner connector with an updated_since boundary."""

    def __init__(self, updated_since: datetime | None) -> None:
        self._value = updated_since

    def get(self, *, overlap_seconds: int) -> datetime | None:
        return self._value

    def set(self, value: datetime) -> None:
        pass

    def close(self) -> None:
        pass


class BitrixOpenLinesIncrementalConnector:
    """Adapt BitrixOpenLinesConnector to the IncrementalConnector protocol."""

    def __init__(
        self,
        client: OpenLinesClient,
        config: BitrixOpenLinesConfig,
        *,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> None:
        self._client = client
        self._config = config
        self._page_size = page_size
        self._inner: BitrixOpenLinesConnector | None = None
        self._records_iter: Iterator[dict[str, JsonValue]] | None = None
        self._updated_since: datetime | None = None

    def get_source_key(self) -> str:
        return "bitrix_chat"

    def open_query(self, updated_since: datetime | None) -> None:
        self._updated_since = updated_since
        stub = _StubWatermarkStore(updated_since)
        self._inner = BitrixOpenLinesConnector(
            self._client,
            stub,
            self._config,
            mode="api",
            incremental=True,
            include_crm_records=False,
        )
        self._records_iter = self._inner.fetch_records()

    def fetch_next_page(self) -> IncrementalPage:
        if self._records_iter is None or self._inner is None:
            raise RuntimeError("open_query has not been called")
        batch: list[dict[str, JsonValue]] = []
        for record in self._records_iter:
            batch.append(record)
            if len(batch) >= self._page_size:
                break
        has_more = len(batch) >= self._page_size
        pending = self._inner._pending_watermark
        max_ts = pending or self._updated_since or datetime.now(UTC)
        return IncrementalPage(
            records=tuple(batch),
            has_more=has_more,
            max_updated_at=max_ts,
        )

    def close(self) -> None:
        self._records_iter = None
        self._inner = None
        self._client.close()
