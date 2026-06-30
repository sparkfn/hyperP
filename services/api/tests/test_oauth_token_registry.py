from __future__ import annotations

import time

import pytest
from fakeredis.aioredis import FakeRedis

from src.auth import oauth_token_registry as reg


@pytest.fixture()
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    client = FakeRedis(decode_responses=True)

    async def _get_redis() -> FakeRedis:
        return client

    monkeypatch.setattr(reg, "get_redis", _get_redis)
    return client


@pytest.mark.asyncio
async def test_register_then_list_returns_token(fake_redis: FakeRedis) -> None:
    now = int(time.time())
    await reg.register_token(
        jti="t1", client_id="hpc_a", secret_id="sec_1",
        scope="persons:read", issued_at=now, expires_at=now + 600,
    )
    tokens = await reg.list_client_tokens("hpc_a")
    assert len(tokens) == 1
    assert tokens[0].jti == "t1"
    assert tokens[0].secret_id == "sec_1"
    assert tokens[0].last_used_ip is None


@pytest.mark.asyncio
async def test_touch_updates_last_used(fake_redis: FakeRedis) -> None:
    now = int(time.time())
    await reg.register_token(jti="t1", client_id="hpc_a", secret_id="sec_1",
                             scope="persons:read", issued_at=now, expires_at=now + 600)
    await reg.touch_token("t1", last_used_ip="203.0.113.5", last_used_at=now + 5)
    tokens = await reg.list_client_tokens("hpc_a")
    assert tokens[0].last_used_ip == "203.0.113.5"
    assert tokens[0].last_used_at == now + 5


@pytest.mark.asyncio
async def test_revoke_removes_from_list(fake_redis: FakeRedis) -> None:
    now = int(time.time())
    await reg.register_token(jti="t1", client_id="hpc_a", secret_id="sec_1",
                             scope="persons:read", issued_at=now, expires_at=now + 600)
    await reg.revoke_token_entry("hpc_a", "t1")
    assert await reg.list_client_tokens("hpc_a") == []


@pytest.mark.asyncio
async def test_clear_client_tokens_purges_only_that_client(fake_redis: FakeRedis) -> None:
    now = int(time.time())
    await reg.register_token(jti="a1", client_id="hpc_a", secret_id="sec_1",
                             scope="persons:read", issued_at=now, expires_at=now + 600)
    await reg.register_token(jti="a2", client_id="hpc_a", secret_id="sec_1",
                             scope="persons:read", issued_at=now, expires_at=now + 600)
    await reg.register_token(jti="b1", client_id="hpc_b", secret_id="sec_9",
                             scope="persons:read", issued_at=now, expires_at=now + 600)

    removed = await reg.clear_client_tokens("hpc_a")

    assert removed == 2
    assert await reg.list_client_tokens("hpc_a") == []
    # The hashes are gone too, not just the set membership.
    assert not await fake_redis.exists("oauth_token:a1")
    assert not await fake_redis.exists("oauth_token:a2")
    # A different client's tokens are untouched.
    assert [t.jti for t in await reg.list_client_tokens("hpc_b")] == ["b1"]


@pytest.mark.asyncio
async def test_clear_all_wipes_registry(fake_redis: FakeRedis) -> None:
    now = int(time.time())
    await reg.register_token(jti="t1", client_id="hpc_a", secret_id="sec_1",
                             scope="persons:read", issued_at=now, expires_at=now + 600)
    await reg.clear_all_tokens()
    assert await reg.list_client_tokens("hpc_a") == []


@pytest.mark.asyncio
async def test_expired_token_pruned_from_list_and_set(fake_redis: FakeRedis) -> None:
    now = int(time.time())
    await reg.register_token(jti="old", client_id="hpc_a", secret_id="sec_1",
                             scope="persons:read", issued_at=now - 1000, expires_at=now + 600)
    # Simulate the token having passed its expiry (a key Redis hasn't reaped yet).
    await fake_redis.hset("oauth_token:old", "expires_at", str(now - 10))

    assert await reg.list_client_tokens("hpc_a") == []
    assert not await fake_redis.exists("oauth_token:old")
    assert not await fake_redis.sismember("oauth_client_tokens:hpc_a", "old")


@pytest.mark.asyncio
async def test_stale_jti_pruned_when_hash_missing(fake_redis: FakeRedis) -> None:
    await fake_redis.sadd("oauth_client_tokens:hpc_a", "ghost")
    assert await reg.list_client_tokens("hpc_a") == []
    # fakeredis returns int 0/1 for SISMEMBER; the stale member must be pruned.
    assert not await fake_redis.sismember("oauth_client_tokens:hpc_a", "ghost")
