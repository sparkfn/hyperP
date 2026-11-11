"""OAuth2 client-credentials endpoints (root /v1/oauth spelling deprecated: use /oauth2/v1)."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Coroutine

from fastapi import APIRouter, FastAPI, Form, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from src.auth.oauth_client_models import OAuthTokenResponse
from src.auth.oauth_clients import (
    active_secret,
    requested_scopes_or_default,
    validate_client_credentials,
)
from src.auth.oauth_token_registry import register_token
from src.auth.oauth_tokens import JsonWebKeySet, build_jwks, issue_client_access_token

# RFC 8594 Sunset for the deprecated root /v1/oauth routes. Proposed date (issue #480);
# a fixed constant keeps responses and tests deterministic.
SUNSET_HTTP_DATE = "Thu, 01 Jul 2027 00:00:00 GMT"


class LegacyOAuthRoute(APIRoute):
    """Root-only route class adding ``Sunset``; copy_router_routes copies endpoints only."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[object, object, Response]]:
        handler = super().get_route_handler()

        async def sunset_handler(request: Request) -> Response:
            response = await handler(request)
            response.headers["Sunset"] = SUNSET_HTTP_DATE
            return response

        return sunset_handler


router = APIRouter(prefix="/v1/oauth", tags=["OAuth"], route_class=LegacyOAuthRoute)

_ROOT_SUCCESSORS: dict[str, str] = {
    "/v1/oauth/token": "POST /oauth2/v1/token (externally /api/oauth2/v1/token)",
    "/v1/oauth/jwks": "GET /oauth2/v1/jwks (externally /api/oauth2/v1/jwks)",
}


def mark_root_routes_deprecated(app: FastAPI) -> None:
    """Flag the root /v1/oauth registrations deprecated; copied routes keep their metadata."""
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path in _ROOT_SUCCESSORS:
            successor = _ROOT_SUCCESSORS[route.path]
            route.deprecated = True
            route.summary = f"{route.name.title()}. Deprecated: use {successor}."


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

    secret = active_secret(client)
    if secret is None:
        return _oauth_error(
            status.HTTP_401_UNAUTHORIZED, "invalid_client", "Invalid client credentials."
        )
    secret_id = secret.secret_id

    expires_in = client.access_token_ttl_seconds
    now = int(time.time())
    jti = str(uuid.uuid4())
    access_token = issue_client_access_token(
        client, granted_scopes, secret_id=secret_id, expires_in_seconds=expires_in, jti=jti
    )
    await register_token(
        jti=jti,
        client_id=client.client_id,
        secret_id=secret_id,
        scope=" ".join(granted_scopes),
        issued_at=now,
        expires_at=now + expires_in,
    )
    return JSONResponse(
        OAuthTokenResponse(
            access_token=access_token, expires_in=expires_in, scope=" ".join(granted_scopes)
        ).model_dump(),
        headers=_NO_CACHE_HEADERS,
    )


@router.get("/jwks")
async def jwks() -> JsonWebKeySet:
    """Return public signing keys for HyperP-issued access tokens."""
    return build_jwks()
