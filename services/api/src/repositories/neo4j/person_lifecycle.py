"""Neo4j implementation of the internal Person retirement command."""

from __future__ import annotations

from neo4j import AsyncManagedTransaction

from src.graph.client import get_session
from src.graph.queries.person_lifecycle import RETIRE_PERSON
from src.identity_link_revisions import append_person_retirement_revisions


class Neo4jPersonLifecycleRepository:
    async def retire_person(self, person_id: str, reason: str, actor_id: str) -> bool:
        async with get_session(write=True) as session:
            return await session.execute_write(_retire_person_tx, person_id, reason, actor_id)


async def _retire_person_tx(
    tx: AsyncManagedTransaction,
    person_id: str,
    reason: str,
    actor_id: str,
) -> bool:
    result = await tx.run(
        RETIRE_PERSON,
        person_id=person_id,
        reason=reason,
        actor_id=actor_id,
    )
    record = await result.single()
    if record is None:
        return False
    event_id = str(record["lifecycle_event_id"])
    await append_person_retirement_revisions(
        tx,
        person_id=person_id,
        cause_prefix=f"person-retirement:{event_id}",
        effective_at=str(record["retired_at"]),
    )
    return True
