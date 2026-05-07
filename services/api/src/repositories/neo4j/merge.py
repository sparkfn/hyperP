"""Neo4j implementation of MergeRepository."""

from __future__ import annotations

from neo4j import AsyncManagedTransaction

from src.graph.client import get_session
from src.graph.converters import to_str
from src.graph.queries import (
    CHECK_BOTH_PERSONS_ACTIVE,
    CHECK_EXISTING_LOCK,
    CHECK_NO_MATCH_LOCK,
    CREATE_PERSON_PAIR_LOCK,
    CREATE_UNMERGE_AUDIT,
    DELETE_LOCK,
    EXECUTE_MANUAL_MERGE,
    FLAG_AFFECTED_RECORDS_FOR_REVIEW,
    GET_UNMERGE_TARGET,
    REVERT_MERGE,
)
from src.repositories.protocols.merge import MergeOutcome


def _ordered_pair(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left < right else (right, left)


class Neo4jMergeRepository:
    async def manual_merge(
        self, from_id: str, to_id: str, reason: str, actor_id: str
    ) -> MergeOutcome:
        async with get_session(write=True) as session:
            return await session.execute_write(_manual_merge_tx, from_id, to_id, reason, actor_id)

    async def unmerge(
        self, merge_event_id: str, reason: str, actor_id: str
    ) -> tuple[str, str] | None:
        async with get_session(write=True) as session:
            return await session.execute_write(_unmerge_tx, merge_event_id, reason, actor_id)

    async def create_lock(
        self,
        left: str,
        right: str,
        lock_type: str,
        reason: str,
        expires_at: str | None,
        actor_id: str,
    ) -> tuple[str, str | None]:
        async with get_session(write=True) as session:
            return await session.execute_write(
                _create_lock_tx, left, right, lock_type, reason, expires_at, actor_id
            )

    async def delete_lock(self, lock_id: str) -> bool:
        async with get_session(write=True) as session:
            return await session.execute_write(_delete_lock_tx, lock_id)


async def _manual_merge_tx(
    tx: AsyncManagedTransaction, from_id: str, to_id: str, reason: str, actor_id: str
) -> MergeOutcome:
    left, right = _ordered_pair(from_id, to_id)
    lock_result = await tx.run(CHECK_NO_MATCH_LOCK, left=left, right=right)
    lock_record = await lock_result.single()
    if lock_record is not None and bool(lock_record["is_locked"]):
        return MergeOutcome(blocked=True)

    person_result = await tx.run(CHECK_BOTH_PERSONS_ACTIVE, from_id=from_id, to_id=to_id)
    if await person_result.single() is None:
        return MergeOutcome(not_found=True)

    merge_result = await tx.run(
        EXECUTE_MANUAL_MERGE,
        from_id=from_id,
        to_id=to_id,
        reason=reason,
        actor_id=actor_id,
    )
    record = await merge_result.single()
    if record is None:
        return MergeOutcome(not_found=True)
    return MergeOutcome(merge_event_id=to_str(record["merge_event_id"]))


async def _unmerge_tx(
    tx: AsyncManagedTransaction, merge_event_id: str, reason: str, actor_id: str
) -> tuple[str, str] | None:
    target_result = await tx.run(GET_UNMERGE_TARGET, merge_event_id=merge_event_id)
    target = await target_result.single()
    if target is None:
        return None
    absorbed_id = to_str(target["absorbed_id"])
    survivor_id = to_str(target["survivor_id"])

    await tx.run(REVERT_MERGE, absorbed_id=absorbed_id, survivor_id=survivor_id)
    await tx.run(
        CREATE_UNMERGE_AUDIT,
        absorbed_id=absorbed_id,
        survivor_id=survivor_id,
        reason=reason,
        original_merge_event_id=merge_event_id,
        actor_id=actor_id,
    )
    await tx.run(FLAG_AFFECTED_RECORDS_FOR_REVIEW, merge_event_id=merge_event_id)
    return absorbed_id, survivor_id


async def _create_lock_tx(
    tx: AsyncManagedTransaction,
    left: str,
    right: str,
    lock_type: str,
    reason: str,
    expires_at: str | None,
    actor_id: str,
) -> tuple[str, str | None]:
    existing = await tx.run(CHECK_EXISTING_LOCK, left=left, right=right)
    existing_record = await existing.single()
    if existing_record is not None:
        return "conflict", to_str(existing_record["lock_id"])

    result = await tx.run(
        CREATE_PERSON_PAIR_LOCK,
        left=left,
        right=right,
        lock_type=lock_type,
        reason=reason,
        expires_at=expires_at,
        actor_id=actor_id,
    )
    record = await result.single()
    if record is None:
        return "not_found", None
    return "ok", to_str(record["lock_id"])


async def _delete_lock_tx(tx: AsyncManagedTransaction, lock_id: str) -> bool:
    result = await tx.run(DELETE_LOCK, lock_id=lock_id)
    record = await result.single()
    return record is not None
