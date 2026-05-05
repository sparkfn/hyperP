"""Admin endpoints for source systems and field-trust configuration."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from src.auth.deps import require_admin
from src.auth.models import AuthUser
from src.http_utils import envelope, http_error
from src.repositories.deps import get_admin_repo
from src.repositories.protocols.admin import AdminRepository, FieldTrustResponse, SourceSystemInfo
from src.types import ApiResponse
from src.types_requests import FieldTrustUpdateRequest

router = APIRouter()


@router.get(
    "/v1/source-systems",
    response_model=ApiResponse[list[SourceSystemInfo]],
    dependencies=[Depends(require_admin)],
)
async def list_source_systems(
    request: Request,
    repo: AdminRepository = Depends(get_admin_repo),
) -> ApiResponse[list[SourceSystemInfo]]:
    """List configured source systems."""
    systems = await repo.get_all_source_systems()
    return envelope(systems, request)


@router.get(
    "/v1/source-systems/{source_key}/field-trust",
    response_model=ApiResponse[FieldTrustResponse],
    dependencies=[Depends(require_admin)],
)
async def get_field_trust(
    source_key: str,
    request: Request,
    repo: AdminRepository = Depends(get_admin_repo),
) -> ApiResponse[FieldTrustResponse]:
    """Return field-level trust configuration for a source system."""
    result = await repo.get_field_trust(source_key)
    if result is None:
        raise http_error(404, "not_found", f"Source system '{source_key}' not found.", request)
    return envelope(result, request)


@router.patch(
    "/v1/source-systems/{source_key}/field-trust",
    response_model=ApiResponse[FieldTrustResponse],
)
async def update_field_trust(
    source_key: str,
    body: FieldTrustUpdateRequest,
    request: Request,
    _user: AuthUser = Depends(require_admin),
    repo: AdminRepository = Depends(get_admin_repo),
) -> ApiResponse[FieldTrustResponse]:
    """Update trust tiers for one or more fields on a source system."""
    if not body.updates:
        raise http_error(
            400, "invalid_request", "Provide at least one field trust update.", request
        )

    merged = await repo.update_field_trust(source_key, body.updates)
    if merged is None:
        raise http_error(404, "not_found", f"Source system '{source_key}' not found.", request)

    return envelope(
        FieldTrustResponse(source_key=source_key, display_name=None, field_trust=merged), request
    )
