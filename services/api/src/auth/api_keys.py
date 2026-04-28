"""Server-to-server API key generation, storage, and validation."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from src.auth.api_key_models import ApiKey, ApiKeyCreatedResponse, CreateApiKeyRequest
from src.auth.models import AuthUser
from src.config import config
from src.graph.client import get_session
from src.graph.converters import to_datetime, to_optional_str, to_str
from src.graph.queries.api_keys import (
    CREATE_API_KEY_CONSTRAINT,
    CREATE_API_KEY_NODE,
    DELETE_API_KEY,
    GET_API_KEY_BY_ID,
    GET_API_KEY_BY_PREFIX_HASH,
    GET_API_KEYS_FOR_ADMIN,
    REVOKE_API_KEY,
    UPDATE_API_KEY_LAST_USED,
)

# HMAC key secret — set once from config on first access.
_API_KEY_SECRET: str | None = None

# Redis key for the revoked-key-prefix set (TTL auto-cleans expired entries).
_REVOKED_SET_KEY = "revoked_api_keys"


def _get_secret() -> bytes:
    global _API_KEY_SECRET
    if _API_KEY_SECRET is None:
        _API_KEY_SECRET = config.api_key_secret or "change-me-in-env"
    return _API_KEY_SECRET.encode()


def _hash_key(plain: str) -> str:
    """HMAC-SHA256 hash of the plain key using the server secret."""
    return hmac.new(_get_secret(), plain.encode(), hashlib.sha256).hexdigest()


def _generate_key() -> tuple[str, str]:
    """Generate a new API key and its prefix. Returns (plain_key, prefix)."""
    raw = f"hp_{secrets.token_urlsafe(32)}"
    prefix = raw[:10]
    return raw, prefix


def _api_key_from_record(record: object) -> ApiKey:
    if not isinstance(record, dict):
        raise ValueError("unexpected api_key record shape")
    return ApiKey(
        id=to_str(record.get("id")),
        key_prefix=to_str(record.get("prefix")),
        name=to_str(record.get("name")),
        entity_key=to_optional_str(record.get("entity_key")),
        scopes=to_str(record.get("scopes", "")).split(",") if record.get("scopes") else [],
        created_by=to_str(record.get("created_by")),
        created_at=to_datetime(record.get("created_at")),
        expires_at=to_datetime(record.get("expires_at")),
        last_used_at=to_datetime(record.get("last_used_at")),
        is_revoked=to_str(record.get("is_revoked", "false")) == "true",
    )


async def ensure_api_key_constraint() -> None:
    """Create the :ApiKey uniqueness constraint if it does not exist."""
    try:
        async with get_session(write=True) as session:
            await session.run(CREATE_API_KEY_CONSTRAINT)
    except Exception:  # noqa: BLE001 — best-effort at startup
        pass


async def create_api_key(
    req: CreateApiKeyRequest, actor: AuthUser
) -> ApiKeyCreatedResponse:
    """Create and store a new API key. Returns the plain secret once only."""
    plain_key, prefix = _generate_key()
    key_hash = _hash_key(plain_key)
    key_id = str(uuid.uuid4())
    now = datetime.now(UTC).replace(tzinfo=None)  # Neo4j datetime() has no tz
    expires_at: datetime | None = None
    if req.expires_in_days:
        expires_at = now + timedelta(days=req.expires_in_days)

    async with get_session(write=True) as session:
        await session.run(
            CREATE_API_KEY_NODE,
            id=key_id,
            prefix=prefix,
            key_hash=key_hash,
            name=req.name,
            entity_key=req.entity_key,
            scopes=",".join(req.scopes),
            created_by=actor.email,
            created_at=now.isoformat(),
            expires_at=expires_at.isoformat() if expires_at else None,
        )

    return ApiKeyCreatedResponse(
        id=key_id,
        key=plain_key,
        key_prefix=prefix,
        name=req.name,
        scopes=req.scopes,
        expires_at=expires_at,
    )


async def list_api_keys() -> list[ApiKey]:
    """Return all non-revoked API keys (without secrets)."""
    async with get_session() as session:
        result = await session.run(GET_API_KEYS_FOR_ADMIN)
        keys: list[ApiKey] = []
        async for record in result:
            try:
                keys.append(_api_key_from_record(record["key"]))
            except (ValueError, KeyError):
                continue
        return keys


async def revoke_api_key(key_id: str) -> bool:
    """Soft-revoke an API key. Adds prefix to Redis and marks revoked in Neo4j."""
    async with get_session() as session:
        result = await session.run(GET_API_KEY_BY_ID, id=key_id)
        record = await result.single()
        if record is None:
            return False
        key_obj = _api_key_from_record(record["key"])

    from src.redis_client import get_redis  # local import to avoid circular dep

    redis = await get_redis()
    await redis.sadd(_REVOKED_SET_KEY, key_obj.key_prefix)  # type: ignore[misc]
    # TTL = key expiry or 1 year fallback
    if key_obj.expires_at:
        now = datetime.now(UTC).replace(tzinfo=None)
        ttl = int((key_obj.expires_at - now).total_seconds())
        if ttl > 0:
            await redis.expire(_REVOKED_SET_KEY, ttl)
    else:
        await redis.expire(_REVOKED_SET_KEY, 86400 * 365)

    async with get_session(write=True) as session:
        await session.run(REVOKE_API_KEY, id=key_id)
    return True


async def delete_api_key(key_id: str) -> bool:
    """Permanently delete an API key node."""
    async with get_session(write=True) as session:
        await session.run(DELETE_API_KEY, id=key_id)
    return True


async def validate_api_key(plain_key: str) -> tuple[ApiKey, list[str]] | None:
    """Validate an API key. Returns (ApiKey, scopes) or None.

    Checks, in order:
      1. Prefix not in Redis revocation set
      2. Hash found in Neo4j
      3. Not expired
      4. Not soft-revoked in Neo4j

    Fires off an async last_used_at update on success.
    """
    from src.redis_client import get_redis  # local to avoid circular import

    prefix = plain_key[:10]

    # Fast-path rejection via Redis
    redis = await get_redis()
    if await redis.sismember(_REVOKED_SET_KEY, prefix):  # type: ignore[misc]
        return None

    # Look up by hash
    key_hash = _hash_key(plain_key)
    async with get_session() as session:
        result = await session.run(GET_API_KEY_BY_PREFIX_HASH, key_hash=key_hash)
        record = await result.single()
        if record is None:
            return None
        key_obj = _api_key_from_record(record["key"])

    # Expiry and revocation checks
    if key_obj.is_revoked:
        return None
    if key_obj.expires_at:
        expires_at = key_obj.expires_at
        if expires_at.tzinfo is not None:
            expires_at = expires_at.replace(tzinfo=None)
        now = datetime.now(UTC).replace(tzinfo=None)
        if now > expires_at:
            return None

    # Fire-and-forget last_used_at touch
    import asyncio

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_update_last_used(key_obj.id))
    except RuntimeError:
        pass  # no running loop — skip

    return key_obj, key_obj.scopes


async def _update_last_used(key_id: str) -> None:
    """Touch last_used_at after successful validation."""
    try:
        async with get_session(write=True) as session:
            await session.run(UPDATE_API_KEY_LAST_USED, id=key_id)
    except Exception:  # noqa: BLE001
        pass


def check_scope(scopes: list[str], required: str) -> bool:
    """Return True if scopes include *required* or *admin* (super-set)."""
    return "admin" in scopes or required in scopes
