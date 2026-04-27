"""Redis-backed token revocation store.

Tokens are stored in a Redis SET (`revoked_tokens`) keyed by the token's
`jti` (JWT ID) claim. Each entry is set with a TTL equal to the token's
absolute expiry timestamp so it self-cleans exactly when the token would
have expired anyway.
"""

from __future__ import annotations

import base64
import json
import logging
import time

from src.redis_client import get_redis

log = logging.getLogger(__name__)

_REVOKED_SET = "revoked_tokens"


async def revoke_token(jti: str, exp: int) -> None:
    """Add a token's jti to the revocation list with TTL = exp (Unix timestamp)."""
    client = await get_redis()
    ttl = exp - _now()
    if ttl <= 0:
        # Token is already expired — nothing to revoke.
        return
    await client.sadd(_REVOKED_SET, jti)  # type: ignore[misc]
    await client.expireat(_REVOKED_SET, exp)
    log.info("Revoked token jti=%s, expires in %d s", jti, ttl)


async def is_token_revoked(jti: str) -> bool:
    """Return True if the jti is in the revocation list."""
    client = await get_redis()
    return bool(await client.sismember(_REVOKED_SET, jti))  # type: ignore[misc]


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
