"""OAuth2 client-credentials endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Form, status
from fastapi.responses import JSONResponse

from src.auth.oauth_client_models import OAuthTokenResponse
from src.auth.oauth_clients import requested_scopes_or_default, validate_client_credentials
from src.auth.oauth_tokens import JsonWebKeySet, build_jwks, issue_client_access_token
from src.config import config

router = APIRouter(prefix="/v1/oauth", tags=["OAuth"])

_NO_CACHE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}


def _oauth_error(status_code: int, error: str, description: str) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status_code,
        headers=_NO_CACHE_HEADERS,
    )


def _missing_form_field_error(field_name: str) -> JSONResponse:
    return _oauth_error(
        status.HTTP_400_BAD_REQUEST,
        "invalid_request",
        f"Missing required form field: {field_name}.",
    )


@router.post("/token", response_model=OAuthTokenResponse)
async def token(
    grant_type: str | None = Form(default=None),
    client_id: str | None = Form(default=None),
    client_secret: str | None = Form(default=None),
    scope: str | None = Form(default=None),
) -> OAuthTokenResponse | JSONResponse:
    """Issue an access token using OAuth2 client credentials."""
    if grant_type is None:
        return _missing_form_field_error("grant_type")
    if client_id is None:
        return _missing_form_field_error("client_id")
    if client_secret is None:
        return _missing_form_field_error("client_secret")

    if grant_type != "client_credentials":
        return _oauth_error(
            status.HTTP_400_BAD_REQUEST,
            "unsupported_grant_type",
            "Only grant_type=client_credentials is supported.",
        )

    validated = await validate_client_credentials(client_id, client_secret)
    if validated is None:
        return _oauth_error(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_client",
            "Invalid client credentials.",
        )

    client, assigned_scopes = validated
    granted_scopes = requested_scopes_or_default(scope, assigned_scopes)
    if granted_scopes is None:
        return _oauth_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_scope",
            "Requested scope is not assigned to this client.",
        )

    expires_in = min(
        config.oauth_access_token_expiry_minutes,
        config.oauth_max_access_token_expiry_minutes,
    ) * 60
    access_token = issue_client_access_token(
        client, granted_scopes, expires_in_seconds=expires_in
    )
    return JSONResponse(
        OAuthTokenResponse(
            access_token=access_token,
            expires_in=expires_in,
            scope=" ".join(granted_scopes),
        ).model_dump(),
        headers=_NO_CACHE_HEADERS,
    )


@router.get("/jwks")
async def jwks() -> JsonWebKeySet:
    """Return public signing keys for HyperP-issued access tokens."""
    return build_jwks()
