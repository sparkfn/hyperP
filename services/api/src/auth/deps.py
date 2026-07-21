"""FastAPI dependencies for authentication and authorization."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from cachetools import TTLCache
from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.auth.models import AuthUser, Role
from src.auth.oauth_client_models import (
    ALLOWED_OAUTH_CLIENT_SCOPES,
    OAuthClient,
    OAuthClientUser,
)
from src.auth.oauth_clients import active_secret, check_scope, get_oauth_client_by_id
from src.auth.oauth_token_registry import touch_token
from src.auth.oauth_tokens import verify_client_access_token
from src.auth.revoke import decode_jwt_claims, is_token_revoked
from src.auth.store import (
    get_entities_for_review_case,
    get_entity_for_source,
    upsert_user_on_login,
)
from src.auth.verify import verify_google_id_token
from src.config import config
from src.http_utils import client_ip, http_error

log = logging.getLogger(__name__)

# Keyed by jti so we can evict on logout. Value is the raw token so we can
# also evict on raw-token lookup (token rotation etc.).
_USER_CACHE: TTLCache[str, tuple[str, AuthUser]] = TTLCache(maxsize=1024, ttl=30.0)
_BEARER_AUTH = HTTPBearer(auto_error=False)
_BEARER_CREDENTIALS = Security(_BEARER_AUTH)
_SHARED_MULTI_ENTITY_SOURCE_KEYS: frozenset[str] = frozenset({"bitrix_chat"})

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

    cache_key = jti if jti is not None else token
    cached = _USER_CACHE.get(cache_key)
    if cached is not None and cached[0] == token:
        return cached[1]

    try:
        claims = await verify_google_id_token(token)
    except ValueError as exc:
        log.warning("Token verification failed: %s", exc)
        raise http_error(401, "unauthorized", f"Invalid token: {exc}", request) from exc

    user = await upsert_user_on_login(
        email=claims.email, google_sub=claims.sub, display_name=claims.name
    )
    _USER_CACHE[cache_key] = (token, user)
    return user


# --- OAuth client (server-to-server) auth ---


def _reconciled_oauth_scopes(
    token_scopes: list[str], client: OAuthClient, request: Request
) -> list[str]:
    token_scope_set = set(token_scopes)
    if len(token_scope_set) != len(token_scopes):
        raise http_error(403, "forbidden", "OAuth token scopes are invalid.", request)
    if any(scope not in ALLOWED_OAUTH_CLIENT_SCOPES for scope in token_scopes):
        raise http_error(403, "forbidden", "OAuth token scopes are invalid.", request)

    persisted_scope_set = set(client.scopes)
    if "admin" in persisted_scope_set:
        return client.scopes
    if any(scope not in persisted_scope_set for scope in token_scopes):
        raise http_error(
            403,
            "forbidden",
            "OAuth token scopes are no longer assigned to this client.",
            request,
        )
    return token_scopes


def _reconciled_oauth_entity_key(
    token_entity_key: str | None, client: OAuthClient, request: Request
) -> str | None:
    if token_entity_key != client.entity_key:
        raise http_error(
            403,
            "forbidden",
            "OAuth token entity scope no longer matches this client.",
            request,
        )
    return client.entity_key


def _current_secret_id(client: OAuthClient) -> str | None:
    secret = active_secret(client)
    return secret.secret_id if secret is not None else None


async def _touch_token_last_used(jti: str, request: Request) -> None:
    try:
        await touch_token(jti, last_used_ip=client_ip(request))
    except Exception:  # noqa: BLE001 — token tracking is best-effort
        log.debug("Failed to update OAuth token last-used", exc_info=True)


async def get_current_user_or_oauth_client(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = _BEARER_CREDENTIALS,
) -> AuthUser | OAuthClientUser:
    """Authenticate either a Google ID token or a HyperP OAuth client token."""
    if not config.auth_enabled:
        return _DEV_BYPASS_USER

    if credentials is None:
        raise http_error(401, "unauthorized", "Missing Authorization bearer token.", request)
    token = credentials.credentials

    try:
        claims = verify_client_access_token(token)
    except ValueError:
        return await get_current_user(request, credentials)

    if await is_token_revoked(claims.jti):
        raise http_error(401, "token_revoked", "Token has been revoked.", request)

    client = await get_oauth_client_by_id(claims.client_id)
    if client is None:
        raise http_error(
            401,
            "unauthorized",
            "OAuth client no longer exists or has been revoked.",
            request,
        )
    if client.disabled_at is not None:
        raise http_error(
            403,
            "forbidden",
            "OAuth client is disabled.",
            request,
        )

    current_secret_id = _current_secret_id(client)
    if current_secret_id is None or claims.secret_id != current_secret_id:
        raise http_error(
            401,
            "unauthorized",
            "OAuth client secret has been rotated; token is no longer valid.",
            request,
        )

    reconciled_scopes = _reconciled_oauth_scopes(claims.scopes, client, request)
    reconciled_entity_key = _reconciled_oauth_entity_key(claims.entity_key, client, request)
    await _touch_token_last_used(claims.jti, request)
    role: Role = "admin" if "admin" in reconciled_scopes else "employee"
    return OAuthClientUser(
        email=f"oauth:{claims.client_id}",
        google_sub=claims.client_id,
        role=role,
        entity_key=reconciled_entity_key,
        display_name=claims.client_id,
        source="oauth_client",
        client_id=claims.client_id,
        key_scopes=reconciled_scopes,
    )


# All require_* functions use get_current_user_or_oauth_client so they accept
# both Bearer users and OAuth-client users. role checks are safe for both types.


async def require_active_user(
    request: Request, user: AuthUser | OAuthClientUser = Depends(get_current_user_or_oauth_client)
) -> AuthUser | OAuthClientUser:
    """Allow only users who have been assigned an entity (or are admins)."""
    if user.role == "first_time":
        raise http_error(
            403,
            "forbidden_pending_assignment",
            "Your account is pending entity assignment by an administrator.",
            request,
        )
    return user


async def require_human_user(
    request: Request, user: AuthUser | OAuthClientUser = Depends(get_current_user_or_oauth_client)
) -> AuthUser:
    """Allow browser/human users only; reject OAuth client credentials."""
    if isinstance(user, OAuthClientUser):
        raise http_error(
            403,
            "forbidden",
            "OAuth clients cannot access human workflow routes.",
            request,
        )
    return user


async def require_admin(
    request: Request, user: AuthUser | OAuthClientUser = Depends(get_current_user_or_oauth_client)
) -> AuthUser | OAuthClientUser:
    """Admin-only gate."""
    if user.role != "admin":
        raise http_error(
            403, "forbidden", "This action requires administrator privileges.", request
        )
    return user


async def require_human_admin(
    request: Request, user: AuthUser = Depends(require_human_user)
) -> AuthUser:
    """Allow human administrator users only."""
    if user.role != "admin":
        raise http_error(
            403, "forbidden", "This action requires administrator privileges.", request
        )
    return user


async def require_mutator_for_source(
    source_key: str,
    request: Request,
    user: AuthUser | OAuthClientUser = Depends(get_current_user_or_oauth_client),
) -> AuthUser | OAuthClientUser:
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
    if source_key in _SHARED_MULTI_ENTITY_SOURCE_KEYS:
        raise http_error(
            403,
            "forbidden_entity_scope",
            "Only administrators can mutate a shared multi-entity source.",
            request,
        )
    target_entities = await get_entity_for_source(source_key)
    if target_entities is None:
        raise http_error(404, "not_found", f"Source system '{source_key}' not found.", request)
    if len(target_entities) > 1:
        raise http_error(
            403,
            "forbidden_entity_scope",
            "Only administrators can mutate a source spanning multiple entities.",
            request,
        )
    if not target_entities:
        raise http_error(
            403,
            "forbidden_entity_scope",
            "Only administrators can mutate a source without an entity scope.",
            request,
        )
    target_entity = next(iter(target_entities))
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
    user: AuthUser | OAuthClientUser = Depends(get_current_user_or_oauth_client),
) -> AuthUser | OAuthClientUser:
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
    user: AuthUser | OAuthClientUser = Depends(get_current_user_or_oauth_client),
) -> AuthUser | OAuthClientUser:
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


def require_scope(
    required: str,
) -> Callable[[Request, AuthUser | OAuthClientUser], Awaitable[AuthUser | OAuthClientUser]]:
    """Return a dependency that checks OAuth scopes for OAuth clients only."""

    async def _dep(
        request: Request,
        user: AuthUser | OAuthClientUser = Depends(get_current_user_or_oauth_client),
    ) -> AuthUser | OAuthClientUser:
        if not isinstance(user, OAuthClientUser):
            return user
        if not check_scope(user.key_scopes, required):
            raise http_error(
                403, "forbidden", f"OAuth client lacks required scope: {required}", request
            )
        return user

    return _dep


def require_oauth_client_scope(
    required: str,
) -> Callable[[Request, AuthUser | OAuthClientUser], Awaitable[AuthUser | OAuthClientUser]]:
    """Return a dependency requiring a HyperP OAuth client token with *required* scope.

    Unlike `require_scope`, this rejects human/Google principals — the dedicated
    machine API surface accepts OAuth2 client credentials only. The dev-bypass
    principal (auth disabled) is allowed through so local development is unaffected.
    """

    async def _dep(
        request: Request,
        user: AuthUser | OAuthClientUser = Depends(get_current_user_or_oauth_client),
    ) -> AuthUser | OAuthClientUser:
        if not config.auth_enabled:
            return user
        if not isinstance(user, OAuthClientUser):
            raise http_error(
                403,
                "forbidden",
                "This endpoint requires OAuth2 client credentials.",
                request,
            )
        if not check_scope(user.key_scopes, required):
            raise http_error(
                403, "forbidden", f"OAuth client lacks required scope: {required}", request
            )
        return user

    return _dep
