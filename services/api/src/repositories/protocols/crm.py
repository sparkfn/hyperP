"""CRM metrics repository protocol."""

from __future__ import annotations

from typing import Protocol

from src.types_crm import PersonCrmMetrics


class CrmMetricsRepository(Protocol):
    async def get_person_crm_metrics(self, person_id: str) -> PersonCrmMetrics | None: ...
