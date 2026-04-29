"""Sales history endpoint for the persons resource."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from src.http_utils import envelope, next_cursor, page_window
from src.repositories.deps import get_sales_repo
from src.repositories.protocols.sales import SalesRepository
from src.types import ApiResponse
from src.types_sales import SalesOrder

router = APIRouter(prefix="/v1/persons")


@router.get("/{person_id}/sales", response_model=ApiResponse[list[SalesOrder]])
async def get_person_sales(
    person_id: str,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    repo: SalesRepository = Depends(get_sales_repo),
) -> ApiResponse[list[SalesOrder]]:
    """Return sales orders for a person with line items and products."""
    skip, page_limit = page_window(cursor, limit)
    items, total = await repo.get_person_sales(person_id, skip, page_limit)
    has_more = skip + page_limit < total
    return envelope(items, request, next_cursor(skip, page_limit, has_more), total_count=total)
