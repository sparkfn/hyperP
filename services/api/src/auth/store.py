"""Neo4j-backed user store and entity-scope lookups for authorization."""

from __future__ import annotations

from typing import cast

from src.auth.models import AuthUser, Role
from src.config import config
from src.graph.client import get_session
from src.graph.converters import to_optional_str, to_str
from src.graph.queries.users import (
    GET_ENTITIES_FOR_REVIEW_CASE,
    GET_ENTITY_FOR_ENTITY_KEY,
    GET_ENTITY_FOR_SOURCE,
    GET_USER_BY_EMAIL,
    LIST_USERS,
    UPDATE_USER,
    UPSERT_USER_ON_LOGIN,
)

_VALID_ROLES: frozenset[str] = frozenset({"admin", "employee", "first_time"})


def _role_from_value(value: object) -> Role:
    raw = to_str(value, "first_time")
    if raw not in _VALID_ROLES:
        return "first_time"
    return cast(Role, raw)


def _auth_user_from_record(user_map: object) -> AuthUser:
    if not isinstance(user_map, dict):
        raise ValueError("unexpected user record shape")
    return AuthUser(
        email=to_str(user_map.get("email")),
        google_sub=to_str(user_map.get("google_sub")),
        role=_role_from_value(user_map.get("role")),
        entity_key=to_optional_str(user_map.get("entity_key")),
        display_name=to_optional_str(user_map.get("display_name")),
    )


async def upsert_user_on_login(
    email: str, google_sub: str, display_name: str | None
) -> AuthUser:
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


async def update_user(
    email: str, new_role: Role | None, entity_key: str | None
) -> AuthUser | None:
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
        result = await session.run(
            GET_ENTITIES_FOR_REVIEW_CASE, review_case_id=review_case_id
        )
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
