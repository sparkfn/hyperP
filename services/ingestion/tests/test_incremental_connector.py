"""Structural tests for IncrementalConnector protocol and IncrementalPage."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from src.incremental_connector import IncrementalConnector, IncrementalPage


class _FakeConnector:
    """Minimal class satisfying IncrementalConnector."""

    def open_query(self, updated_since: datetime | None) -> None:
        pass

    def fetch_next_page(self) -> IncrementalPage:
        return IncrementalPage(records=(), has_more=False, max_updated_at=datetime.now(tz=UTC))

    def get_source_key(self) -> str:
        return "fake"

    def close(self) -> None:
        pass


def test_protocol_satisfied_by_fake() -> None:
    connector = _FakeConnector()
    assert isinstance(connector, IncrementalConnector)


def test_incremental_page_frozen() -> None:
    page = IncrementalPage(
        records=(),
        has_more=False,
        max_updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(AttributeError):
        page.has_more = True  # type: ignore[misc]
