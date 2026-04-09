"""Review queue endpoints: list, fetch, assign, submit action."""

from __future__ import annotations

from typing import LiteralString

from fastapi import APIRouter, Query, Request
from neo4j import AsyncManagedTransaction
from pydantic import BaseModel

from src.graph.client import get_session
from src.graph.converters import GraphRecord, GraphValue, to_str
from src.graph.mappers import map_review_case_detail, map_review_case_summary
from src.graph.queries import (
    ASSIGN_REVIEW_CASE,
    CREATE_NO_MATCH_LOCK_FROM_REVIEW,
    GET_REVIEW_CASE,
    LIST_REVIEW_CASES,
)
from src.http_utils import envelope, http_error, next_cursor, page_window
from src.types import (
    ApiResponse,
    ApiReviewActionType,
    AssignReviewRequest,
    ReviewActionRequest,
    ReviewCaseDetail,
    ReviewCaseSummary,
)

router = APIRouter(prefix="/v1/review-cases")


class AssignResponse(BaseModel):
    review_case_id: str
    queue_state: str
    assigned_to: str


class ActionResponse(BaseModel):
    review_case_id: str
    queue_state: str
    resolution: str | None = None


def _record_to_dict(keys: list[str], values: list[GraphValue]) -> GraphRecord:
    return dict(zip(keys, values, strict=True))


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
) -> ApiResponse[list[ReviewCaseSummary]]:
    """List review cases with optional filters."""
    skip, page_limit = page_window(cursor, limit)
    async with get_session() as session:
        result = await session.run(
            LIST_REVIEW_CASES,
            queue_state=queue_state,
            assigned_to=assigned_to,
            priority_lte=priority_lte,
            skip=skip,
            limit=page_limit + 1,
        )
        records = [_record_to_dict(r.keys(), list(r.values())) async for r in result]
    has_more = len(records) > page_limit
    items = [map_review_case_summary(rec) for rec in records[:page_limit]]
    return envelope(items, request, next_cursor(skip, page_limit, has_more))


@router.get("/{review_case_id}", response_model=ApiResponse[ReviewCaseDetail])
async def get_review_case(
    review_case_id: str, request: Request
) -> ApiResponse[ReviewCaseDetail]:
    """Return a single review case with comparison payload."""
    async with get_session() as session:
        result = await session.run(GET_REVIEW_CASE, review_case_id=review_case_id)
        record = await result.single()
    if record is None:
        raise http_error(404, "review_case_not_found", "Review case was not found.", request)
    return envelope(
        map_review_case_detail(_record_to_dict(record.keys(), list(record.values()))), request
    )


@router.post("/{review_case_id}/assign", response_model=ApiResponse[AssignResponse])
async def assign_review_case(
    review_case_id: str, body: AssignReviewRequest, request: Request
) -> ApiResponse[AssignResponse]:
    """Assign a review case to a reviewer."""
    async with get_session(write=True) as session:
        record = await session.execute_write(_assign_tx, review_case_id, body.assigned_to)
    if record is None:
        raise http_error(
            404,
            "review_case_not_found",
            "Review case was not found or is not assignable.",
            request,
        )
    return envelope(
        AssignResponse(
            review_case_id=to_str(record.get("review_case_id")),
            queue_state=to_str(record.get("queue_state")),
            assigned_to=to_str(record.get("assigned_to")),
        ),
        request,
    )


async def _assign_tx(
    tx: AsyncManagedTransaction, review_case_id: str, assigned_to: str
) -> GraphRecord | None:
    result = await tx.run(
        ASSIGN_REVIEW_CASE, review_case_id=review_case_id, assigned_to=assigned_to
    )
    record = await result.single()
    if record is None:
        return None
    return dict(record["review_case"])


@router.post("/{review_case_id}/actions", response_model=ApiResponse[ActionResponse])
async def submit_review_action(
    review_case_id: str, body: ReviewActionRequest, request: Request
) -> ApiResponse[ActionResponse]:
    """Submit a review action (merge / reject / defer / escalate / manual_no_match)."""
    new_state, resolution = _resolve_action(body.action_type)
    async with get_session(write=True) as session:
        record = await session.execute_write(
            _action_tx,
            review_case_id,
            body.action_type.value,
            new_state,
            resolution,
            body.notes,
            body.metadata.follow_up_at,
        )
    if record is None:
        raise http_error(
            404,
            "review_case_not_found",
            "Review case was not found or is not actionable.",
            request,
        )
    return envelope(
        ActionResponse(
            review_case_id=to_str(record.get("review_case_id")),
            queue_state=to_str(record.get("queue_state")),
            resolution=resolution,
        ),
        request,
    )


async def _action_tx(
    tx: AsyncManagedTransaction,
    review_case_id: str,
    action_type: str,
    new_state: str,
    resolution: str | None,
    notes: str | None,
    follow_up_at: str | None,
) -> GraphRecord | None:
    set_clauses: list[LiteralString] = [
        "rc.queue_state = $new_state",
        "rc.updated_at = datetime()",
        "rc.actions = rc.actions + [{"
        " action_type: $action_type, actor_type: 'reviewer', actor_id: 'current_user',"
        " notes: $notes, created_at: toString(datetime())}]",
    ]
    if resolution is not None:
        set_clauses.append("rc.resolution = $resolution")
        set_clauses.append("rc.resolved_at = datetime()")
    if follow_up_at is not None:
        set_clauses.append("rc.follow_up_at = datetime($follow_up_at)")

    joined: LiteralString = ", ".join(set_clauses)
    cypher: LiteralString = (
        "MATCH (rc:ReviewCase {review_case_id: $review_case_id}) "
        "WHERE rc.queue_state IN ['open', 'assigned', 'deferred'] "
        "SET " + joined + " "
        "RETURN rc {.review_case_id, .queue_state, .resolution} AS review_case"
    )

    result = await tx.run(
        cypher,
        review_case_id=review_case_id,
        new_state=new_state,
        action_type=action_type,
        notes=notes,
        resolution=resolution,
        follow_up_at=follow_up_at,
    )
    record = await result.single()
    if record is None:
        return None

    if action_type == ApiReviewActionType.MANUAL_NO_MATCH.value:
        await tx.run(
            CREATE_NO_MATCH_LOCK_FROM_REVIEW,
            review_case_id=review_case_id,
            notes=notes or "Manual no-match from review",
        )

    return dict(record["review_case"])
