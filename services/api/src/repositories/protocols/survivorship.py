"""Survivorship repository protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class BatchOverrideResult:
    outcome: str
    """'ok', 'person_not_found', 'sr_not_found', or 'fact_not_found'."""
    failed_attribute: str | None = field(default=None)
    """Attribute name that caused the failure; None when outcome is 'ok'."""


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

    async def create_batch_overrides(
        self,
        person_id: str,
        items: list[tuple[str, str]],
        reason: str,
        actor_id: str,
    ) -> BatchOverrideResult:
        """Apply multiple field overrides atomically in one write transaction."""
        ...
