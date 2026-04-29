"""Sales repository protocol."""

from __future__ import annotations

from typing import Protocol

from src.types_sales import SalesOrder


class SalesRepository(Protocol):
    async def get_person_sales(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[SalesOrder], int]: ...
