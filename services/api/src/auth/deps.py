"""FastAPI dependencies for authentication and authorization."""

from __future__ import annotations

from cachetools import TTLCache
from fastapi import Depends, Request

from src.auth.models import AuthUser
from src.auth.store import (
    get_entities_for_review_case,
    get_entity_for_source,
    upsert_user_on_login,
)
from src.auth.verify import verify_google_id_token
from src.config import config
from src.http_utils import http_error

_USER_CACHE: TTLCache[str, AuthUser] = TTLCache(maxsize=1024, ttl=30.0)

_DEV_BYPASS_USER: AuthUser = AuthUser(
    email="dev-bypass@local",
    google_sub="dev-bypass",
    role="admin",
    entity_key=None,
    display_name="Dev Bypass",
)


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization") or request.headers.get("Authorization")
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


async def get_current_user(request: Request) -> AuthUser:
    """Resolve the authenticated principal from the Bearer ID token.

    Allows first-time users through — enforcement against unassigned accounts
    is done by `require_active_user`. Used directly only by /v1/auth/me.
    """
    if not config.auth_enabled:
        return _DEV_BYPASS_USER

    token = _bearer_token(request)
    if token is None:
        raise http_error(401, "unauthorized", "Missing Bearer token.", request)

    cached = _USER_CACHE.get(token)
    if cached is not None:
        return cached

    try:
        claims = verify_google_id_token(token)
    except ValueError as exc:
        raise http_error(401, "unauthorized", f"Invalid token: {exc}", request) from exc

    # Upsert is idempotent: ON CREATE sets role based on bootstrap admin list,
    # ON MATCH only updates google_sub / display_name / last_login_at. That's
    # exactly the refresh semantics we want on every verified login.
    user = await upsert_user_on_login(
        email=claims.email, google_sub=claims.sub, display_name=claims.name
    )
    _USER_CACHE[token] = user
    return user


async def require_active_user(
    request: Request, user: AuthUser = Depends(get_current_user)
) -> AuthUser:
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
    request: Request, user: AuthUser = Depends(get_current_user)
) -> AuthUser:
    """Admin-only gate."""
    if user.role != "admin":
        raise http_error(
            403, "forbidden", "This action requires administrator privileges.", request
        )
    return user


async def require_mutator_for_source(
    source_key: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> AuthUser:
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
        raise http_error(
            404, "not_found", f"Source system '{source_key}' not found.", request
        )
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
    user: AuthUser = Depends(get_current_user),
) -> AuthUser:
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
    user: AuthUser = Depends(get_current_user),
) -> AuthUser:
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
