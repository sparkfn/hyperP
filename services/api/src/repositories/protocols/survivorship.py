"""Survivorship repository protocol."""

from __future__ import annotations

from typing import Protocol


class SurvivorshipRepository(Protocol):
    async def recompute_golden_profile(self, person_id: str) -> float | None: ...

    async def create_override(
        self,
        person_id: str,
        attribute_name: str,
        source_record_pk: str,
        reason: str,
        actor_id: str,
    ) -> str:
        """Returns 'ok', 'person_not_found', 'sr_not_found', or 'fact_not_found'."""
        ...
