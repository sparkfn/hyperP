"""FastAPI dependencies for authentication and authorization."""

from __future__ import annotations

import logging
from collections.abc import Callable

from cachetools import TTLCache
from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field

from src.auth.api_keys import check_scope, validate_api_key
from src.auth.models import AuthUser
from src.auth.revoke import decode_jwt_claims, is_token_revoked
from src.auth.store import (
    get_entities_for_review_case,
    get_entity_for_source,
    upsert_user_on_login,
)
from src.auth.verify import verify_google_id_token
from src.config import config
from src.http_utils import http_error

log = logging.getLogger(__name__)

# Keyed by jti so we can evict on logout. Value is the raw token so we can
# also evict on raw-token lookup (token rotation etc.).
_USER_CACHE: TTLCache[str, tuple[str, AuthUser]] = TTLCache(maxsize=1024, ttl=30.0)
_BEARER_AUTH = HTTPBearer(auto_error=False)
_BEARER_CREDENTIALS = Security(_BEARER_AUTH)

_DEV_BYPASS_USER: AuthUser = AuthUser(
    email="dev-bypass@local",
    google_sub="dev-bypass",
    role="admin",
    entity_key=None,
    display_name="Dev Bypass",
)

# Alias for callers that need HTTPBearer credential extraction.
_BEARER_DEP = _BEARER_CREDENTIALS


def evict_user_cache(jti: str | None) -> None:
    """Remove a user's cached entry by jti so revocation takes effect immediately."""
    if jti is not None:
        _USER_CACHE.pop(jti, None)


def _extract_bearer_token(
    credentials: HTTPAuthorizationCredentials | None,
) -> str | None:
    if credentials is None:
        return None
    return credentials.credentials


def _get_api_key_header(request: Request) -> str | None:
    """Read X-Api-Key from the raw request (avoids HTTPBearer consuming headers)."""
    return request.headers.get(config.api_key_header_name) or None


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = _BEARER_CREDENTIALS,
) -> AuthUser:
    """Resolve the authenticated principal from the Bearer ID token.

    Allows first-time users through — enforcement against unassigned accounts
    is done by `require_active_user`. Used directly only by /v1/auth/me.
    """
    if not config.auth_enabled:
        return _DEV_BYPASS_USER

    token = _extract_bearer_token(credentials)
    if token is None:
        raise http_error(401, "unauthorized", "Missing Bearer token.", request)

    jti, _exp = decode_jwt_claims(token)
    if jti is not None and await is_token_revoked(jti):
        raise http_error(401, "token_revoked", "Token has been revoked.", request)

    cached = _USER_CACHE.get(jti if jti is not None else token)
    if cached is not None:
        return cached[1]

    try:
        claims = verify_google_id_token(token)
    except ValueError as exc:
        log.warning("Token verification failed: %s", exc)
        raise http_error(401, "unauthorized", f"Invalid token: {exc}", request) from exc

    user = await upsert_user_on_login(
        email=claims.email, google_sub=claims.sub, display_name=claims.name
    )
    _USER_CACHE[jti if jti is not None else token] = (token, user)
    return user


# --- API key (server-to-server) auth ---


class ApiKeyUser(AuthUser):
    """AuthUser subclass for API-key-authenticated callers."""

    source: str = "api_key"
    key_scopes: list[str] = Field(default_factory=list)


async def get_current_user_or_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = _BEARER_CREDENTIALS,
) -> AuthUser | ApiKeyUser:
    """Resolve principal from either a Bearer token or an API key.

    API key is attempted first when enabled; falls back to Bearer on a missing
    or invalid header. Human Bearer users get a 401 for missing tokens.
    """
    if not config.auth_enabled:
        return _DEV_BYPASS_USER

    # Read X-Api-Key from raw request headers so HTTPBearer doesn't consume it.
    api_key_header = _get_api_key_header(request)
    if config.api_keys_enabled and api_key_header is not None:
        result = await validate_api_key(api_key_header)
        if result is not None:
            key_obj, scopes = result
            user = ApiKeyUser(
                email=f"apikey:{key_obj.key_prefix}@api",
                google_sub=key_obj.id,
                role="admin",
                entity_key=key_obj.entity_key,
                display_name=key_obj.name,
            )
            user.key_scopes = scopes
            request.state.api_key_scopes = scopes
            return user

    # Fall back to Bearer — raises 401 for missing token
    return await get_current_user(request, credentials)


