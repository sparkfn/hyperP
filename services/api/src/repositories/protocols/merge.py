"""Merge repository protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class MergeOutcome:
    blocked: bool = False
    not_found: bool = False
    merge_event_id: str | None = None


class MergeRepository(Protocol):
    async def manual_merge(
        self, from_id: str, to_id: str, reason: str, actor_id: str
    ) -> MergeOutcome: ...

    async def unmerge(
        self, merge_event_id: str, reason: str, actor_id: str
    ) -> tuple[str, str] | None:
        """Returns (absorbed_id, survivor_id) or None if not found."""
        ...

    async def create_lock(
        self,
        left: str,
        right: str,
        lock_type: str,
        reason: str,
        expires_at: str | None,
        actor_id: str,
    ) -> tuple[str, str | None]:
        """Returns (status, lock_id). Status is 'ok', 'conflict', or 'not_found'."""
        ...

    async def delete_lock(self, lock_id: str) -> bool: ...
