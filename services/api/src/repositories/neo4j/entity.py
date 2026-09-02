"""Neo4j implementation of EntityRepository."""

from __future__ import annotations

import asyncio
from time import monotonic

from src.config import config
from src.graph.client import get_session
from src.graph.mappers_entities import (
    map_entity_filter_option,
    map_entity_person,
    map_entity_summary,
    map_source_system_summary,
)
from src.graph.queries import (
    LIST_ENTITIES,
    LIST_ENTITY_FILTER_OPTIONS,
    LIST_FILTER_SOURCE_SYSTEMS,
    get_entity_persons_query,
)
from src.types import EntityFilterOption, EntityPerson, EntitySummary, SourceSystemSummary

from ._utils import record_to_dict


class Neo4jEntityRepository:
    def __init__(self) -> None:
        self._summary_cache: list[EntitySummary] | None = None
        self._summary_cache_expires_at = 0.0
        self._summary_cache_lock = asyncio.Lock()

    def _cached_summary(self) -> list[EntitySummary] | None:
        if self._summary_cache is None or monotonic() >= self._summary_cache_expires_at:
            return None
        return [item.model_copy(deep=True) for item in self._summary_cache]

    async def _load_all(self) -> list[EntitySummary]:
        async with get_session() as session:
            result = await session.run(LIST_ENTITIES)
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        return [map_entity_summary(rec) for rec in records]

    async def get_all(self) -> list[EntitySummary]:
        ttl = config.entity_summary_cache_ttl_seconds
        if ttl <= 0:
            return await self._load_all()
        cached = self._cached_summary()
        if cached is not None:
            return cached
        async with self._summary_cache_lock:
            cached = self._cached_summary()
            if cached is not None:
                return cached
            loaded = await self._load_all()
            self._summary_cache = loaded
            self._summary_cache_expires_at = monotonic() + ttl
            return [item.model_copy(deep=True) for item in loaded]

    async def get_filter_options(self) -> list[EntityFilterOption]:
        async with get_session() as session:
            result = await session.run(LIST_ENTITY_FILTER_OPTIONS)
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        return [map_entity_filter_option(record) for record in records]

    async def get_source_systems(self) -> list[SourceSystemSummary]:
        async with get_session() as session:
            result = await session.run(LIST_FILTER_SOURCE_SYSTEMS)
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        return [map_source_system_summary(rec) for rec in records]

    async def list_persons(
        self,
        entity_key: str,
        skip: int,
        limit: int,
        sort_by: str,
        sort_order: str,
    ) -> tuple[list[EntityPerson], bool]:
        query = get_entity_persons_query(sort_by, sort_order)
        async with get_session() as session:
            result = await session.run(query, entity_key=entity_key, skip=skip, limit=limit + 1)
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        has_more = len(records) > limit
        return [map_entity_person(rec) for rec in records[:limit]], has_more