# All require_* functions use get_current_user_or_api_key so they accept
# both Bearer users and API-key users. role checks are safe for both types.


async def require_active_user(
    request: Request, user: AuthUser | ApiKeyUser = Depends(get_current_user_or_api_key)
) -> AuthUser | ApiKeyUser:
    """Allow only users who have been assigned an entity (or are admins)."""
    if user.role == "first_time":
        raise http_error(
            403,
            "forbidden_pending_assignment",
            "Your account is pending entity assignment by an administrator.",
            request,
        )
    return user


async def require_admin(
    request: Request, user: AuthUser | ApiKeyUser = Depends(get_current_user_or_api_key)
) -> AuthUser | ApiKeyUser:
    """Admin-only gate."""
    if user.role != "admin":
        raise http_error(
            403, "forbidden", "This action requires administrator privileges.", request
        )
    return user


async def require_mutator_for_source(
    source_key: str,
    request: Request,
    user: AuthUser | ApiKeyUser = Depends(get_current_user_or_api_key),
) -> AuthUser | ApiKeyUser:
    """Allow admin unconditionally; allow employee iff source_key → their entity."""
    if user.role == "admin":
        return user
    if user.role != "employee":
        raise http_error(
            403,
            "forbidden_pending_assignment",
            "Your account is pending entity assignment by an administrator.",
            request,
        )
    target_entity = await get_entity_for_source(source_key)
    if target_entity is None:
        raise http_error(404, "not_found", f"Source system '{source_key}' not found.", request)
    if user.entity_key != target_entity:
        raise http_error(
            403,
            "forbidden_entity_scope",
            "You do not have permission to mutate data for this entity.",
            request,
        )
    return user


async def require_mutator_for_review_case(
    review_case_id: str,
    request: Request,
    user: AuthUser | ApiKeyUser = Depends(get_current_user_or_api_key),
) -> AuthUser | ApiKeyUser:
    """Allow admin; allow employee iff at least one side of the case is their entity."""
    if user.role == "admin":
        return user
    if user.role != "employee":
        raise http_error(
            403,
            "forbidden_pending_assignment",
            "Your account is pending entity assignment by an administrator.",
            request,
        )
    case_entities = await get_entities_for_review_case(review_case_id)
    if user.entity_key is None or user.entity_key not in case_entities:
        raise http_error(
            403,
            "forbidden_entity_scope",
            "You do not have permission to act on this review case.",
            request,
        )
    return user


async def require_mutator_for_entity(
    entity_key: str,
    request: Request,
    user: AuthUser | ApiKeyUser = Depends(get_current_user_or_api_key),
) -> AuthUser | ApiKeyUser:
    """Direct entity-key mutator check (admin or matching employee)."""
    if user.role == "admin":
        return user
    if user.role != "employee":
        raise http_error(
            403,
            "forbidden_pending_assignment",
            "Your account is pending entity assignment by an administrator.",
            request,
        )
    if user.entity_key != entity_key:
        raise http_error(
            403,
            "forbidden_entity_scope",
            "You do not have permission to mutate data for this entity.",
            request,
        )
    return user


def require_scope(required: str) -> Callable[..., AuthUser | ApiKeyUser]:
    """Return a FastAPI dependency that checks for a specific API key scope."""
    async def _dep(
        request: Request,
        user: AuthUser | ApiKeyUser = Depends(get_current_user_or_api_key),
    ) -> AuthUser | ApiKeyUser:
        scopes = getattr(user, "key_scopes", None) or []
        if not check_scope(scopes, required):
            raise http_error(
                403, "forbidden", f"API key lacks required scope: {required}", request
            )
        return user

    return _dep  # type: ignore[return-value]
