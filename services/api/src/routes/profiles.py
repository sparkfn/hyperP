"""Read-only Profile endpoints for derived C3 evidence clusters."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from src.auth.deps import require_scope
from src.http_utils import envelope, http_error, next_cursor, page_window
from src.repositories.deps import get_profile_repo
from src.repositories.protocols.profile import ProfileListFilters, ProfileRepository
from src.types import ApiResponse, Profile, ProfileDetail

router = APIRouter(
    prefix="/v1/profiles",
    tags=["Profiles"],
    dependencies=[Depends(require_scope("persons:read"))],
)


@router.get("", response_model=ApiResponse[list[Profile]], operation_id="list_profiles")
async def list_profiles(
    request: Request,
    q: str | None = Query(default=None),
    status: str | None = Query(default="open"),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    repo: ProfileRepository = Depends(get_profile_repo),
) -> ApiResponse[list[Profile]]:
    """List derived Profiles, defaulting to unverified open clusters."""
    q_clean = q.strip() if q else None
    if q_clean is not None and len(q_clean) < 2:
        raise http_error(
            400,
            "invalid_request",
            "Search query q requires at least 2 characters.",
            request,
        )
    skip, page_limit = page_window(cursor, limit)
    filters = ProfileListFilters(q=q_clean, status=status)
    profiles, total = await repo.get_page(filters, skip, page_limit)
    has_more = skip + page_limit < total
    return envelope(
        profiles,
        request,
        next_cursor(skip, page_limit, has_more),
        total_count=total,
    )


@router.get("/{profile_id}", response_model=ApiResponse[ProfileDetail], operation_id="get_profile")
async def get_profile(
    profile_id: str,
    request: Request,
    repo: ProfileRepository = Depends(get_profile_repo),
) -> ApiResponse[ProfileDetail]:
    """Return one Profile with its member records and weighted identifiers."""
    profile = await repo.get_by_id(profile_id)
    if profile is None:
        raise http_error(404, "profile_not_found", "Profile not found.", request)
    return envelope(profile, request)
