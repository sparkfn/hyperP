"""Protocol definition for NRIC reveal repository."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class NricUnavailableError(Exception):
    """Raised when all NRIC values for a person are unavailable (no usable ciphertext)."""


@dataclass
class NricRevealResult:
    person_id: str
    reveal_id: str
    revealed_at: str
    values: list[str]
    unavailable_count: int


class NricRevealRepository(Protocol):
    """Protocol for audited NRIC reveal repository operations."""

    async def reveal(
        self,
        person_id: str,
        actor_email: str,
        reason: str,
        request_id: str,
    ) -> NricRevealResult | None: ...
