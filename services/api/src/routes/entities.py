"""Entity endpoints: list entities and their linked persons."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from src.http_utils import envelope, next_cursor, page_window
from src.repositories.deps import get_entity_repo
from src.repositories.protocols.entity import EntityRepository
from src.types import ApiResponse, EntityPerson, EntitySummary

router = APIRouter(prefix="/v1/entities")


@router.get("", response_model=ApiResponse[list[EntitySummary]])
async def list_entities(
    request: Request,
    repo: EntityRepository = Depends(get_entity_repo),
) -> ApiResponse[list[EntitySummary]]:
    """Return all entities with person counts."""
    entities = await repo.get_all()
    return envelope(entities, request)


@router.get("/{entity_key}/persons", response_model=ApiResponse[list[EntityPerson]])
async def list_entity_persons(
    entity_key: str,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    sort_by: str = Query(default="preferred_full_name"),
    sort_order: str = Query(default="asc"),
    repo: EntityRepository = Depends(get_entity_repo),
) -> ApiResponse[list[EntityPerson]]:
    """Return persons linked to an entity with phone confidence and sorting."""
    skip, page_limit = page_window(cursor, limit)
    persons, has_more = await repo.list_persons(entity_key, skip, page_limit, sort_by, sort_order)
    return envelope(persons, request, next_cursor(skip, page_limit, has_more))
