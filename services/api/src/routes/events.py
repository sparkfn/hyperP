"""Downstream event polling endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from src.http_utils import envelope, http_error, next_cursor, page_window
from src.repositories.deps import get_event_repo
from src.repositories.protocols.event import EventRepository
from src.types import ApiResponse, DownstreamEvent

router = APIRouter()


@router.get("/v1/events", response_model=ApiResponse[list[DownstreamEvent]])
async def list_events(
    request: Request,
    since: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    repo: EventRepository = Depends(get_event_repo),
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

    items, has_more = await repo.get_page(since, event_type, skip, page_limit)
    return envelope(items, request, next_cursor(skip, page_limit, has_more))
