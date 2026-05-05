"""User administration endpoints (admin-only)."""

from __future__ import annotations

from typing import Literal, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from src.auth.deps import require_human_admin
from src.auth.models import AuthUser, Role
from src.auth.store import (
    PreRegisterUserInput,
    PreRegisterUserResult,
    bulk_pre_register_users,
    entity_exists,
    get_user_by_email,
    list_users,
    normalize_email,
    update_user,
)
from src.http_utils import envelope, http_error
from src.types import ApiResponse

router = APIRouter(prefix="/v1/users")


class UserResponse(BaseModel):
    email: str
    google_sub: str | None
    role: Role
    entity_key: str | None = None
    display_name: str | None = None


class UserUpdateRequest(BaseModel):
    role: Literal["admin", "employee", "first_time"] | None = None
    entity_key: str | None = None


class UserBulkCreateRow(BaseModel):
    email: str
    role: str
    entity_key: str | None = None


class UserBulkCreateResult(BaseModel):
    email: str
    status: Literal["created", "error"]
    code: str | None = None
    message: str | None = None
    user: UserResponse | None = None


class UserBulkCreateResponse(BaseModel):
    results: list[UserBulkCreateResult]


class UserBulkCreateRequest(BaseModel):
    users: list[UserBulkCreateRow]


_VALID_ROLES: frozenset[str] = frozenset({"admin", "employee", "first_time"})


class UserAssignmentError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _role_from_value(value: str) -> Role | None:
    if value not in _VALID_ROLES:
        return None
    return cast(Role, value)


def _valid_email_shape(email: str) -> bool:
    if not email or email.count("@") != 1 or any(char.isspace() for char in email):
        return False
    local, domain = email.split("@", maxsplit=1)
    return bool(
        local
        and domain
        and "." in domain
        and not domain.startswith(".")
        and not domain.endswith(".")
    )


def _normalise_assignment(role: Role, entity_key: str | None) -> tuple[Role, str | None]:
    target_entity = entity_key.strip() if entity_key else None
    if role == "employee" and not target_entity:
        raise UserAssignmentError("invalid_request", "An employee must be assigned an entity_key.")
    if role in {"admin", "first_time"}:
        return role, None
    return role, target_entity


def _to_response(user: AuthUser) -> UserResponse:
    return UserResponse(
        email=user.email,
        google_sub=user.google_sub,
        role=user.role,
        entity_key=user.entity_key,
        display_name=user.display_name,
    )


def _result_to_response(result: PreRegisterUserResult) -> UserBulkCreateResult:
    return UserBulkCreateResult(
        email=result.email,
        status=result.status,
        code=result.code,
        message=result.message,
        user=_to_response(result.user) if result.user is not None else None,
    )


def _row_error(email: str, code: str, message: str) -> UserBulkCreateResult:
    return UserBulkCreateResult(
        email=email,
        status="error",
        code=code,
        message=message,
        user=None,
    )


@router.get("", response_model=ApiResponse[list[UserResponse]])
async def list_all_users(
    request: Request, _admin: AuthUser = Depends(require_human_admin)
) -> ApiResponse[list[UserResponse]]:
    """Return every known user. Admin only."""
    users = await list_users()
    return envelope([_to_response(u) for u in users], request)


@router.post("/bulk", response_model=ApiResponse[UserBulkCreateResponse])
async def bulk_create_users(
    body: UserBulkCreateRequest,
    request: Request,
    _admin: AuthUser = Depends(require_human_admin),
) -> ApiResponse[UserBulkCreateResponse]:
    """Pre-register multiple users before their first Google login. Admin only."""
    normalized_emails = [normalize_email(row.email) for row in body.users]
    duplicate_emails = {
        email for email in normalized_emails if normalized_emails.count(email) > 1
    }
    results: list[UserBulkCreateResult | None] = []
    valid_rows: list[PreRegisterUserInput] = []
    valid_indexes: list[int] = []

    for row, normalized_email in zip(body.users, normalized_emails, strict=True):
        if not _valid_email_shape(normalized_email):
            results.append(
                _row_error(normalized_email, "invalid_email", "A valid email is required.")
            )
            continue
        role = _role_from_value(row.role)
        if role is None:
            results.append(
                _row_error(
                    normalized_email,
                    "invalid_role",
                    "Role must be one of admin, employee, or first_time.",
                )
            )
            continue
        if normalized_email in duplicate_emails:
            results.append(
                _row_error(
                    normalized_email,
                    "duplicate_email",
                    "Email appears more than once in this request.",
                )
            )
            continue
        try:
            role, entity_key = _normalise_assignment(role, row.entity_key)
        except UserAssignmentError as exc:
            results.append(_row_error(normalized_email, exc.code, exc.message))
            continue
        if entity_key is not None and not await entity_exists(entity_key):
            results.append(
                _row_error(
                    normalized_email,
                    "not_found",
                    f"Entity '{entity_key}' does not exist.",
                )
            )
            continue
        valid_indexes.append(len(results))
        valid_rows.append(
            PreRegisterUserInput(
                email=normalized_email,
                role=role,
                entity_key=entity_key,
            )
        )
        results.append(None)

    if valid_rows:
        store_results = await bulk_pre_register_users(valid_rows)
        for index, result in zip(valid_indexes, store_results, strict=True):
            results[index] = _result_to_response(result)

    return envelope(
        UserBulkCreateResponse(
            results=[result for result in results if result is not None]
        ),
        request,
    )


@router.patch("/{email}", response_model=ApiResponse[UserResponse])
async def patch_user(
    email: str,
    body: UserUpdateRequest,
    request: Request,
    _admin: AuthUser = Depends(require_human_admin),
) -> ApiResponse[UserResponse]:
    """Update a user's role and/or entity assignment. Admin only."""
    new_role: Role | None = body.role
    role_provided = "role" in body.model_fields_set
    entity_provided = "entity_key" in body.model_fields_set
    if not role_provided and not entity_provided:
        raise http_error(
            400,
            "invalid_request",
            "No user fields were provided to update.",
            request,
        )

    existing: AuthUser | None = None
    if not role_provided:
        existing = await get_user_by_email(email)
        if existing is None:
            raise http_error(404, "not_found", f"User '{email}' not found.", request)
        new_role = existing.role

    if entity_provided:
        target_entity = body.entity_key.strip() if body.entity_key is not None else None
    elif existing is not None:
        target_entity = existing.entity_key
    else:
        target_entity = body.entity_key.strip() if body.entity_key is not None else None

    if not role_provided and new_role in {"admin", "first_time"} and target_entity is not None:
        raise http_error(
            400,
            "invalid_request",
            "Only employees can be assigned an entity_key.",
            request,
        )

    if new_role is not None:
        try:
            effective_role, target_entity = _normalise_assignment(new_role, target_entity)
        except UserAssignmentError as exc:
            raise http_error(400, exc.code, exc.message, request) from exc
    else:
        effective_role = None

    if target_entity is not None and not await entity_exists(target_entity):
        raise http_error(404, "not_found", f"Entity '{target_entity}' does not exist.", request)

    updated = await update_user(email=email, new_role=effective_role, entity_key=target_entity)
    if updated is None:
        raise http_error(404, "not_found", f"User '{email}' not found.", request)
    return envelope(_to_response(updated), request)
