"""Sales history endpoint for the persons resource."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from src.graph.client import get_session
from src.graph.converters import GraphRecord, GraphValue
from src.graph.mappers_sales import map_sales_order
from src.graph.queries import COUNT_PERSON_SALES, GET_PERSON_SALES
from src.http_utils import envelope, next_cursor, page_window
from src.types import ApiResponse
from src.types_sales import SalesOrder

router = APIRouter(prefix="/v1/persons")


def _record_to_dict(keys: list[str], values: list[GraphValue]) -> GraphRecord:
    return dict(zip(keys, values, strict=True))


def _to_total(record: object | None) -> int:
    if record is None:
        return 0
    try:
        val = record["total"]  # type: ignore[index]  # neo4j Record supports subscript
        return int(val) if val is not None else 0
    except (KeyError, TypeError, ValueError):
        return 0


@router.get("/{person_id}/sales", response_model=ApiResponse[list[SalesOrder]])
async def get_person_sales(
    person_id: str,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> ApiResponse[list[SalesOrder]]:
    """Return sales orders for a person with line items and products."""
    skip, page_limit = page_window(cursor, limit)
    async with get_session() as session:
        result = await session.run(
            GET_PERSON_SALES, person_id=person_id, skip=skip, limit=page_limit + 1
        )
        records: list[GraphRecord] = [
            _record_to_dict(r.keys(), list(r.values())) async for r in result
        ]
        count_result = await session.run(COUNT_PERSON_SALES, person_id=person_id)
        count_record = await count_result.single()
    has_more = len(records) > page_limit
    items = [map_sales_order(rec) for rec in records[:page_limit]]
    return envelope(
        items, request, next_cursor(skip, page_limit, has_more), total_count=_to_total(count_record)
    )
