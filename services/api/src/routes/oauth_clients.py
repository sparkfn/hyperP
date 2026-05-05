"""Admin endpoints for managing OAuth machine clients."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from src.auth.deps import require_human_admin
from src.auth.models import AuthUser
from src.auth.oauth_client_models import (
    CreateOAuthClientRequest,
    CreateOAuthClientSecretRequest,
    OAuthClient,
    OAuthClientCreatedResponse,
    OAuthClientSecretCreatedResponse,
)
from src.auth.oauth_clients import (
    create_oauth_client,
    create_oauth_client_secret,
    delete_oauth_client,
    disable_oauth_client,
    list_oauth_clients,
    revoke_oauth_client_secret,
)
from src.http_utils import http_error

router = APIRouter(prefix="/v1/admin/oauth-clients", tags=["Admin"])


@router.post("", response_model=OAuthClientCreatedResponse, status_code=201)
async def create_oauth_client_handler(
    body: CreateOAuthClientRequest,
    user: AuthUser = Depends(require_human_admin),
) -> OAuthClientCreatedResponse:
    """Create an OAuth client and return its first secret once."""
    return await create_oauth_client(body, user)


@router.get("", response_model=list[OAuthClient])
async def list_oauth_clients_handler(
    _user: AuthUser = Depends(require_human_admin),
) -> list[OAuthClient]:
    """List OAuth clients for admin management."""
    return await list_oauth_clients()


@router.post(
    "/{client_id}/secrets",
    response_model=OAuthClientSecretCreatedResponse,
    status_code=201,
)
async def create_oauth_secret_handler(
    client_id: str,
    body: CreateOAuthClientSecretRequest,
    request: Request,
    _user: AuthUser = Depends(require_human_admin),
) -> OAuthClientSecretCreatedResponse:
    """Create another one-time secret for an OAuth client."""
    created = await create_oauth_client_secret(client_id, body)
    if created is None:
        raise http_error(404, "not_found", "OAuth client not found.", request)
    return created


@router.post("/{client_id}/secrets/{secret_id}/revoke", status_code=204)
async def revoke_oauth_secret_handler(
    client_id: str,
    secret_id: str,
    request: Request,
    _user: AuthUser = Depends(require_human_admin),
) -> None:
    """Revoke one OAuth client secret."""
    if not await revoke_oauth_client_secret(client_id, secret_id):
        raise http_error(404, "not_found", "OAuth client secret not found.", request)


@router.post("/{client_id}/disable", status_code=204)
async def disable_oauth_client_handler(
    client_id: str,
    request: Request,
    _user: AuthUser = Depends(require_human_admin),
) -> None:
    """Disable an OAuth client."""
    if not await disable_oauth_client(client_id):
        raise http_error(404, "not_found", "OAuth client not found.", request)


@router.delete("/{client_id}", status_code=204)
async def delete_oauth_client_handler(
    client_id: str,
    request: Request,
    _user: AuthUser = Depends(require_human_admin),
) -> None:
    """Delete an OAuth client and its secrets."""
    if not await delete_oauth_client(client_id):
        raise http_error(404, "not_found", "OAuth client not found.", request)
