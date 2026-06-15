"""Redis-backed token revocation store.

Each revoked token is stored under its own per-jti key (`revoked:{jti}`)
with a TTL equal to the token's absolute expiry timestamp, so it self-cleans
exactly when the token would have expired anyway. Per-jti keys avoid the
shared-set pitfall where setting the set's TTL to a sooner-expiring token's
expiry would prematurely drop revocations for longer-lived tokens.
"""

from __future__ import annotations

import base64
import json
import logging
import time

from src.redis_client import get_redis

log = logging.getLogger(__name__)

_REVOKED_PREFIX = "revoked:"


async def revoke_token(jti: str, exp: int) -> None:
    """Mark a token's jti revoked until its own expiry (per-jti key)."""
    client = await get_redis()
    ttl = exp - _now()
    if ttl <= 0:
        return
    await client.set(f"{_REVOKED_PREFIX}{jti}", "1", exat=exp)
    log.info("Revoked token jti=%s, expires in %d s", jti, ttl)


async def is_token_revoked(jti: str) -> bool:
    """Return True if the jti has a live revocation key."""
    client = await get_redis()
    return bool(await client.exists(f"{_REVOKED_PREFIX}{jti}"))


def _now() -> int:
    return int(time.time())


def decode_jwt_claims(token: str) -> tuple[str | None, int | None]:
    """Decode jti and exp from a raw JWT without verification.

    Returns (jti, exp). Returns (None, None) if the token is malformed.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None, None

        # Add padding as needed for base64url decode
        def _pad(data: str) -> bytes:
            rem = len(data) % 4
            if rem:
                data += "=" * (4 - rem)
            return base64.urlsafe_b64decode(data)

        payload = json.loads(_pad(parts[1]))
        jti: str | None = payload.get("jti")
        exp_raw = payload.get("exp")
        exp: int | None = int(exp_raw) if isinstance(exp_raw, (int, float)) else None
        return jti, exp
    except Exception:  # noqa: BLE001
        return None, None
