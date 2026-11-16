"""Profile repository protocol — database-agnostic interface for C3 Profiles."""

from __future__ import annotations

from typing import Protocol, TypedDict

from src.types import Profile, ProfileDetail


class ProfileListFilters(TypedDict, total=False):
    q: str | None
    status: str | None


class ProfileRepository(Protocol):
    async def get_page(
        self,
        filters: ProfileListFilters,
        skip: int,
        limit: int,
    ) -> tuple[list[Profile], int]: ...

    async def get_by_id(self, profile_id: str) -> ProfileDetail | None: ...
