"""Redis-backed registry of live machine OAuth access tokens.

Each issued token is recorded as a hash `oauth_token:{jti}` with a TTL equal
to the token's remaining lifetime, plus membership in the per-client set
`oauth_client_tokens:{client_id}`. TTL expiry auto-cleans the hash; the set is
pruned lazily on read. Used for admin token tracking and manual revocation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from src.redis_client import get_redis

_TOKEN_PREFIX = "oauth_token:"
_CLIENT_SET_PREFIX = "oauth_client_tokens:"


@dataclass(frozen=True, slots=True)
class TokenRecord:
    """One live access token's tracked metadata."""

    jti: str
    client_id: str
    secret_id: str
    scope: str
    issued_at: int
    expires_at: int
    last_used_at: int | None
    last_used_ip: str | None


def _now() -> int:
    return int(time.time())


async def register_token(
    *, jti: str, client_id: str, secret_id: str, scope: str, issued_at: int, expires_at: int
) -> None:
    """Record a freshly issued token. TTL = remaining lifetime."""
    ttl = expires_at - _now()
    if ttl <= 0:
        return
    client = await get_redis()
    key = f"{_TOKEN_PREFIX}{jti}"
    await client.hset(  # type: ignore[misc]
        key,
        mapping={
            "client_id": client_id,
            "secret_id": secret_id,
            "scope": scope,
            "issued_at": str(issued_at),
            "expires_at": str(expires_at),
            "last_used_at": "",
            "last_used_ip": "",
        },
    )
    await client.expire(key, ttl)
    await client.sadd(f"{_CLIENT_SET_PREFIX}{client_id}", jti)  # type: ignore[misc]


async def touch_token(
    jti: str, *, last_used_ip: str | None, last_used_at: int | None = None
) -> None:
    """Best-effort update of last-used metadata; no-op if the hash expired."""
    client = await get_redis()
    key = f"{_TOKEN_PREFIX}{jti}"
    if not await client.exists(key):
        return
    await client.hset(  # type: ignore[misc]
        key,
        mapping={
            "last_used_at": str(last_used_at if last_used_at is not None else _now()),
            "last_used_ip": last_used_ip or "",
        },
    )


async def list_client_tokens(client_id: str) -> list[TokenRecord]:
    """Return the client's live tokens, pruning expired and stale set members.

    A jti is dropped when its hash is gone (TTL-collected) or its `expires_at`
    is in the past — the latter guards against a key Redis has not lazily
    reaped yet, so the set does not accumulate dead members between mints.
    """
    client = await get_redis()
    now = _now()
    set_key = f"{_CLIENT_SET_PREFIX}{client_id}"
    jtis: set[str] = set(await client.smembers(set_key))  # type: ignore[misc]
    records: list[TokenRecord] = []
    for jti in jtis:
        data: dict[str, str] = await client.hgetall(f"{_TOKEN_PREFIX}{jti}")  # type: ignore[misc]
        record = _to_record(jti, data) if data else None
        if record is None or record.expires_at <= now:
            await client.srem(set_key, jti)  # type: ignore[misc]
            if record is not None:
                await client.delete(f"{_TOKEN_PREFIX}{jti}")
            continue
        records.append(record)
    records.sort(key=lambda r: r.issued_at, reverse=True)
    return records


async def revoke_token_entry(client_id: str, jti: str) -> bool:
    """Remove a token from the registry. Returns True if it existed."""
    client = await get_redis()
    existed = bool(await client.exists(f"{_TOKEN_PREFIX}{jti}"))
    await client.delete(f"{_TOKEN_PREFIX}{jti}")
    await client.srem(f"{_CLIENT_SET_PREFIX}{client_id}", jti)  # type: ignore[misc]
    return existed


async def clear_client_tokens(client_id: str) -> int:
    """Delete every registry entry for one client. Returns how many were removed.

    Used on secret rotation: rotation invalidates all outstanding tokens via the
    secret_id kill-switch, so leaving their registry hashes alive until TTL would
    make the admin token list advertise tokens that can no longer authenticate.
    """
    client = await get_redis()
    set_key = f"{_CLIENT_SET_PREFIX}{client_id}"
    jtis: set[str] = set(await client.smembers(set_key))  # type: ignore[misc]
    for jti in jtis:
        await client.delete(f"{_TOKEN_PREFIX}{jti}")
    await client.delete(set_key)
    return len(jtis)


async def clear_all_tokens() -> None:
    """Delete every OAuth token registry key (used by the wipe migration)."""
    client = await get_redis()
    async for key in client.scan_iter(match=f"{_TOKEN_PREFIX}*"):
        await client.delete(key)
    async for key in client.scan_iter(match=f"{_CLIENT_SET_PREFIX}*"):
        await client.delete(key)


def _to_record(jti: str, data: dict[str, str]) -> TokenRecord:
    return TokenRecord(
        jti=jti,
        client_id=data.get("client_id", ""),
        secret_id=data.get("secret_id", ""),
        scope=data.get("scope", ""),
        issued_at=int(data.get("issued_at") or 0),
        expires_at=int(data.get("expires_at") or 0),
        last_used_at=int(data["last_used_at"]) if data.get("last_used_at") else None,
        last_used_ip=data.get("last_used_ip") or None,
    )
