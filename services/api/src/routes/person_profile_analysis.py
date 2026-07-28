"""Authenticated current and history endpoints for Person profile analyses."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from src.auth.deps import require_human_admin, require_human_user, require_scope
from src.auth.models import AuthUser
from src.celery_client import enqueue_profile_analysis_request
from src.config import AppConfig, get_config
from src.http_utils import envelope, http_error, next_cursor, page_window
from src.repositories.deps import get_person_repo
from src.repositories.protocols.person import PersonRepository
from src.types import ApiResponse
from src.types_profile_analysis import (
    PersonProfileAnalyses,
    ProfileAnalysisHistoryItem,
    ProfileAnalysisRequestBody,
    ProfileAnalysisRequestRequeueResult,
    ProfileAnalysisRequestResult,
    ProfileAnalysisRetryBody,
    ProfileAnalysisRetryResult,
    ProfileAnalysisType,
)

router = APIRouter(
    prefix="/v1/persons",
    tags=["Persons"],
    dependencies=[
        Depends(require_scope("persons:read")),
        Depends(require_human_user),
    ],
)

def _profile_analysis_retry_actor_id(user: AuthUser) -> str:
    """Return the normalized human identity used by existing audit records."""
    return user.email.strip().lower()


async def _dispatch_profile_analysis_request(
    request_id: str,
    request: Request,
    repo: PersonRepository,
) -> None:
    """Dispatch a durable request or mark it failed without exposing internals."""
    try:
        enqueue_profile_analysis_request(request_id)
    except Exception:
        await repo.mark_profile_analysis_request_dispatch_failed(request_id)
        raise http_error(
            503,
            "profile_analysis_dispatch_failed",
            "Profile analysis could not be queued. Try again later.",
            request,
        ) from None


@router.get(
    "/{person_id}/profile-analyses",
    response_model=ApiResponse[PersonProfileAnalyses],
)
async def get_person_profile_analyses(
    person_id: str,
    request: Request,
    repo: PersonRepository = Depends(get_person_repo),
    app_config: AppConfig = Depends(get_config),
    user: AuthUser = Depends(require_human_user),
) -> ApiResponse[PersonProfileAnalyses]:
    """Return current independent Person analyses and their refresh states."""
    analyses = await repo.get_profile_analyses(
        person_id,
        _profile_analysis_retry_actor_id(user),
    )
    if analyses is None:
        raise http_error(404, "person_not_found", "Person not found.", request)
    if not app_config.profile_analysis_enabled:
        analyses = analyses.model_copy(
            update={
                "refresh_state": "disabled",
                "sales": analyses.sales.model_copy(
                    update={
                        "refresh_state": "disabled",
                        "failure_code": None,
                        "auto_request_allowed": False,
                        "retry_allowed": False,
                    }
                ),
                "contact_tracing": analyses.contact_tracing.model_copy(
                    update={
                        "refresh_state": "disabled",
                        "failure_code": None,
                        "auto_request_allowed": False,
                        "retry_allowed": False,
                    }
                ),
            }
        )
    return envelope(analyses, request)


@router.post(
    "/{person_id}/profile-analyses/requests",
    response_model=ApiResponse[ProfileAnalysisRequestResult],
    status_code=202,
)
async def request_person_profile_analysis(
    person_id: str,
    body: ProfileAnalysisRequestBody,
    request: Request,
    repo: PersonRepository = Depends(get_person_repo),
    app_config: AppConfig = Depends(get_config),
) -> ApiResponse[ProfileAnalysisRequestResult]:
    """Queue one on-demand Sales or Contact Tracing analysis generation."""
    if not app_config.profile_analysis_enabled:
        raise http_error(409, "profile_analysis_disabled", "Profile analysis is disabled.", request)
    result = await repo.request_profile_analysis(person_id, body.analysis_type, body.force)
    if result is None:
        raise http_error(404, "person_not_found", "Person not found.", request)
    if result.state == "force_limited":
        details = (
            {
                "force_available_at": result.force_available_at,
                "force_available_at_display": result.force_available_at_display or "",
            }
            if result.force_available_at is not None
            else None
        )
        raise http_error(
            429,
            "profile_analysis_force_limit",
            "The forced refresh limit has been reached. Try again later.",
            request,
            details=details,
        )
    if result.state == "queued" and result.request_id is not None:
        await _dispatch_profile_analysis_request(result.request_id, request, repo)
    return envelope(result, request)


@router.post(
    "/{person_id}/profile-analyses/retries",
    response_model=ApiResponse[ProfileAnalysisRetryResult],
    status_code=202,
)
async def retry_failed_person_profile_analysis(
    person_id: str,
    body: ProfileAnalysisRetryBody,
    request: Request,
    repo: PersonRepository = Depends(get_person_repo),
    app_config: AppConfig = Depends(get_config),
    user: AuthUser = Depends(require_human_user),
) -> ApiResponse[ProfileAnalysisRetryResult]:
    """Retry one terminal failure within the user's rolling Person budget."""
    if not app_config.profile_analysis_enabled:
        raise http_error(409, "profile_analysis_disabled", "Profile analysis is disabled.", request)
    result = await repo.retry_failed_profile_analysis(
        person_id,
        body.analysis_type,
        _profile_analysis_retry_actor_id(user),
    )
    if result is None:
        raise http_error(404, "person_not_found", "Person not found.", request)
    if result.state == "retry_limited":
        details = (
            {
                "retry_available_at": result.retry_available_at or "",
                "retry_available_at_display": result.retry_available_at_display or "",
            }
            if result.retry_available_at is not None
            else None
        )
        raise http_error(
            429,
            "profile_analysis_retry_limit",
            "The profile analysis retry limit has been reached. Try again later.",
            request,
            details=details,
        )
    if result.state == "not_failed":
        raise http_error(
            409,
            "profile_analysis_retry_not_failed",
            "The selected profile analysis is no longer failed.",
            request,
        )
    if result.state == "queued" and result.request_id is not None:
        await _dispatch_profile_analysis_request(result.request_id, request, repo)
    return envelope(result, request)


