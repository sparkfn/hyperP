"""Tests for the per-jti Redis revocation store."""

from __future__ import annotations

import time

import pytest
from fakeredis.aioredis import FakeRedis

from src.auth import revoke


@pytest.fixture()
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    client = FakeRedis(decode_responses=True)

    async def _get_redis() -> FakeRedis:
        return client

    monkeypatch.setattr(revoke, "get_redis", _get_redis)
    return client


@pytest.mark.asyncio
async def test_revoking_earlier_token_does_not_unrevoke_later(fake_redis: FakeRedis) -> None:
    now = int(time.time())
    await revoke.revoke_token("late", now + 3600)
    await revoke.revoke_token("early", now + 5)

    assert await revoke.is_token_revoked("late") is True
    assert await revoke.is_token_revoked("early") is True

    # The fix's guarantee: each jti has its own key with its own TTL, so the
    # short-lived "early" revocation cannot evict the long-lived "late" one.
    # (Under the old shared-set design both shared one key whose TTL was
    # overwritten to now+5.) Assert the keys are independent with distinct TTLs.
    late_ttl = await fake_redis.ttl("revoked:late")
    early_ttl = await fake_redis.ttl("revoked:early")
    assert late_ttl > early_ttl
    assert late_ttl > 60


@pytest.mark.asyncio
async def test_unknown_jti_not_revoked(fake_redis: FakeRedis) -> None:
    assert await revoke.is_token_revoked("never-seen") is False
