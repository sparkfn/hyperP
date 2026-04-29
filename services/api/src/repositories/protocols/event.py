"""Event repository protocol."""

from __future__ import annotations

from typing import Protocol

from src.types import DownstreamEvent


class EventRepository(Protocol):
    async def get_page(
        self,
        since: str,
        event_type: str | None,
        skip: int,
        limit: int,
    ) -> tuple[list[DownstreamEvent], bool]:
        """Returns (items, has_more). has_more detected via +1 fetch."""
        ...
