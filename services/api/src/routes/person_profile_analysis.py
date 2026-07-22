"""Authenticated current and history endpoints for Person profile analyses."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from src.auth.deps import require_human_user, require_scope
from src.config import AppConfig, get_config
from src.http_utils import envelope, http_error, next_cursor, page_window
from src.repositories.deps import get_person_repo
from src.repositories.protocols.person import PersonRepository
from src.types import ApiResponse
from src.types_profile_analysis import (
    PersonProfileAnalyses,
    ProfileAnalysisHistoryItem,
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


@router.get(
    "/{person_id}/profile-analyses",
    response_model=ApiResponse[PersonProfileAnalyses],
)
async def get_person_profile_analyses(
    person_id: str,
    request: Request,
    repo: PersonRepository = Depends(get_person_repo),
    app_config: AppConfig = Depends(get_config),
) -> ApiResponse[PersonProfileAnalyses]:
    """Return current independent Person analyses and their refresh states."""
    analyses = await repo.get_profile_analyses(person_id)
    if analyses is None:
        raise http_error(404, "person_not_found", "Person not found.", request)
    if not app_config.profile_analysis_enabled:
        analyses = analyses.model_copy(
            update={
                "refresh_state": "disabled",
                "sales": analyses.sales.model_copy(
                    update={"refresh_state": "disabled", "failure_code": None}
                ),
                "contact_tracing": analyses.contact_tracing.model_copy(
                    update={"refresh_state": "disabled", "failure_code": None}
                ),
            }
        )
    return envelope(analyses, request)


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
