"""Internal production lifecycle commands for Persons."""

from __future__ import annotations

from typing import Protocol


class PersonLifecycleRepository(Protocol):
    async def retire_person(self, person_id: str, reason: str, actor_id: str) -> bool: ...
