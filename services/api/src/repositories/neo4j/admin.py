"""Neo4j implementation of AdminRepository."""

from __future__ import annotations

from neo4j import AsyncManagedTransaction

from src.graph.client import get_session
from src.graph.converters import to_optional_str, to_str
from src.graph.queries import GET_FIELD_TRUST, LIST_SOURCE_SYSTEMS, UPDATE_FIELD_TRUST
from src.repositories.protocols.admin import FieldTrustResponse, SourceSystemInfo
from src.types import TrustTier


class Neo4jAdminRepository:
    async def get_all_source_systems(self) -> list[SourceSystemInfo]:
        async with get_session() as session:
            result = await session.run(LIST_SOURCE_SYSTEMS)
            systems: list[SourceSystemInfo] = []
            async for record in result:
                ss = record["source_system"]
                if not isinstance(ss, dict):
                    continue
                field_trust_raw = ss.get("field_trust")
                field_trust: dict[str, str] = {}
                if isinstance(field_trust_raw, dict):
                    field_trust = {to_str(k): to_str(v) for k, v in field_trust_raw.items()}
                systems.append(
                    SourceSystemInfo(
                        source_system_id=to_optional_str(ss.get("source_system_id")),
                        source_key=to_str(ss.get("source_key")),
                        display_name=to_optional_str(ss.get("display_name")),
                        system_type=to_optional_str(ss.get("system_type")),
                        is_active=bool(ss.get("is_active")),
                        field_trust=field_trust,
                        entity_key=to_optional_str(record["entity_key"]),
                        created_at=to_optional_str(ss.get("created_at")),
                        updated_at=to_optional_str(ss.get("updated_at")),
                    )
                )
        return systems

    async def get_field_trust(self, source_key: str) -> FieldTrustResponse | None:
        async with get_session() as session:
            result = await session.run(GET_FIELD_TRUST, source_key=source_key)
            record = await result.single()
        if record is None:
            return None
        field_trust_raw = record["field_trust"]
        field_trust: dict[str, str] = {}
        if isinstance(field_trust_raw, dict):
            field_trust = {to_str(k): to_str(v) for k, v in field_trust_raw.items()}
        return FieldTrustResponse(
            source_key=to_str(record["source_key"]),
            display_name=to_optional_str(record["display_name"]),
            field_trust=field_trust,
        )

    async def update_field_trust(
        self,
        source_key: str,
        updates: dict[str, TrustTier],
    ) -> dict[str, str] | None:
        async with get_session(write=True) as session:
            return await session.execute_write(_update_trust_tx, source_key, updates)


async def _update_trust_tx(
    tx: AsyncManagedTransaction, source_key: str, updates: dict[str, TrustTier]
) -> dict[str, str] | None:
    current = await tx.run(GET_FIELD_TRUST, source_key=source_key)
    record = await current.single()
    if record is None:
        return None
    existing_raw = record["field_trust"]
    existing: dict[str, str] = {}
    if isinstance(existing_raw, dict):
        existing = {to_str(k): to_str(v) for k, v in existing_raw.items()}
    merged: dict[str, str] = {**existing, **{k: v.value for k, v in updates.items()}}
    await tx.run(UPDATE_FIELD_TRUST, source_key=source_key, field_trust=merged)
    return merged
