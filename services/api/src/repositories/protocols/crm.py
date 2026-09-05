"""Read protocol for graph-backed CRM deal metrics and Bitrix scope resolution."""

from __future__ import annotations

from typing import Protocol

from src.types_crm import BitrixDealScope, PersonCrmDealMetrics


class CrmDealMetricsRepository(Protocol):
    async def get_person_crm_deal_metrics(self, person_id: str) -> PersonCrmDealMetrics | None: ...

    async def resolve_bitrix_deal_scope(
        self, person_id: str, source_instance: str, deal_limit: int
    ) -> BitrixDealScope | None: ...
