"""Neo4j implementation of EntityRepository."""

from __future__ import annotations

import asyncio
from time import monotonic

from src.config import config
from src.graph.client import get_session
from src.graph.mappers_entities import (
    map_entity_filter_option,
    map_entity_metadata,
    map_entity_person,
    map_entity_summary,
    map_source_system_summary,
)
from src.graph.queries import (
    LIST_ENTITIES,
    LIST_ENTITY_FILTER_OPTIONS,
    LIST_ENTITY_METADATA,
    LIST_FILTER_SOURCE_SYSTEMS,
    get_entity_persons_query,
)
from src.request_timing import create_detached_task
from src.types import (
    EntityFilterOption,
    EntityMetadata,
    EntityMetrics,
    EntityPerson,
    EntitySummary,
    SourceSystemSummary,
)

from ._utils import record_to_dict


class Neo4jEntityRepository:
    def __init__(self) -> None:
        self._summary_cache: list[EntitySummary] | None = None
        self._summary_cache_expires_at = 0.0
        self._summary_cache_lock = asyncio.Lock()
        self._summary_refresh_task: asyncio.Task[list[EntitySummary]] | None = None

    def _cached_summary(self) -> list[EntitySummary] | None:
        if self._summary_cache is None or monotonic() >= self._summary_cache_expires_at:
            return None
        return [item.model_copy(deep=True) for item in self._summary_cache]

    async def _load_all(self) -> list[EntitySummary]:
        async with get_session() as session:
            result = await session.run(LIST_ENTITIES)
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        return [map_entity_summary(rec) for rec in records]

    async def get_metadata(self) -> list[EntityMetadata]:
        async with get_session() as session:
            result = await session.run(LIST_ENTITY_METADATA)
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        return [map_entity_metadata(record) for record in records]

    async def get_metrics(self) -> list[EntityMetrics]:
        summaries = await self.get_all()
        return [
            EntityMetrics(
                entity_key=item.entity_key,
                person_count=item.person_count,
                source_record_count=item.source_record_count,
                last_ingested_at=item.last_ingested_at,
                active_review_cases=item.active_review_cases,
            )
            for item in summaries
        ]

    async def get_all(self) -> list[EntitySummary]:
        ttl = config.entity_summary_cache_ttl_seconds
        if ttl <= 0:
            return await self._load_all()
        cached = self._cached_summary()
        if cached is not None:
            return cached
        if self._summary_cache is not None:
            self._start_summary_refresh(ttl)
            return [item.model_copy(deep=True) for item in self._summary_cache]
        async with self._summary_cache_lock:
            cached = self._cached_summary()
            if cached is not None:
                return cached
            loaded = await self._load_all()
            self._summary_cache = loaded
            self._summary_cache_expires_at = monotonic() + ttl
            return [item.model_copy(deep=True) for item in loaded]

    def _start_summary_refresh(self, ttl: int) -> None:
        if self._summary_refresh_task is not None and not self._summary_refresh_task.done():
            return
        task = create_detached_task(self._load_all(), background_read=True)
        self._summary_refresh_task = task

        def store_result(completed: asyncio.Task[list[EntitySummary]]) -> None:
            if self._summary_refresh_task is completed:
                self._summary_refresh_task = None
            if completed.cancelled():
                return
            try:
                loaded = completed.result()
            except Exception:
                return
            self._summary_cache = loaded
            self._summary_cache_expires_at = monotonic() + ttl

        task.add_done_callback(store_result)

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
