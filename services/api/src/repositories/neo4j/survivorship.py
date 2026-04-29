"""Neo4j implementation of SurvivorshipRepository."""

from __future__ import annotations

from datetime import UTC, datetime

from neo4j import AsyncManagedTransaction

from src.graph.client import get_session
from src.graph.converters import to_str
from src.graph.golden_profile import recompute_golden_profile_tx
from src.graph.queries import (
    CHECK_SOURCE_RECORD_LINKED,
    GET_FACT_VALUE,
    GET_PERSON_OVERRIDES_FULL,
    UPDATE_GOLDEN_FIELD,
    UPDATE_OVERRIDES,
)


def _parse_overrides(raw: object) -> dict[str, dict[str, str]]:
    if not isinstance(raw, dict):
        return {}
    return {
        to_str(k): {to_str(ik): to_str(iv) for ik, iv in v.items()}
        for k, v in raw.items()
        if isinstance(v, dict)
    }


def _fact_value_to_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


class Neo4jSurvivorshipRepository:
    async def recompute_golden_profile(self, person_id: str) -> float | None:
        async with get_session(write=True) as session:
            return await session.execute_write(recompute_golden_profile_tx, person_id)

    async def create_override(
        self,
        person_id: str,
        attribute_name: str,
        source_record_pk: str,
        reason: str,
        actor_id: str,
    ) -> str:
        async with get_session(write=True) as session:
            return await session.execute_write(
                _override_tx,
                person_id,
                attribute_name,
                source_record_pk,
                reason,
                actor_id,
            )


async def _override_tx(
    tx: AsyncManagedTransaction,
    person_id: str,
    attribute_name: str,
    source_record_pk: str,
    reason: str,
    actor_id: str,
) -> str:
    person_record = await (await tx.run(GET_PERSON_OVERRIDES_FULL, person_id=person_id)).single()
    if person_record is None:
        return "person_not_found"

    if (
        await (
            await tx.run(
                CHECK_SOURCE_RECORD_LINKED,
                source_record_pk=source_record_pk,
                person_id=person_id,
            )
        ).single()
        is None
    ):
        return "sr_not_found"

    bare_attr = attribute_name.removeprefix("preferred_")
    fact_record = await (
        await tx.run(
            GET_FACT_VALUE,
            person_id=person_id,
            attribute_name=bare_attr,
            source_record_pk=source_record_pk,
        )
    ).single()
    if fact_record is None:
        return "fact_not_found"

    overrides = _parse_overrides(person_record["overrides"])
    overrides[attribute_name] = {
        "source_record_pk": source_record_pk,
        "reason": reason,
        "actor_type": "admin",
        "actor_id": actor_id,
        "created_at": datetime.now(UTC).isoformat(),
    }
    await tx.run(UPDATE_OVERRIDES, person_id=person_id, overrides=overrides)

    field_name = (
        attribute_name if attribute_name.startswith("preferred_") else f"preferred_{attribute_name}"
    )
    selected_value = _fact_value_to_str(fact_record["value"])
    await tx.run(
        UPDATE_GOLDEN_FIELD,
        person_id=person_id,
        field_name=field_name,
        value=selected_value,
    )
    return "ok"
