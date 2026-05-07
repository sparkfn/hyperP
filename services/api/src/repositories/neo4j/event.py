"""Neo4j implementation of EventRepository."""

from __future__ import annotations

from src.graph.client import get_session
from src.graph.mappers import map_downstream_event
from src.graph.queries import LIST_EVENTS
from src.types import DownstreamEvent

from ._utils import record_to_dict


class Neo4jEventRepository:
    async def get_page(
        self,
        since: str,
        event_type: str | None,
        skip: int,
        limit: int,
    ) -> tuple[list[DownstreamEvent], bool]:
        async with get_session() as session:
            result = await session.run(
                LIST_EVENTS,
                since=since,
                event_type=event_type,
                skip=skip,
                limit=limit + 1,
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        has_more = len(records) > limit
        return [map_downstream_event(rec) for rec in records[:limit]], has_more
