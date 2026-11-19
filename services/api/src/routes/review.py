"""Review queue endpoints: list, fetch, assign, submit action."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from src.auth.deps import require_human_user, require_mutator_for_review_case
from src.auth.models import AuthUser
from src.graph.queries import REVIEW_SORT_KEYS
from src.http_utils import envelope, http_error, next_cursor, page_window
from src.repositories.deps import get_review_repo
from src.repositories.protocols.review import ReviewListFilters, ReviewRepository
from src.types import ApiResponse, ApiReviewActionType, ReviewCaseDetail, ReviewCaseSummary
from src.types_requests import (
    AssignReviewRequest,
    BulkReviewActionRequest,
    ReviewActionRequest,
)

router = APIRouter(prefix="/v1/review-cases", tags=["Review"])

# Upper bound on the number of review cases one bulk request may touch.
MAX_BULK_REVIEW_CASE_IDS = 100


class AssignResponse(BaseModel):
    review_case_id: str
    queue_state: str
    assigned_to: str


class ActionResponse(BaseModel):
    review_case_id: str
    queue_state: str
    resolution: str | None = None


class BulkActionItemResponse(BaseModel):
    review_case_id: str
    status: Literal["updated", "error"]
    queue_state: str | None = None
    resolution: str | None = None
    error_code: str | None = None


class BulkActionResponse(BaseModel):
    items: list[BulkActionItemResponse]


def _resolve_action(action_type: str) -> tuple[str, str | None]:
    """Map an API action type to (new_queue_state, resolution)."""
    if action_type == ApiReviewActionType.MERGE.value:
        return "resolved", "merge"
    if action_type == ApiReviewActionType.REJECT.value:
        return "resolved", "reject"
    if action_type == ApiReviewActionType.MANUAL_NO_MATCH.value:
        return "resolved", "manual_no_match"
    if action_type == ApiReviewActionType.RESOLVE.value:
        return "resolved", "resolve"
    if action_type == ApiReviewActionType.DEFER.value:
        return "deferred", None
    if action_type == ApiReviewActionType.ESCALATE.value:
        return "assigned", None
    if action_type == ApiReviewActionType.EXPIRE.value:
        return "expired", "expired"
    if action_type == ApiReviewActionType.DISMISS.value:
        return "dismissed", "dismissed"
    return "open", None


@router.get("", response_model=ApiResponse[list[ReviewCaseSummary]])
async def list_review_cases(
    request: Request,
    queue_state: str | None = Query(default=None),
    assigned_to: str | None = Query(default=None),
    person_id: str | None = Query(default=None),
    priority_lte: int | None = Query(default=None),
    priority_gte: int | None = Query(default=None),
    decision: str | None = Query(default=None),
    engine_type: str | None = Query(default=None),
    confidence_gte: float | None = Query(default=None),
    confidence_lte: float | None = Query(default=None),
    created_after: str | None = Query(default=None),
    created_before: str | None = Query(default=None),
    sla_due_after: str | None = Query(default=None),
    sla_due_before: str | None = Query(default=None),
    overdue_sla: bool | None = Query(default=None),
    resolved: bool | None = Query(default=None),
    q: str | None = Query(default=None),
    sort_by: str | None = Query(default=None),
    sort_order: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    _user: AuthUser = Depends(require_human_user),
    repo: ReviewRepository = Depends(get_review_repo),
) -> ApiResponse[list[ReviewCaseSummary]]:
    """List review cases with optional search, filters, sort, and pagination."""
    q_clean: str | None = q.strip() if q else None
    if q_clean is not None and len(q_clean) < 3:
        raise http_error(
            400,
            "invalid_request",
            "Search query q requires at least 3 characters.",
            request,
        )
    if sort_by is not None and sort_by not in REVIEW_SORT_KEYS:
        raise http_error(400, "invalid_request", f"Unknown sort_by: {sort_by}", request)

    skip, page_limit = page_window(cursor, limit)
    filters: ReviewListFilters = {
        "queue_state": queue_state,
        "assigned_to": assigned_to,
        "person_id": person_id,
        "priority_lte": priority_lte,
        "priority_gte": priority_gte,
        "decision": decision,
        "engine_type": engine_type,
        "confidence_gte": confidence_gte,
        "confidence_lte": confidence_lte,
        "created_after": created_after,
        "created_before": created_before,
        "sla_due_after": sla_due_after,
        "sla_due_before": sla_due_before,
        "overdue_sla": overdue_sla,
        "resolved": resolved,
        "q": q_clean,
        "sort_by": sort_by,
        "sort_order": sort_order,
    }
    items, total = await repo.get_page(filters, skip, page_limit)
    has_more = skip + page_limit < total
    return envelope(items, request, next_cursor(skip, page_limit, has_more), total_count=total)


@router.get("/by-match-decision/{match_decision_id}", response_model=ApiResponse[ReviewCaseDetail])
async def get_review_case_by_match_decision(
    match_decision_id: str,
    request: Request,
    _user: AuthUser = Depends(require_human_user),
    repo: ReviewRepository = Depends(get_review_repo),
) -> ApiResponse[ReviewCaseDetail]:
    """Return the review case attached to a match decision."""
    case = await repo.get_by_match_decision_id(match_decision_id)
    if case is None:
        raise http_error(404, "review_case_not_found", "Review case was not found.", request)
    return envelope(case, request)


@router.get("/{review_case_id}", response_model=ApiResponse[ReviewCaseDetail])
async def get_review_case(
    review_case_id: str,
    request: Request,
    _user: AuthUser = Depends(require_human_user),
    repo: ReviewRepository = Depends(get_review_repo),
) -> ApiResponse[ReviewCaseDetail]:
    """Return a single review case with comparison payload."""
    case = await repo.get_by_id(review_case_id)
    if case is None:
        raise http_error(404, "review_case_not_found", "Review case was not found.", request)
    return envelope(case, request)


async def require_human_review_case_mutator(
    user: AuthUser = Depends(require_human_user),
    review_user: AuthUser = Depends(require_mutator_for_review_case),
) -> AuthUser:
    """Require both a human principal and review-case mutation permission."""
    _ = user
    return review_user


@router.post("/{review_case_id}/recreate", response_model=ApiResponse[ReviewCaseDetail])
async def recreate_review_case(
    review_case_id: str,
    request: Request,
    user: AuthUser = Depends(require_human_review_case_mutator),
    repo: ReviewRepository = Depends(get_review_repo),
) -> ApiResponse[ReviewCaseDetail]:
    """Create a new open review case from a resolved case without mutating history."""
    case = await repo.recreate(review_case_id, user.email)
    if case is None:
        raise http_error(404, "review_case_not_found", "Review case was not found.", request)
    return envelope(case, request)


@router.post("/{review_case_id}/assign", response_model=ApiResponse[AssignResponse])
async def assign_review_case(
    review_case_id: str,
    body: AssignReviewRequest,
    request: Request,
    _user: AuthUser = Depends(require_human_review_case_mutator),
    repo: ReviewRepository = Depends(get_review_repo),
) -> ApiResponse[AssignResponse]:
    """Assign a review case to a reviewer."""
    result = await repo.assign(review_case_id, body.assigned_to)
    if result is None:
        raise http_error(
            404,
            "review_case_not_found",
            "Review case was not found or is not assignable.",
            request,
        )
    return envelope(
        AssignResponse(
            review_case_id=result["review_case_id"],
            queue_state=result["queue_state"],
            assigned_to=result["assigned_to"],
        ),
        request,
    )


@router.post("/{review_case_id}/actions", response_model=ApiResponse[ActionResponse])
async def submit_review_action(
    review_case_id: str,
    body: ReviewActionRequest,
    request: Request,
    user: AuthUser = Depends(require_human_review_case_mutator),
    repo: ReviewRepository = Depends(get_review_repo),
) -> ApiResponse[ActionResponse]:
    """Submit a review action (merge / reject / defer / escalate / manual_no_match / resolve)."""
    new_state, resolution = _resolve_action(body.action_type.value)
    result = await repo.submit_action(
        review_case_id,
        body.action_type.value,
        new_state,
        resolution,
        body.notes,
        body.metadata.follow_up_at,
        user.email,
        body.metadata.survivor_person_id,
        [selection.to_selection() for selection in body.metadata.golden_profile_selections],
        reason_code=body.reason_code.value if body.reason_code is not None else None,
    )

    if result is None:
        raise http_error(
            404, "review_case_not_found", "Review case not found or not actionable.", request
        )
    if result.get("merge_blocked"):
        raise http_error(
            409, "merge_blocked", "A no-match lock exists between these persons.", request
        )
    if result.get("pair_audit_merge_disabled"):
        raise http_error(
            422,
            "pair_audit_merge_disabled",
            "Merge is disabled for pair-audit review cases.",
            request,
        )
    if result.get("merge_not_applicable"):
        raise http_error(
            422,
            "merge_not_applicable",
            "Review case does not link two active persons; cannot execute merge.",
            request,
        )
    if result.get("resolve_not_applicable"):
        raise http_error(
            422,
            "resolve_not_applicable",
            "Review case does not link two persons without source-record evidence; "
            "cannot execute resolve.",
            request,
        )

    return envelope(
        ActionResponse(
            review_case_id=result.get("review_case_id", ""),
            queue_state=result.get("queue_state", ""),
            resolution=resolution,
        ),
        request,
    )


@router.post(
    "/bulk-actions",
    response_model=ApiResponse[BulkActionResponse],
    operation_id="submit_bulk_review_action",
)
async def submit_bulk_review_action(
    body: BulkReviewActionRequest,
    request: Request,
    user: AuthUser = Depends(require_human_user),
    repo: ReviewRepository = Depends(get_review_repo),
) -> ApiResponse[BulkActionResponse]:
    """Apply one review action to many review cases, reporting per-item outcomes."""
    if not 1 <= len(body.review_case_ids) <= MAX_BULK_REVIEW_CASE_IDS:
        raise http_error(
            400,
            "invalid_request",
            "review_case_ids must contain between 1 and "
            f"{MAX_BULK_REVIEW_CASE_IDS} review case ids.",
            request,
        )
    # Authorize every requested case before any mutation, so a batch that
    # crosses an entity boundary is rejected without writing anything.
    for review_case_id in body.review_case_ids:
        await require_mutator_for_review_case(review_case_id, request, user)

    new_state, resolution = _resolve_action(body.action_type.value)
    outcome = await repo.submit_bulk_action(
        list(body.review_case_ids),
        body.action_type.value,
        new_state,
        resolution,
        body.notes,
        body.follow_up_at,
        user.email,
        body.reason_code.value,
    )
    items = [
        BulkActionItemResponse(
            review_case_id=item["review_case_id"],
            status=item["status"],
            queue_state=item["queue_state"],
            resolution=item["resolution"],
            error_code=item["error_code"],
        )
        for item in outcome["items"]
    ]
    return envelope(BulkActionResponse(items=items), request)
