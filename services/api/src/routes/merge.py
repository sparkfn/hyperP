"""Manual merge, unmerge, and person-pair lock endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from neo4j import AsyncManagedTransaction
from pydantic import BaseModel

from src.auth.deps import require_admin
from src.auth.models import AuthUser
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
from src.http_utils import envelope, http_error
from src.types import ApiResponse
from src.types_requests import LockRequest, ManualMergeRequest, UnmergeRequest

router = APIRouter()


class ManualMergeResponse(BaseModel):
    merge_event_id: str
    from_person_id: str
    to_person_id: str
    status: str


class UnmergeResponse(BaseModel):
    merge_event_id: str
    absorbed_person_id: str
    survivor_person_id: str
    status: str


class LockResponse(BaseModel):
    lock_id: str
    left_person_id: str
    right_person_id: str
    lock_type: str


class LockDeletedResponse(BaseModel):
    lock_id: str
    status: str


class _MergeOutcome(BaseModel):
    blocked: bool = False
    not_found: bool = False
    merge_event_id: str | None = None


def _ordered_pair(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left < right else (right, left)


@router.post("/v1/persons/manual-merge", response_model=ApiResponse[ManualMergeResponse])
async def manual_merge(
    body: ManualMergeRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> ApiResponse[ManualMergeResponse]:
    """Manually merge two canonical persons inside a single Neo4j transaction."""
    async with get_session(write=True) as session:
        outcome = await session.execute_write(
            _manual_merge_tx,
            body.from_person_id,
            body.to_person_id,
            body.reason,
            user.email,
        )

    if outcome.blocked:
        raise http_error(
            409, "merge_blocked", "A no-match lock exists between these persons.", request
        )
    if outcome.not_found or outcome.merge_event_id is None:
        raise http_error(
            404, "person_not_found", "One or both persons not found or not active.", request
        )

    return envelope(
        ManualMergeResponse(
            merge_event_id=outcome.merge_event_id,
            from_person_id=body.from_person_id,
            to_person_id=body.to_person_id,
            status="completed",
        ),
        request,
    )


async def _manual_merge_tx(
    tx: AsyncManagedTransaction, from_id: str, to_id: str, reason: str, actor_id: str
) -> _MergeOutcome:
    left, right = _ordered_pair(from_id, to_id)
    lock_result = await tx.run(CHECK_NO_MATCH_LOCK, left=left, right=right)
    lock_record = await lock_result.single()
    if lock_record is not None and bool(lock_record["is_locked"]):
        return _MergeOutcome(blocked=True)

    person_result = await tx.run(CHECK_BOTH_PERSONS_ACTIVE, from_id=from_id, to_id=to_id)
    if await person_result.single() is None:
        return _MergeOutcome(not_found=True)

    merge_result = await tx.run(
        EXECUTE_MANUAL_MERGE,
        from_id=from_id,
        to_id=to_id,
        reason=reason,
        actor_id=actor_id,
    )
    record = await merge_result.single()
    if record is None:
        return _MergeOutcome(not_found=True)
    return _MergeOutcome(merge_event_id=to_str(record["merge_event_id"]))


@router.post("/v1/persons/unmerge", response_model=ApiResponse[UnmergeResponse])
async def unmerge(
    body: UnmergeRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> ApiResponse[UnmergeResponse]:
    """Undo a prior merge event."""
    async with get_session(write=True) as session:
        result = await session.execute_write(
            _unmerge_tx, body.merge_event_id, body.reason, user.email
        )
    if result is None:
        raise http_error(404, "not_found", "Merge event not found or already unmerged.", request)
    absorbed_id, survivor_id = result
    return envelope(
        UnmergeResponse(
            merge_event_id=body.merge_event_id,
            absorbed_person_id=absorbed_id,
            survivor_person_id=survivor_id,
            status="unmerged",
        ),
        request,
    )


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


@router.post("/v1/locks/person-pair", response_model=ApiResponse[LockResponse], status_code=201)
async def create_person_pair_lock(
    body: LockRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> ApiResponse[LockResponse]:
    """Create a persistent lock to prevent repeated merge suggestions."""
    left, right = _ordered_pair(body.left_person_id, body.right_person_id)
    async with get_session(write=True) as session:
        outcome = await session.execute_write(
            _create_lock_tx,
            left,
            right,
            body.lock_type,
            body.reason,
            body.expires_at,
            user.email,
        )

    status, lock_id = outcome
    if status == "conflict":
        raise http_error(
            409,
            "manual_lock_conflict",
            "An active lock already exists between these persons.",
            request,
            details={"existing_lock_id": lock_id or ""},
        )
    if status == "not_found" or lock_id is None:
        raise http_error(404, "person_not_found", "One or both persons not found.", request)

    return envelope(
        LockResponse(
            lock_id=lock_id,
            left_person_id=left,
            right_person_id=right,
            lock_type=body.lock_type,
        ),
        request,
    )


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


@router.delete("/v1/locks/{lock_id}", response_model=ApiResponse[LockDeletedResponse])
async def delete_lock(
    lock_id: str,
    request: Request,
    _user: AuthUser = Depends(require_admin),
) -> ApiResponse[LockDeletedResponse]:
    """Remove an existing person-pair lock."""
    async with get_session(write=True) as session:
        deleted = await session.execute_write(_delete_lock_tx, lock_id)
    if not deleted:
        raise http_error(404, "not_found", "Lock not found.", request)
    return envelope(LockDeletedResponse(lock_id=lock_id, status="deleted"), request)


async def _delete_lock_tx(tx: AsyncManagedTransaction, lock_id: str) -> bool:
    result = await tx.run(DELETE_LOCK, lock_id=lock_id)
    record = await result.single()
    return record is not None
