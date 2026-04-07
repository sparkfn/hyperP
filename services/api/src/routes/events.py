"""Downstream event polling endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from src.graph.client import get_session
from src.graph.converters import GraphRecord, GraphValue
from src.graph.mappers import map_downstream_event
from src.graph.queries import LIST_EVENTS
from src.http_utils import envelope, http_error, next_cursor, page_window
from src.types import ApiResponse, DownstreamEvent

router = APIRouter()


def _record_to_dict(keys: list[str], values: list[GraphValue]) -> GraphRecord:
    return dict(zip(keys, values, strict=True))


@router.get("/v1/events", response_model=ApiResponse[list[DownstreamEvent]])
async def list_events(
    request: Request,
    since: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> ApiResponse[list[DownstreamEvent]]:
    """Poll downstream identity-change events derived from MergeEvent nodes."""
    if since is None:
        raise http_error(
            400,
            "invalid_request",
            "since parameter is required (ISO 8601 timestamp).",
            request,
        )

    skip, page_limit = page_window(cursor, limit)
    page_limit = min(page_limit if limit else 50, 200)

    async with get_session() as session:
        result = await session.run(
            LIST_EVENTS,
            since=since,
            event_type=event_type,
            skip=skip,
            limit=page_limit + 1,
        )
        records = [_record_to_dict(r.keys(), list(r.values())) async for r in result]

    has_more = len(records) > page_limit
    items = [map_downstream_event(rec) for rec in records[:page_limit]]
    return envelope(items, request, next_cursor(skip, page_limit, has_more))
