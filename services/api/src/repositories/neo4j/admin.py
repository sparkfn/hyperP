"""Neo4j implementation of AdminRepository."""

from __future__ import annotations

from neo4j import AsyncManagedTransaction

from src.graph.client import get_session
from src.graph.converters import to_optional_str, to_str, to_str_dict
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
                latest_run = record["latest_run"]
                latest = latest_run if isinstance(latest_run, dict) else {}
                latest_failure = record["latest_failure"]
                failure = latest_failure if isinstance(latest_failure, dict) else {}
                field_trust = to_str_dict(ss.get("field_trust"))
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
                        latest_run_id=to_optional_str(latest.get("ingest_run_id")),
                        latest_run_status=to_optional_str(latest.get("status")),
                        latest_run_started_at=to_optional_str(latest.get("started_at")),
                        latest_run_finished_at=to_optional_str(latest.get("finished_at")),
                        latest_failure_category=to_optional_str(failure.get("failure_category")),
                        latest_failure_exception_class=to_optional_str(
                            failure.get("failure_exception_class")
                        ),
                        latest_failure_message=to_optional_str(failure.get("failure_message")),
                        latest_failure_mode=to_optional_str(failure.get("failure_mode")),
                        latest_failure_started_at=to_optional_str(failure.get("started_at")),
                        latest_failure_task_id=to_optional_str(failure.get("failure_task_id")),
                        latest_failure_checkpoint=to_optional_str(
                            failure.get("failure_checkpoint")
                        ),
                    )
                )
        return systems

    async def get_field_trust(self, source_key: str) -> FieldTrustResponse | None:
        async with get_session() as session:
            result = await session.run(GET_FIELD_TRUST, source_key=source_key)
            record = await result.single()
        if record is None:
            return None
        return FieldTrustResponse(
            source_key=to_str(record["source_key"]),
            display_name=to_optional_str(record["display_name"]),
            field_trust=to_str_dict(record["field_trust"]),
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
    existing: dict[str, str] = to_str_dict(record["field_trust"])
    merged: dict[str, str] = {**existing, **{k: v.value for k, v in updates.items()}}
    await tx.run(UPDATE_FIELD_TRUST, source_key=source_key, field_trust=merged)
    return merged
