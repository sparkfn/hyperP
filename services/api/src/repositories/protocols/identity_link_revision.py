"""Repository contract for OAuth identity-link synchronization reads."""

from __future__ import annotations

from typing import Protocol

from src.identity_link_types import IdentityLinkRevision


class IdentityLinkRevisionRepository(Protocol):
    async def event_page(
        self, after_revision: int, through_revision: int, limit: int
    ) -> list[IdentityLinkRevision]: ...

    async def snapshot_page(
        self, snapshot_revision: int, after_link_key: str, limit: int
    ) -> tuple[list[IdentityLinkRevision], str | None]: ...

    async def current_revision_and_ready(self) -> tuple[int, bool]: ...
