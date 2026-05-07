"""Neo4j implementation of SalesRepository."""

from __future__ import annotations

from src.graph.client import get_session
from src.graph.mappers_sales import map_sales_order
from src.graph.queries import COUNT_PERSON_SALES, GET_PERSON_SALES
from src.types_sales import SalesOrder

from ._utils import record_to_dict, to_total


class Neo4jSalesRepository:
    async def get_person_sales(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[SalesOrder], int]:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_SALES, person_id=person_id, skip=skip, limit=limit + 1
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
            count_result = await session.run(COUNT_PERSON_SALES, person_id=person_id)
            count_record = await count_result.single()
        return [map_sales_order(rec) for rec in records[:limit]], to_total(count_record)