@router.post(
    "/{person_id}/profile-analyses/requests/{request_id}/requeue",
    response_model=ApiResponse[ProfileAnalysisRequestRequeueResult],
    status_code=202,
)
async def requeue_failed_person_profile_analysis(
    person_id: str,
    request_id: str,
    request: Request,
    repo: PersonRepository = Depends(get_person_repo),
    app_config: AppConfig = Depends(get_config),
    _admin: object = Depends(require_human_admin),
) -> ApiResponse[ProfileAnalysisRequestRequeueResult]:
    """Allow a human administrator to requeue one safe terminal analysis failure."""
    if not app_config.profile_analysis_enabled:
        raise http_error(409, "profile_analysis_disabled", "Profile analysis is disabled.", request)
    result = await repo.requeue_failed_profile_analysis_request(
        person_id,
        request_id,
        app_config.profile_analysis_retry_limit,
    )
    if result is None:
        raise http_error(404, "profile_analysis_request_not_found", "Request not found.", request)
    if result.state != "requeued":
        raise http_error(
            409,
            f"profile_analysis_requeue_{result.state}",
            "Profile analysis request cannot be requeued.",
            request,
        )
    await _dispatch_profile_analysis_request(result.request_id, request, repo)
    return envelope(result, request)


@router.get(
    "/{person_id}/profile-analyses/history",
    response_model=ApiResponse[list[ProfileAnalysisHistoryItem]],
)
async def get_person_profile_analysis_history(
    person_id: str,
    request: Request,
    analysis_type: ProfileAnalysisType | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[ProfileAnalysisHistoryItem]]:
    """Return terminal profile-analysis history in deterministic newest-first order."""
    skip, page_limit = page_window(cursor, limit)
    page = await repo.get_profile_analysis_history(
        person_id,
        analysis_type,
        skip,
        page_limit,
    )
    if page is None:
        raise http_error(404, "person_not_found", "Person not found.", request)
    items, total = page
    has_more = skip + page_limit < total
    return envelope(
        items,
        request,
        next_cursor(skip, page_limit, has_more),
        total_count=total,
    )
