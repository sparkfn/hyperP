"""Neo4j implementation of ProfileRepository."""

from __future__ import annotations

from src.graph.client import get_session
from src.graph.mappers_profiles import map_profile, map_profile_detail
from src.graph.queries import COUNT_PROFILES, GET_PROFILE, LIST_PROFILES
from src.repositories.protocols.profile import ProfileListFilters
from src.types import Profile, ProfileDetail

from ._utils import record_to_dict, to_total


class Neo4jProfileRepository:
    async def get_page(
        self,
        filters: ProfileListFilters,
        skip: int,
        limit: int,
    ) -> tuple[list[Profile], int]:
        q = filters.get("q")
        status = filters.get("status")
        q_value = q.lower() if q else None
        async with get_session() as session:
            total_record = await (
                await session.run(
                    COUNT_PROFILES,
                    q=q_value,
                    status=status,
                    skip=skip,
                    limit=limit,
                )
            ).single()
            result = await session.run(
                LIST_PROFILES,
                q=q_value,
                status=status,
                skip=skip,
                limit=limit,
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        return [map_profile(record) for record in records], to_total(total_record)

    async def get_by_id(self, profile_id: str) -> ProfileDetail | None:
        async with get_session() as session:
            result = await session.run(GET_PROFILE, profile_id=profile_id)
            record = await result.single()
        if record is None:
            return None
        return map_profile_detail(record_to_dict(record.keys(), list(record.values())))
