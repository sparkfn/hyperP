"""Entity endpoints: list entities and their linked persons."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from src.graph.client import get_session
from src.graph.converters import GraphRecord, GraphValue
from src.graph.mappers_entities import map_entity_person, map_entity_summary
from src.graph.queries import LIST_ENTITIES, get_entity_persons_query
from src.http_utils import envelope, next_cursor, page_window
from src.types import ApiResponse, EntityPerson, EntitySummary

router = APIRouter(prefix="/v1/entities")


def _record_to_dict(keys: list[str], values: list[GraphValue]) -> GraphRecord:
    return dict(zip(keys, values, strict=True))


@router.get("", response_model=ApiResponse[list[EntitySummary]])
async def list_entities(request: Request) -> ApiResponse[list[EntitySummary]]:
    """Return all entities with person counts."""
    async with get_session() as session:
        result = await session.run(LIST_ENTITIES)
        records = [_record_to_dict(r.keys(), list(r.values())) async for r in result]
    entities = [map_entity_summary(rec) for rec in records]
    return envelope(entities, request)


@router.get("/{entity_key}/persons", response_model=ApiResponse[list[EntityPerson]])
async def list_entity_persons(
    entity_key: str,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    sort_by: str = Query(default="preferred_full_name"),
    sort_order: str = Query(default="asc"),
) -> ApiResponse[list[EntityPerson]]:
    """Return persons linked to an entity with phone confidence and sorting."""
    skip, page_limit = page_window(cursor, limit)
    query = get_entity_persons_query(sort_by, sort_order)
    async with get_session() as session:
        result = await session.run(
            query, entity_key=entity_key, skip=skip, limit=page_limit + 1
        )
        records = [_record_to_dict(r.keys(), list(r.values())) async for r in result]
    has_more = len(records) > page_limit
    persons = [map_entity_person(rec) for rec in records[:page_limit]]
    return envelope(persons, request, next_cursor(skip, page_limit, has_more))
