"""Manual merge, unmerge, and person-pair lock endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from src.auth.deps import require_admin
from src.auth.models import AuthUser
from src.http_utils import envelope, http_error
from src.repositories.deps import get_merge_repo
from src.repositories.protocols.merge import MergeRepository
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


def _ordered_pair(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left < right else (right, left)


@router.post("/v1/persons/manual-merge", response_model=ApiResponse[ManualMergeResponse])
async def manual_merge(
    body: ManualMergeRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
    repo: MergeRepository = Depends(get_merge_repo),
) -> ApiResponse[ManualMergeResponse]:
    """Manually merge two canonical persons inside a single transaction."""
    outcome = await repo.manual_merge(
        body.from_person_id, body.to_person_id, body.reason, user.email
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


@router.post("/v1/persons/unmerge", response_model=ApiResponse[UnmergeResponse])
async def unmerge(
    body: UnmergeRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
    repo: MergeRepository = Depends(get_merge_repo),
) -> ApiResponse[UnmergeResponse]:
    """Undo a prior merge event."""
    result = await repo.unmerge(body.merge_event_id, body.reason, user.email)
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


@router.post("/v1/locks/person-pair", response_model=ApiResponse[LockResponse], status_code=201)
async def create_person_pair_lock(
    body: LockRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
    repo: MergeRepository = Depends(get_merge_repo),
) -> ApiResponse[LockResponse]:
    """Create a persistent lock to prevent repeated merge suggestions."""
    left, right = _ordered_pair(body.left_person_id, body.right_person_id)
    status, lock_id = await repo.create_lock(
        left, right, body.lock_type, body.reason, body.expires_at, user.email
    )

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


@router.delete("/v1/locks/{lock_id}", response_model=ApiResponse[LockDeletedResponse])
async def delete_lock(
    lock_id: str,
    request: Request,
    _user: AuthUser = Depends(require_admin),
    repo: MergeRepository = Depends(get_merge_repo),
) -> ApiResponse[LockDeletedResponse]:
    """Remove an existing person-pair lock."""
    deleted = await repo.delete_lock(lock_id)
    if not deleted:
        raise http_error(404, "not_found", "Lock not found.", request)
    return envelope(LockDeletedResponse(lock_id=lock_id, status="deleted"), request)
