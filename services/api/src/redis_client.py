"""Async Redis client backed by redis.asyncio. Used for token revocation."""

from __future__ import annotations

import redis.asyncio as redis

_client: redis.Redis | None = None


async def get_redis() -> redis.Redis:
    """Return the shared async Redis connection."""
    global _client
    if _client is None:
        from src.config import config

        url = config.redis_url
        _client = redis.from_url(url, decode_responses=True)  # type: ignore[no-untyped-call]
    return _client


async def close_redis() -> None:
    """Close the Redis connection pool."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
