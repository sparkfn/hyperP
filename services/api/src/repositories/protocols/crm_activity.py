"""Protocol for bounded request-time Bitrix CRM activity reads."""

from __future__ import annotations

from typing import Protocol

from src.types_crm import BitrixDealScope, PersonCrmActivityMetrics


class CrmActivityMetricsRepository(Protocol):
    async def get_person_crm_activity_metrics(
        self, scope: BitrixDealScope
    ) -> PersonCrmActivityMetrics: ...
