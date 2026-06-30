"""Admin endpoints for managing OAuth machine clients."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from src.auth.deps import require_human_admin
from src.auth.models import AuthUser
from src.auth.oauth_client_models import (
    CreateOAuthClientRequest,
    OAuthAccessTokenView,
    OAuthClient,
    OAuthClientCreatedResponse,
    RotateSecretResponse,
    UpdateOAuthClientRequest,
)
from src.auth.oauth_clients import (
    create_oauth_client,
    delete_oauth_client,
    disable_oauth_client,
    list_oauth_clients,
    rotate_oauth_client_secret,
    update_oauth_client,
)
from src.auth.oauth_token_registry import (
    TokenRecord,
    clear_client_tokens,
    list_client_tokens,
    revoke_token_entry,
)
from src.auth.revoke import revoke_token
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
    "/{client_id}/rotate-secret",
    response_model=RotateSecretResponse,
    status_code=201,
)
async def rotate_oauth_secret_handler(
    client_id: str,
    request: Request,
    _user: AuthUser = Depends(require_human_admin),
) -> RotateSecretResponse:
    """Rotate an OAuth client's secret, returning the new plaintext once."""
    rotated = await rotate_oauth_client_secret(client_id)
    if rotated is None:
        raise http_error(404, "not_found", "OAuth client not found.", request)
    # Old tokens are already dead via the secret_id kill-switch; purge their
    # registry entries so the token list reflects only usable tokens.
    await clear_client_tokens(client_id)
    return rotated


@router.patch("/{client_id}", status_code=204)
async def update_client_handler(
    client_id: str,
    body: UpdateOAuthClientRequest,
    request: Request,
    _user: AuthUser = Depends(require_human_admin),
) -> None:
    """Update a client's name, scopes, or access-token TTL."""
    if not await update_oauth_client(client_id, body):
        raise http_error(404, "not_found", "OAuth client not found.", request)


@router.get("/{client_id}/tokens", response_model=list[OAuthAccessTokenView])
async def list_tokens_handler(
    client_id: str,
    _user: AuthUser = Depends(require_human_admin),
) -> list[OAuthAccessTokenView]:
    """List a client's currently-valid access tokens."""
    records: list[TokenRecord] = await list_client_tokens(client_id)
    return [
        OAuthAccessTokenView(
            jti=r.jti,
            scope=r.scope,
            issued_at=r.issued_at,
            expires_at=r.expires_at,
            last_used_at=r.last_used_at,
            last_used_ip=r.last_used_ip,
        )
        for r in records
    ]


@router.post("/{client_id}/tokens/{jti}/revoke", status_code=204)
async def revoke_token_handler(
    client_id: str,
    jti: str,
    request: Request,
    _user: AuthUser = Depends(require_human_admin),
) -> None:
    """Revoke one access token immediately."""
    records = await list_client_tokens(client_id)
    match = next((r for r in records if r.jti == jti), None)
    if match is None:
        raise http_error(404, "not_found", "Token not found.", request)
    await revoke_token(jti, match.expires_at)
    await revoke_token_entry(client_id, jti)


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
