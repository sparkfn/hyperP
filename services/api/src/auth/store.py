"""Neo4j-backed user store and entity-scope lookups for authorization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from neo4j.exceptions import ClientError

from src.auth.models import AuthUser, Role
from src.config import config
from src.graph.client import get_session
from src.graph.converters import GraphValue, to_optional_str, to_str
from src.graph.queries.users import (
    CREATE_PRE_REGISTERED_USER,
    EXISTING_USER_EMAILS,
    GET_ENTITIES_FOR_REVIEW_CASE,
    GET_ENTITY_FOR_ENTITY_KEY,
    GET_ENTITY_FOR_SOURCE,
    GET_USER_BY_EMAIL,
    LIST_USERS,
    UPDATE_USER,
    UPSERT_USER_ON_LOGIN,
)

_VALID_ROLES: frozenset[str] = frozenset({"admin", "employee", "first_time"})
PreRegisterStatus = Literal["created", "error"]


class UserAlreadyExistsError(RuntimeError):
    def __init__(self, email: str) -> None:
        super().__init__(email)
        self.email = email


@dataclass(frozen=True)
class PreRegisterUserInput:
    """Admin-submitted user row for pre-registration before first login."""

    email: str
    role: Role
    entity_key: str | None


@dataclass(frozen=True)
class PreRegisterUserResult:
    """Per-row result for bulk user pre-registration."""

    email: str
    status: PreRegisterStatus
    code: str | None
    message: str | None
    user: AuthUser | None


def _role_from_value(value: GraphValue) -> Role:
    raw = to_str(value, "first_time")
    if raw not in _VALID_ROLES:
        return "first_time"
    return cast(Role, raw)


def _auth_user_from_record(user_map: object) -> AuthUser:
    if not isinstance(user_map, dict):
        raise ValueError("unexpected user record shape")
    return AuthUser(
        email=to_str(user_map.get("email")),
        google_sub=to_optional_str(user_map.get("google_sub")),
        role=_role_from_value(user_map.get("role")),
        entity_key=to_optional_str(user_map.get("entity_key")),
        display_name=to_optional_str(user_map.get("display_name")),
    )


async def upsert_user_on_login(email: str, google_sub: str, display_name: str | None) -> AuthUser:
    """MERGE a :User node on sign-in, returning the resolved principal."""
    lowered = email.lower()
    is_bootstrap_admin = lowered in config.bootstrap_admin_email_set
    async with get_session(write=True) as session:
        result = await session.run(
            UPSERT_USER_ON_LOGIN,
            email=lowered,
            google_sub=google_sub,
            display_name=display_name,
            bootstrap_admin=is_bootstrap_admin,
        )
        record = await result.single()
    if record is None:
        raise RuntimeError("Failed to upsert :User node")
    return _auth_user_from_record(record["user"])


async def get_user_by_email(email: str) -> AuthUser | None:
    """Return the :User for this email, or None."""
    async with get_session() as session:
        result = await session.run(GET_USER_BY_EMAIL, email=email.lower())
        record = await result.single()
    if record is None:
        return None
    return _auth_user_from_record(record["user"])


async def list_users() -> list[AuthUser]:
    """Return all :User nodes ordered by email."""
    users: list[AuthUser] = []
    async with get_session() as session:
        result = await session.run(LIST_USERS)
        async for record in result:
            users.append(_auth_user_from_record(record["user"]))
    return users


def normalize_email(email: str) -> str:
    """Return the canonical email form used as the :User key."""
    return email.strip().lower()


async def existing_user_emails(emails: list[str]) -> set[str]:
    """Return normalized emails that already have :User nodes."""
    normalized_emails = [normalize_email(email) for email in emails]
    if not normalized_emails:
        return set()
    async with get_session() as session:
        result = await session.run(EXISTING_USER_EMAILS, emails=normalized_emails)
        existing: set[str] = set()
        async for record in result:
            existing.add(normalize_email(to_str(record["email"])))
    return existing


def _is_user_email_constraint_error(exc: ClientError) -> bool:
    message = exc.message or ""
    return (
        exc.code == "Neo.ClientError.Schema.ConstraintValidationFailed"
        and "User" in message
        and "email" in message
    )


async def create_pre_registered_user(row: PreRegisterUserInput) -> AuthUser:
    """Create a pre-registered :User row before its first Google login."""
    try:
        async with get_session(write=True) as session:
            result = await session.run(
                CREATE_PRE_REGISTERED_USER,
                email=normalize_email(row.email),
                role=row.role,
                entity_key=row.entity_key,
            )
            record = await result.single()
    except ClientError as exc:
        if _is_user_email_constraint_error(exc):
            raise UserAlreadyExistsError(normalize_email(row.email)) from exc
        raise
    if record is None:
        raise RuntimeError("Failed to create pre-registered :User node")
    return _auth_user_from_record(record["user"])


async def bulk_pre_register_users(rows: list[PreRegisterUserInput]) -> list[PreRegisterUserResult]:
    """Create pre-registered users, returning per-row success or duplicate errors."""
    normalized_rows: list[PreRegisterUserInput] = [
        PreRegisterUserInput(
            email=normalize_email(row.email),
            role=row.role,
            entity_key=row.entity_key,
        )
        for row in rows
    ]
    normalized_emails = [row.email for row in normalized_rows]
    duplicate_emails = {
        email for email in normalized_emails if normalized_emails.count(email) > 1
    }
    existing_emails = await existing_user_emails(
        [row.email for row in normalized_rows if row.email not in duplicate_emails]
    )
    results: list[PreRegisterUserResult] = []
    for row in normalized_rows:
        if row.email in duplicate_emails:
            results.append(
                PreRegisterUserResult(
                    email=row.email,
                    status="error",
                    code="duplicate_email",
                    message="Email appears more than once in this request.",
                    user=None,
                )
            )
            continue
        if row.email in existing_emails:
            results.append(
                PreRegisterUserResult(
                    email=row.email,
                    status="error",
                    code="user_exists",
                    message="User already exists.",
                    user=None,
                )
            )
            continue
        try:
            user = await create_pre_registered_user(row)
        except UserAlreadyExistsError:
            results.append(
                PreRegisterUserResult(
                    email=row.email,
                    status="error",
                    code="user_exists",
                    message="User already exists.",
                    user=None,
                )
            )
            continue
        results.append(
            PreRegisterUserResult(
                email=row.email,
                status="created",
                code=None,
                message=None,
                user=user,
            )
        )
    return results


async def update_user(email: str, new_role: Role | None, entity_key: str | None) -> AuthUser | None:
    """Set role and/or entity_key on a user; returns updated record or None if not found."""
    async with get_session(write=True) as session:
        result = await session.run(
            UPDATE_USER,
            email=email.lower(),
            new_role=new_role,
            entity_key=entity_key,
        )
        record = await result.single()
    if record is None:
        return None
    return _auth_user_from_record(record["user"])


async def get_entity_for_source(source_key: str) -> str | None:
    """Return the entity_key that OPERATES the given source system."""
    async with get_session() as session:
        result = await session.run(GET_ENTITY_FOR_SOURCE, source_key=source_key)
        record = await result.single()
    if record is None:
        return None
    return to_optional_str(record["entity_key"])


async def get_entities_for_review_case(review_case_id: str) -> list[str]:
    """Return the set of entity keys a review case's comparison touches."""
    async with get_session() as session:
        result = await session.run(GET_ENTITIES_FOR_REVIEW_CASE, review_case_id=review_case_id)
        record = await result.single()
    if record is None:
        return []
    raw = record["entity_keys"]
    if not isinstance(raw, list):
        return []
    return [to_str(k) for k in raw if k is not None]


async def entity_exists(entity_key: str) -> bool:
    """Return True if an :Entity with this entity_key exists."""
    async with get_session() as session:
        result = await session.run(GET_ENTITY_FOR_ENTITY_KEY, entity_key=entity_key)
        record = await result.single()
    return record is not None
