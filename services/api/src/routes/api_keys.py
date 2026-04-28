"""Admin endpoints for managing server-to-server API keys."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from src.auth.api_key_models import ApiKey, ApiKeyCreatedResponse, CreateApiKeyRequest
from src.auth.api_keys import create_api_key, delete_api_key, list_api_keys, revoke_api_key
from src.auth.deps import ApiKeyUser, require_admin
from src.auth.models import AuthUser
from src.http_utils import http_error

router = APIRouter(prefix="/v1/admin/api-keys", tags=["Admin"])


@router.post(
    "",
    response_model=ApiKeyCreatedResponse,
    status_code=201,
    summary="Create an API key",
    description=(
        "Creates a new server-to-server API key. "
        "The full secret is returned **once only** — store it immediately."
    ),
)
async def post_api_key(
    request: Request,
    body: CreateApiKeyRequest,
    user: AuthUser | ApiKeyUser = Depends(require_admin),
) -> ApiKeyCreatedResponse:
    """Create a new API key. Admin only. The plain secret is only returned at creation time."""
    return await create_api_key(body, user)


@router.get(
    "",
    response_model=list[ApiKey],
    summary="List API keys",
    description=(
        "Returns all non-revoked API keys (without secrets). "
        "Revoked keys are not shown."
    ),
)
async def get_api_keys(
    request: Request,
    user: AuthUser | ApiKeyUser = Depends(require_admin),
) -> list[ApiKey]:
    """List all active API keys. Admin only."""
    return await list_api_keys()


@router.delete(
    "/{key_id}",
    status_code=204,
    summary="Delete an API key",
    description="Permanently deletes an API key. Prefer revocation for audit trails.",
)
async def delete_api_key_handler(
    key_id: str,
    request: Request,
    user: AuthUser | ApiKeyUser = Depends(require_admin),
) -> None:
    """Delete an API key by id. Admin only."""
    await delete_api_key(key_id)


@router.post(
    "/{key_id}/revoke",
    status_code=204,
    summary="Revoke an API key",
    description=(
        "Soft-revokes an API key so it immediately stops validating. "
        "Adds the key prefix to the Redis revocation list for fast reject-path checks."
    ),
)
async def revoke_api_key_handler(
    key_id: str,
    request: Request,
    user: AuthUser | ApiKeyUser = Depends(require_admin),
) -> None:
    """Revoke an API key by id. Admin only."""
    found = await revoke_api_key(key_id)
    if not found:
        raise http_error(404, "not_found", f"API key '{key_id}' not found.", request)
