"""Entity repository protocol."""

from __future__ import annotations

from typing import Protocol

from src.types import (
    EntityFilterOption,
    EntityMetadata,
    EntityMetrics,
    EntityPerson,
    EntitySummary,
    SourceSystemSummary,
)


class EntityRepository(Protocol):
    async def get_all(self) -> list[EntitySummary]: ...
    async def get_metadata(self) -> list[EntityMetadata]: ...
    async def get_metrics(self) -> list[EntityMetrics]: ...

    async def get_filter_options(self) -> list[EntityFilterOption]: ...

    async def get_source_systems(self) -> list[SourceSystemSummary]: ...

    async def list_persons(
        self,
        entity_key: str,
        skip: int,
        limit: int,
        sort_by: str,
        sort_order: str,
    ) -> tuple[list[EntityPerson], bool]:
        """Returns (items, has_more). has_more detected via +1 fetch."""
        ...
