"""Async CRM deal-count projection maintenance."""

from __future__ import annotations

from neo4j import AsyncManagedTransaction

from src.graph.queries.crm_deal_count import RECOMPUTE_PERSON_CRM_DEAL_COUNTS


async def recompute_person_crm_deal_counts(
    tx: AsyncManagedTransaction, person_ids: list[str] | tuple[str, ...]
) -> tuple[str, ...]:
    ids = sorted(set(person_ids))
    if not ids:
        return ()
    result = await tx.run(RECOMPUTE_PERSON_CRM_DEAL_COUNTS, person_ids=ids)
    person_ids_result = [str(record["person_id"]) async for record in result]
    return tuple(person_ids_result)
