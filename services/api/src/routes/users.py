"""User administration endpoints (admin-only)."""

from __future__ import annotations

from typing import Literal, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from src.auth.deps import require_admin
from src.auth.models import AuthUser, Role
from src.auth.store import entity_exists, list_users, update_user
from src.http_utils import envelope, http_error
from src.types import ApiResponse

router = APIRouter(prefix="/v1/users")


class UserResponse(BaseModel):
    email: str
    google_sub: str
    role: str
    entity_key: str | None = None
    display_name: str | None = None


class UserUpdateRequest(BaseModel):
    role: Literal["admin", "employee", "first_time"] | None = None
    entity_key: str | None = None


def _to_response(user: AuthUser) -> UserResponse:
    return UserResponse(
        email=user.email,
        google_sub=user.google_sub,
        role=user.role,
        entity_key=user.entity_key,
        display_name=user.display_name,
    )


@router.get("", response_model=ApiResponse[list[UserResponse]])
async def list_all_users(
    request: Request, _admin: AuthUser = Depends(require_admin)
) -> ApiResponse[list[UserResponse]]:
    """Return every known user. Admin only."""
    users = await list_users()
    return envelope([_to_response(u) for u in users], request)


@router.patch("/{email}", response_model=ApiResponse[UserResponse])
async def patch_user(
    email: str,
    body: UserUpdateRequest,
    request: Request,
    _admin: AuthUser = Depends(require_admin),
) -> ApiResponse[UserResponse]:
    """Update a user's role and/or entity assignment. Admin only."""
    new_role: Role | None = cast(Role, body.role) if body.role is not None else None

    # Enforce invariants: employees require an entity; admins must not be scoped.
    effective_role: Role | None = new_role
    target_entity = body.entity_key

    if effective_role == "employee" and target_entity is None:
        raise http_error(
            400,
            "invalid_request",
            "An employee must be assigned an entity_key.",
            request,
        )
    if effective_role == "admin":
        target_entity = None
    if effective_role == "first_time":
        target_entity = None

    if target_entity is not None and not await entity_exists(target_entity):
        raise http_error(
            404, "not_found", f"Entity '{target_entity}' does not exist.", request
        )

    updated = await update_user(email=email, new_role=effective_role, entity_key=target_entity)
    if updated is None:
        raise http_error(404, "not_found", f"User '{email}' not found.", request)
    return envelope(_to_response(updated), request)
