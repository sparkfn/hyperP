"""Protocol and page type for cursor-based incremental connectors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from src.models import JsonValue


@dataclass(frozen=True)
class IncrementalPage:
    """One page of records returned by an incremental connector."""

    records: tuple[dict[str, JsonValue], ...]
    has_more: bool
    max_updated_at: datetime


@runtime_checkable
class IncrementalConnector(Protocol):
    """Stateful, cursor-based source connector for the watermark runner."""

    def open_query(self, updated_since: datetime | None) -> None: ...
    def fetch_next_page(self) -> IncrementalPage: ...
    def get_source_key(self) -> str: ...
    def close(self) -> None: ...
