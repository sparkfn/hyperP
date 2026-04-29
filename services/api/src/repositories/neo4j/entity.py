"""Neo4j implementation of EntityRepository."""

from __future__ import annotations

from src.graph.client import get_session
from src.graph.mappers_entities import map_entity_person, map_entity_summary
from src.graph.queries import LIST_ENTITIES, get_entity_persons_query
from src.types import EntityPerson, EntitySummary

from ._utils import record_to_dict


class Neo4jEntityRepository:
    async def get_all(self) -> list[EntitySummary]:
        async with get_session() as session:
            result = await session.run(LIST_ENTITIES)
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        return [map_entity_summary(rec) for rec in records]

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
