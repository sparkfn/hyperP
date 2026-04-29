"""Review queue endpoints: list, fetch, assign, submit action."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from src.auth.deps import require_mutator_for_review_case
from src.auth.models import AuthUser
from src.http_utils import envelope, http_error, next_cursor, page_window
from src.repositories.deps import get_review_repo
from src.repositories.protocols.review import ReviewListFilters, ReviewRepository
from src.types import ApiResponse, ApiReviewActionType, ReviewCaseDetail, ReviewCaseSummary
from src.types_requests import AssignReviewRequest, ReviewActionRequest

router = APIRouter(prefix="/v1/review-cases")


class AssignResponse(BaseModel):
    review_case_id: str
    queue_state: str
    assigned_to: str


class ActionResponse(BaseModel):
    review_case_id: str
    queue_state: str
    resolution: str | None = None


def _resolve_action(action_type: ApiReviewActionType) -> tuple[str, str | None]:
    """Map an API action type to (new_queue_state, resolution)."""
    if action_type is ApiReviewActionType.MERGE:
        return "resolved", "merge"
    if action_type is ApiReviewActionType.REJECT:
        return "resolved", "reject"
    if action_type is ApiReviewActionType.MANUAL_NO_MATCH:
        return "resolved", "manual_no_match"
    if action_type is ApiReviewActionType.DEFER:
        return "deferred", None
    if action_type is ApiReviewActionType.ESCALATE:
        return "assigned", None
    return "open", None


@router.get("", response_model=ApiResponse[list[ReviewCaseSummary]])
async def list_review_cases(
    request: Request,
    queue_state: str | None = Query(default=None),
    assigned_to: str | None = Query(default=None),
    priority_lte: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    repo: ReviewRepository = Depends(get_review_repo),
) -> ApiResponse[list[ReviewCaseSummary]]:
    """List review cases with optional filters."""
    skip, page_limit = page_window(cursor, limit)
    filters: ReviewListFilters = {
        "queue_state": queue_state,
        "assigned_to": assigned_to,
        "priority_lte": priority_lte,
    }
    items, has_more = await repo.get_page(filters, skip, page_limit)
    return envelope(items, request, next_cursor(skip, page_limit, has_more))


@router.get("/{review_case_id}", response_model=ApiResponse[ReviewCaseDetail])
async def get_review_case(
    review_case_id: str,
    request: Request,
    repo: ReviewRepository = Depends(get_review_repo),
) -> ApiResponse[ReviewCaseDetail]:
    """Return a single review case with comparison payload."""
    case = await repo.get_by_id(review_case_id)
    if case is None:
        raise http_error(404, "review_case_not_found", "Review case was not found.", request)
    return envelope(case, request)


@router.post("/{review_case_id}/assign", response_model=ApiResponse[AssignResponse])
async def assign_review_case(
    review_case_id: str,
    body: AssignReviewRequest,
    request: Request,
    _user: AuthUser = Depends(require_mutator_for_review_case),
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
    user: AuthUser = Depends(require_mutator_for_review_case),
    repo: ReviewRepository = Depends(get_review_repo),
) -> ApiResponse[ActionResponse]:
    """Submit a review action (merge / reject / defer / escalate / manual_no_match)."""
    new_state, resolution = _resolve_action(body.action_type)
    result = await repo.submit_action(
        review_case_id,
        body.action_type.value,
        new_state,
        resolution,
        body.notes,
        body.metadata.follow_up_at,
        user.email,
        body.metadata.survivor_person_id,
    )

    if result is None:
        raise http_error(
            404, "review_case_not_found", "Review case not found or not actionable.", request
        )
    if result.get("merge_blocked"):
        raise http_error(
            409, "merge_blocked", "A no-match lock exists between these persons.", request
        )
    if result.get("merge_not_applicable"):
        raise http_error(
            422,
            "merge_not_applicable",
            "Review case does not link two active persons; cannot execute merge.",
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
