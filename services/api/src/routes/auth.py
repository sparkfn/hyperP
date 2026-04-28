"""Authentication endpoints: exchange Google ID token for principal info, and logout."""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from src.auth.deps import evict_user_cache, get_current_user
from src.auth.models import AuthUser, Role
from src.auth.revoke import decode_jwt_claims, revoke_token
from src.config import config
from src.http_utils import envelope
from src.types import ApiResponse

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/auth")


class MeResponse(BaseModel):
    email: str
    google_sub: str
    role: Role
    entity_key: str | None = None
    display_name: str | None = None


class LogoutRequest(BaseModel):
    refresh_token: str | None = None


class LogoutResponse(BaseModel):
    ok: bool


def _to_response(user: AuthUser) -> MeResponse:
    return MeResponse(
        email=user.email,
        google_sub=user.google_sub,
        role=user.role,
        entity_key=user.entity_key,
        display_name=user.display_name,
    )


@router.get("/me", response_model=ApiResponse[MeResponse])
async def read_me(
    request: Request, user: AuthUser = Depends(get_current_user)
) -> ApiResponse[MeResponse]:
    """Return the authenticated user's role and entity assignment."""
    return envelope(_to_response(user), request)


@router.post("/logout", response_model=ApiResponse[LogoutResponse])
async def logout(
    request: Request,
    user: AuthUser = Depends(get_current_user),
    body: LogoutRequest | None = None,
) -> ApiResponse[LogoutResponse]:
    """Revoke the current access token in Redis and optionally the refresh token via Google."""
    auth_header = request.headers.get("authorization", "")
    scheme, _, token_str = auth_header.partition(" ")
    jti: str | None = None
    if scheme.lower() == "bearer" and token_str:
        jti, exp = decode_jwt_claims(token_str)
        if jti is not None and exp is not None:
            await revoke_token(jti, exp)
            log.info("Access token revoked for user sub=%s", user.google_sub)

    # Evict from in-process cache so the revoked token can't still be used via
    # the warm-cache path (valid JWT but freshly revoked in Redis).
    evict_user_cache(jti)

    if body and body.refresh_token:
        await _revoke_google_refresh_token(body.refresh_token)

    return envelope(LogoutResponse(ok=True), request)


async def _revoke_google_refresh_token(refresh_token: str) -> None:
    """Call Google's token revocation endpoint to invalidate the refresh token."""
    client_id = config.google_oauth_client_id
    if not client_id:
        log.warning("Cannot revoke Google refresh token: AUTH_GOOGLE_ID not set")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                "https://oauth2.googleapis.com/revoke",
                data={"token": refresh_token, "token_type_hint": "refresh_token"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        log.info("Google refresh token revoked")
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to revoke Google refresh token: %s", exc)
