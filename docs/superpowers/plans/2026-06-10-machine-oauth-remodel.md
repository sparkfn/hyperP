# Machine OAuth2 Remodel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remodel machine OAuth2 so each client has a single active secret (rotation revokes the previous and kills its tokens), per-client access-token TTL, and a Redis-backed token registry that admins can inspect (with last-used IP) and revoke.

**Architecture:** Access tokens carry a `secret_id` claim; verification rejects any token whose `secret_id` is not the client's current active secret (the rotation kill-switch). A Redis registry (`oauth_token:{jti}` hashes + `oauth_client_tokens:{client_id}` sets) tracks live tokens with TTL auto-cleanup. Existing OAuth nodes are wiped at startup (no backfill). Two adjacent auth bugs (shared-set revocation TTL, cache token comparison) are fixed along the way.

**Tech Stack:** FastAPI, Neo4j (Cypher), Redis (`redis.asyncio`), Pydantic v2, uv/pytest; Next.js 15 / React / TypeScript (frontend2).

**Reference spec:** `docs/superpowers/specs/2026-06-10-machine-oauth-remodel-design.md`

**Conventions for every task:**
- Run lint/type for touched Python: `uv run --package profile-unifier-api ruff check services/api/src` and `uv run --package profile-unifier-api mypy --strict services/api/src`.
- Run tests: `uv run pytest services/api/tests/<file> -v`.
- **Do not commit unless the user has explicitly authorized it** (project commit discipline). The "Commit" steps below stage a suggested message; only run them when the user says to commit. Otherwise leave changes staged/unstaged and move on.

---

## Task 1: Fix Redis revocation store — one key per jti

**Files:**
- Modify: `services/api/src/auth/revoke.py`
- Test: `services/api/tests/test_revoke_store.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/test_revoke_store.py
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


@pytest.mark.asyncio
async def test_unknown_jti_not_revoked(fake_redis: FakeRedis) -> None:
    assert await revoke.is_token_revoked("never-seen") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/api/tests/test_revoke_store.py -v`
Expected: FAIL (current implementation uses a single shared set; `late` may report revoked but the test pins the regression — if `fakeredis` is missing, add it: `uv add --package profile-unifier-api --dev fakeredis`).

- [ ] **Step 3: Rewrite the store to per-jti keys**

Replace the body of `services/api/src/auth/revoke.py` revocation functions:

```python
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
```

Delete the `_REVOKED_SET` constant and the `# type: ignore[misc]` SADD/sismember lines.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest services/api/tests/test_revoke_store.py -v`
Expected: PASS

- [ ] **Step 5: Run the existing logout test to confirm no regression**

Run: `uv run pytest services/api/tests -k "revoke or logout" -v`
Expected: PASS (update any test that asserted on the old `revoked_tokens` set name).

- [ ] **Step 6: Commit (only if authorized)**

```bash
git add services/api/src/auth/revoke.py services/api/tests/test_revoke_store.py
git commit -m "fix(auth): per-jti revocation keys to stop un-revocation on shared TTL"
```

---

## Task 2: Fix `_USER_CACHE` token comparison

**Files:**
- Modify: `services/api/src/auth/deps.py:84-86`
- Test: `services/api/tests/test_user_cache.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/test_user_cache.py
"""The Google-token cache must not serve a different token sharing a jti."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from src.auth import deps
from src.auth.models import AuthUser


@pytest.mark.asyncio
async def test_cache_miss_when_token_differs_for_same_jti(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps.config, "auth_enabled", True)
    deps._USER_CACHE.clear()

    cached_user = AuthUser(email="victim@x", google_sub="v", role="admin", entity_key=None, display_name="V")
    deps._USER_CACHE["shared-jti"] = ("real-token", cached_user)

    async def _not_revoked(_jti: str) -> bool:
        return False

    def _claims(_token: str) -> tuple[str | None, int | None]:
        return "shared-jti", None

    async def _verify_fails(_token: str) -> object:
        raise ValueError("bad signature")

    monkeypatch.setattr(deps, "is_token_revoked", _not_revoked)
    monkeypatch.setattr(deps, "decode_jwt_claims", _claims)
    monkeypatch.setattr(deps, "verify_google_id_token", _verify_fails)

    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="forged-token")
    request = _make_request()
    with pytest.raises(HTTPException):
        await deps.get_current_user(request, creds)


def _make_request() -> object:
    from fastapi import Request

    return Request({"type": "http", "headers": []})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/api/tests/test_user_cache.py -v`
Expected: FAIL — the forged token is served from cache (no exception raised).

- [ ] **Step 3: Compare the stored raw token before returning cached user**

In `services/api/src/auth/deps.py`, change the cache read in `get_current_user`:

```python
    cache_key = jti if jti is not None else token
    cached = _USER_CACHE.get(cache_key)
    if cached is not None and cached[0] == token:
        return cached[1]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest services/api/tests/test_user_cache.py -v`
Expected: PASS

- [ ] **Step 5: Commit (only if authorized)**

```bash
git add services/api/src/auth/deps.py services/api/tests/test_user_cache.py
git commit -m "fix(auth): compare cached raw token to close jti-collision bypass"
```

---

## Task 3: `client_ip` helper

**Files:**
- Modify: `services/api/src/http_utils.py`
- Test: `services/api/tests/test_client_ip.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/test_client_ip.py
from __future__ import annotations

from fastapi import Request

from src.http_utils import client_ip


def _request(headers: list[tuple[bytes, bytes]], client: tuple[str, int] | None) -> Request:
    scope: dict[str, object] = {"type": "http", "headers": headers}
    if client is not None:
        scope["client"] = client
    return Request(scope)


def test_uses_leftmost_xff() -> None:
    req = _request([(b"x-forwarded-for", b"203.0.113.5, 10.0.0.1")], ("10.0.0.1", 0))
    assert client_ip(req) == "203.0.113.5"


def test_falls_back_to_socket_host() -> None:
    req = _request([], ("198.51.100.9", 0))
    assert client_ip(req) == "198.51.100.9"


def test_returns_none_when_unknown() -> None:
    req = _request([], None)
    assert client_ip(req) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/api/tests/test_client_ip.py -v`
Expected: FAIL — `client_ip` not defined.

- [ ] **Step 3: Implement the helper**

Add to `services/api/src/http_utils.py`:

```python
def client_ip(request: Request) -> str | None:
    """Return the originating client IP, trusting nginx's X-Forwarded-For.

    nginx forwards X-Forwarded-For on the /api/oauth2/ block; the left-most
    entry is the original client. Falls back to the direct socket host.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest services/api/tests/test_client_ip.py -v`
Expected: PASS

- [ ] **Step 5: Commit (only if authorized)**

```bash
git add services/api/src/http_utils.py services/api/tests/test_client_ip.py
git commit -m "feat(http): client_ip helper reading X-Forwarded-For"
```

---

## Task 4: Token registry module (Redis)

**Files:**
- Create: `services/api/src/auth/oauth_token_registry.py`
- Test: `services/api/tests/test_oauth_token_registry.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/test_oauth_token_registry.py
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
async def test_clear_all_wipes_registry(fake_redis: FakeRedis) -> None:
    now = int(time.time())
    await reg.register_token(jti="t1", client_id="hpc_a", secret_id="sec_1",
                             scope="persons:read", issued_at=now, expires_at=now + 600)
    await reg.clear_all_tokens()
    assert await reg.list_client_tokens("hpc_a") == []


@pytest.mark.asyncio
async def test_stale_jti_pruned_when_hash_missing(fake_redis: FakeRedis) -> None:
    await fake_redis.sadd("oauth_client_tokens:hpc_a", "ghost")
    assert await reg.list_client_tokens("hpc_a") == []
    assert await fake_redis.sismember("oauth_client_tokens:hpc_a", "ghost") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/api/tests/test_oauth_token_registry.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the registry**

```python
# services/api/src/auth/oauth_token_registry.py
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


async def touch_token(jti: str, *, last_used_ip: str | None, last_used_at: int | None = None) -> None:
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
    """Return the client's live tokens, pruning stale set members."""
    client = await get_redis()
    set_key = f"{_CLIENT_SET_PREFIX}{client_id}"
    jtis: set[str] = set(await client.smembers(set_key))  # type: ignore[misc]
    records: list[TokenRecord] = []
    for jti in jtis:
        data: dict[str, str] = await client.hgetall(f"{_TOKEN_PREFIX}{jti}")  # type: ignore[misc]
        if not data:
            await client.srem(set_key, jti)  # type: ignore[misc]
            continue
        records.append(_to_record(jti, data))
    records.sort(key=lambda r: r.issued_at, reverse=True)
    return records


async def revoke_token_entry(client_id: str, jti: str) -> bool:
    """Remove a token from the registry. Returns True if it existed."""
    client = await get_redis()
    existed = bool(await client.exists(f"{_TOKEN_PREFIX}{jti}"))
    await client.delete(f"{_TOKEN_PREFIX}{jti}")
    await client.srem(f"{_CLIENT_SET_PREFIX}{client_id}", jti)  # type: ignore[misc]
    return existed


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest services/api/tests/test_oauth_token_registry.py -v`
Expected: PASS

- [ ] **Step 5: Type-check and commit (only if authorized)**

Run: `uv run --package profile-unifier-api mypy --strict services/api/src/auth/oauth_token_registry.py`
```bash
git add services/api/src/auth/oauth_token_registry.py services/api/tests/test_oauth_token_registry.py
git commit -m "feat(auth): Redis token registry for live machine OAuth tokens"
```

---

## Task 5: Model changes (`oauth_client_models.py`)

**Files:**
- Modify: `services/api/src/auth/oauth_client_models.py`
- Test: `services/api/tests/test_oauth_client_models.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/test_oauth_client_models.py
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.auth.oauth_client_models import (
    ACCESS_TOKEN_TTL_MAX_SECONDS,
    ACCESS_TOKEN_TTL_MIN_SECONDS,
    CreateOAuthClientRequest,
    OAuthAccessTokenView,
    UpdateOAuthClientRequest,
)


def test_create_request_accepts_ttl_in_bounds() -> None:
    req = CreateOAuthClientRequest(name="x", scopes=["persons:read"], access_token_ttl_seconds=900)
    assert req.access_token_ttl_seconds == 900


def test_create_request_rejects_ttl_below_min() -> None:
    with pytest.raises(ValidationError):
        CreateOAuthClientRequest(name="x", scopes=["persons:read"],
                                 access_token_ttl_seconds=ACCESS_TOKEN_TTL_MIN_SECONDS - 1)


def test_create_request_rejects_ttl_above_max() -> None:
    with pytest.raises(ValidationError):
        CreateOAuthClientRequest(name="x", scopes=["persons:read"],
                                 access_token_ttl_seconds=ACCESS_TOKEN_TTL_MAX_SECONDS + 1)


def test_update_request_all_optional() -> None:
    req = UpdateOAuthClientRequest()
    assert req.name is None and req.scopes is None and req.access_token_ttl_seconds is None


def test_access_token_view_shape() -> None:
    view = OAuthAccessTokenView(jti="t1", scope="persons:read", issued_at=1, expires_at=2,
                                last_used_at=None, last_used_ip=None)
    assert view.jti == "t1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/api/tests/test_oauth_client_models.py -v`
Expected: FAIL — new names not defined.

- [ ] **Step 3: Apply model changes**

In `services/api/src/auth/oauth_client_models.py`:

Add bounds constants near the scope constants:

```python
ACCESS_TOKEN_TTL_MIN_SECONDS = 300
ACCESS_TOKEN_TTL_MAX_SECONDS = 86400
DEFAULT_ACCESS_TOKEN_TTL_SECONDS = 900
```

`OAuthClientSecret`: delete the `expires_at` field.

`OAuthClient`: add `access_token_ttl_seconds: int = DEFAULT_ACCESS_TOKEN_TTL_SECONDS`.

`CreateOAuthClientRequest`: remove `secret_expires_in_days`; add

```python
    access_token_ttl_seconds: int = Field(
        default=DEFAULT_ACCESS_TOKEN_TTL_SECONDS,
        ge=ACCESS_TOKEN_TTL_MIN_SECONDS,
        le=ACCESS_TOKEN_TTL_MAX_SECONDS,
    )
```

Add new request + view models:

```python
class UpdateOAuthClientRequest(BaseModel):
    """Patch an existing OAuth client. All fields optional."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    scopes: list[str] | None = None
    access_token_ttl_seconds: int | None = Field(
        default=None, ge=ACCESS_TOKEN_TTL_MIN_SECONDS, le=ACCESS_TOKEN_TTL_MAX_SECONDS
    )

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, scopes: list[str] | None) -> list[str] | None:
        """Validate supported, unique, non-blank scopes when provided."""
        return None if scopes is None else validate_oauth_client_scopes(scopes)


class OAuthAccessTokenView(BaseModel):
    """One live access token's tracked metadata for the admin UI."""

    jti: str
    scope: str
    issued_at: int
    expires_at: int
    last_used_at: int | None = None
    last_used_ip: str | None = None
```

Remove `secret_expires_at` from `OAuthClientCreatedResponse` (rotation/creation no longer expire secrets); delete `CreateOAuthClientSecretRequest` and `OAuthClientSecretCreatedResponse` only after Task 7 stops importing them — for now, add a new rotation response:

```python
class RotateSecretResponse(BaseModel):
    """One-time plaintext for a rotated secret."""

    client_id: str
    client_secret: str
    secret_id: str
    secret_prefix: str
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest services/api/tests/test_oauth_client_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit (only if authorized)**

```bash
git add services/api/src/auth/oauth_client_models.py services/api/tests/test_oauth_client_models.py
git commit -m "feat(auth): per-client token TTL model, drop secret expiry, add token view"
```

---

## Task 6: Cypher queries (`graph/queries/oauth_clients.py`)

**Files:**
- Modify: `services/api/src/graph/queries/oauth_clients.py`
- Test: `services/api/tests/test_oauth_client_queries.py` (create — string-shape assertions)

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/test_oauth_client_queries.py
from __future__ import annotations

from src.graph.queries.oauth_clients import (
    CREATE_OAUTH_CLIENT_WITH_SECRET,
    GET_OAUTH_CLIENT_FOR_VALIDATION,
    ROTATE_OAUTH_CLIENT_SECRET,
    UPDATE_OAUTH_CLIENT,
    WIPE_OAUTH_CLIENTS,
)


def test_create_sets_ttl_and_no_secret_expiry() -> None:
    assert "access_token_ttl_seconds" in CREATE_OAUTH_CLIENT_WITH_SECRET
    assert "secret_expires_at" not in CREATE_OAUTH_CLIENT_WITH_SECRET
    assert "expires_at" not in CREATE_OAUTH_CLIENT_WITH_SECRET


def test_validation_query_only_returns_active_secret() -> None:
    assert "revoked_at IS NULL" in GET_OAUTH_CLIENT_FOR_VALIDATION


def test_rotate_revokes_previous_and_creates_new() -> None:
    assert "revoked_at = datetime()" in ROTATE_OAUTH_CLIENT_SECRET
    assert "CREATE (s:OAuthClientSecret" in ROTATE_OAUTH_CLIENT_SECRET


def test_update_sets_ttl() -> None:
    assert "access_token_ttl_seconds" in UPDATE_OAUTH_CLIENT


def test_wipe_detaches_clients_and_secrets() -> None:
    assert "DETACH DELETE" in WIPE_OAUTH_CLIENTS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/api/tests/test_oauth_client_queries.py -v`
Expected: FAIL — new query constants undefined.

- [ ] **Step 3: Edit the query module**

In `CREATE_OAUTH_CLIENT_WITH_SECRET`: add `c.access_token_ttl_seconds = $access_token_ttl_seconds,` to the `ON CREATE SET`; remove the secret `expires_at` line (the `CASE WHEN $secret_expires_at...` clause), leaving the secret without an expiry property.

In every projection (`GET_OAUTH_CLIENTS_FOR_ADMIN`, `GET_OAUTH_CLIENT_BY_ID`, `GET_OAUTH_CLIENT_FOR_VALIDATION`): add `.access_token_ttl_seconds` to the `c {...}` projection and remove `.expires_at` from each secret projection.

Change `GET_OAUTH_CLIENT_FOR_VALIDATION` to return only the active secret:

```python
GET_OAUTH_CLIENT_FOR_VALIDATION = """
MATCH (c:OAuthClient {client_id: $client_id})-[:HAS_SECRET]->(s:OAuthClientSecret)
WHERE s.revoked_at IS NULL
RETURN c {
  .client_id, .name, .entity_key, .scopes, .created_by,
  .created_at, .disabled_at, .last_used_at, .access_token_ttl_seconds,
  secret: s {
    .secret_id, .secret_hash, .secret_prefix, .created_at,
    .revoked_at, .last_used_at
  }
} AS client
"""
```

Add new constants:

```python
ROTATE_OAUTH_CLIENT_SECRET = """
MATCH (c:OAuthClient {client_id: $client_id})
WHERE c.disabled_at IS NULL
OPTIONAL MATCH (c)-[:HAS_SECRET]->(old:OAuthClientSecret)
WHERE old.revoked_at IS NULL
SET old.revoked_at = datetime()
CREATE (s:OAuthClientSecret {
  secret_id: $secret_id,
  secret_hash: $secret_hash,
  secret_prefix: $secret_prefix,
  created_at: datetime($created_at),
  revoked_at: NULL,
  last_used_at: NULL
})
CREATE (c)-[:HAS_SECRET]->(s)
RETURN s.secret_id AS secret_id
"""

UPDATE_OAUTH_CLIENT = """
MATCH (c:OAuthClient {client_id: $client_id})
SET c.name = coalesce($name, c.name),
    c.scopes = coalesce($scopes, c.scopes),
    c.access_token_ttl_seconds = coalesce($access_token_ttl_seconds, c.access_token_ttl_seconds)
RETURN c.client_id AS client_id
"""

WIPE_OAUTH_CLIENTS = """
MATCH (c:OAuthClient)
OPTIONAL MATCH (c)-[:HAS_SECRET]->(s:OAuthClientSecret)
DETACH DELETE s, c
"""
```

Remove `CREATE_OAUTH_CLIENT_SECRET` and `REVOKE_OAUTH_CLIENT_SECRET` (superseded by rotation). Keep `DISABLE_OAUTH_CLIENT`, `DELETE_OAUTH_CLIENT`, `UPDATE_OAUTH_CLIENT_LAST_USED`, `UPDATE_OAUTH_SECRET_LAST_USED`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest services/api/tests/test_oauth_client_queries.py -v`
Expected: PASS

- [ ] **Step 5: Commit (only if authorized)**

```bash
git add services/api/src/graph/queries/oauth_clients.py services/api/tests/test_oauth_client_queries.py
git commit -m "feat(auth): single-active-secret Cypher, rotation, client update, wipe"
```

---

## Task 7: Service layer (`auth/oauth_clients.py`)

**Files:**
- Modify: `services/api/src/auth/oauth_clients.py`
- Test: `services/api/tests/test_oauth_clients.py` (rewrite affected tests)

- [ ] **Step 1: Write/replace the failing tests**

Replace the secret-expiry and multi-secret tests with single-secret + rotation behavior. Add:

```python
# in services/api/tests/test_oauth_clients.py
@pytest.mark.asyncio
async def test_rotate_secret_revokes_previous_and_returns_new() -> None:
    fake = _FakeSession(single={"secret_id": "sec_2"})
    with patch.object(oauth_clients, "get_session", return_value=_ctx(fake)):
        result = await oauth_clients.rotate_oauth_client_secret("hpc_a")
    assert result is not None
    assert result.client_id == "hpc_a"
    assert result.client_secret.startswith("hps_")
    assert result.secret_id == "sec_2"


@pytest.mark.asyncio
async def test_rotate_secret_returns_none_for_missing_or_disabled_client() -> None:
    fake = _FakeSession(single=None)
    with patch.object(oauth_clients, "get_session", return_value=_ctx(fake)):
        assert await oauth_clients.rotate_oauth_client_secret("ghost") is None


@pytest.mark.asyncio
async def test_update_oauth_client_returns_false_for_missing() -> None:
    fake = _FakeSession(single=None)
    with patch.object(oauth_clients, "get_session", return_value=_ctx(fake)):
        from src.auth.oauth_client_models import UpdateOAuthClientRequest
        assert await oauth_clients.update_oauth_client("ghost", UpdateOAuthClientRequest(name="y")) is False
```

> Note: reuse the file's existing fake-session helpers; if the file lacks `_FakeSession`/`_ctx`, mirror the existing mocking style used by `test_create_oauth_client_secret_returns_one_time_secret`. Delete `test_validate_client_credentials_rejects_expired_secret` and any `secret_expires_in_days` assertions.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest services/api/tests/test_oauth_clients.py -v`
Expected: FAIL — `rotate_oauth_client_secret` / `update_oauth_client` undefined; expiry imports broken.

- [ ] **Step 3: Edit the service**

In `services/api/src/auth/oauth_clients.py`:

- Update imports: drop `CREATE_OAUTH_CLIENT_SECRET`, `REVOKE_OAUTH_CLIENT_SECRET`; add `ROTATE_OAUTH_CLIENT_SECRET`, `UPDATE_OAUTH_CLIENT`, `WIPE_OAUTH_CLIENTS`. Drop `CreateOAuthClientSecretRequest`/`OAuthClientSecretCreatedResponse` imports; add `RotateSecretResponse`, `UpdateOAuthClientRequest`.
- `is_secret_usable`: collapse to `return secret.revoked_at is None` (drop the expiry branch and the `now` param usage for expiry; keep signature stable or simplify — update callers/tests accordingly).
- `_secret_from_record`: remove the `expires_at=` line.
- `create_oauth_client`: pass `access_token_ttl_seconds=req.access_token_ttl_seconds`; remove `secret_expires_at`/`expires_at` handling; update `CREATE_OAUTH_CLIENT_WITH_SECRET` call params. `OAuthClientCreatedResponse` no longer sets `secret_expires_at`.
- `_client_from_record`: read `access_token_ttl_seconds` (default to `DEFAULT_ACCESS_TOKEN_TTL_SECONDS` when absent via `to_int`).
- Replace `create_oauth_client_secret` with:

```python
async def rotate_oauth_client_secret(client_id: str) -> RotateSecretResponse | None:
    """Rotate a client's secret: revoke the previous, create a new one."""
    client_secret = generate_client_secret()
    secret_id = f"sec_{uuid.uuid4()}"
    secret_prefix = client_secret[:10]
    secret_hash = hash_client_secret(client_secret)
    now = datetime.now(UTC).replace(tzinfo=None)
    async with get_session(write=True) as session:
        result = await session.run(
            ROTATE_OAUTH_CLIENT_SECRET,
            client_id=client_id,
            secret_id=secret_id,
            secret_hash=secret_hash,
            secret_prefix=secret_prefix,
            created_at=now.isoformat(),
        )
        if await result.single() is None:
            return None
    return RotateSecretResponse(
        client_id=client_id, client_secret=client_secret,
        secret_id=secret_id, secret_prefix=secret_prefix,
    )


async def update_oauth_client(client_id: str, req: UpdateOAuthClientRequest) -> bool:
    """Patch a client's name/scopes/token TTL. Returns False if not found."""
    async with get_session(write=True) as session:
        result = await session.run(
            UPDATE_OAUTH_CLIENT,
            client_id=client_id,
            name=req.name,
            scopes=",".join(req.scopes) if req.scopes is not None else None,
            access_token_ttl_seconds=req.access_token_ttl_seconds,
        )
        return await result.single() is not None


async def wipe_oauth_clients() -> None:
    """Delete all OAuth client + secret nodes (startup wipe migration)."""
    async with get_session(write=True) as session:
        await session.run(WIPE_OAUTH_CLIENTS)
```

- Remove `revoke_oauth_client_secret`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest services/api/tests/test_oauth_clients.py -v`
Expected: PASS

- [ ] **Step 5: Type-check and commit (only if authorized)**

Run: `uv run --package profile-unifier-api mypy --strict services/api/src/auth/oauth_clients.py`
```bash
git add services/api/src/auth/oauth_clients.py services/api/tests/test_oauth_clients.py
git commit -m "feat(auth): secret rotation, client update, wipe; single active secret"
```

---

## Task 8: Token claims — `secret_id` (`auth/oauth_tokens.py`)

**Files:**
- Modify: `services/api/src/auth/oauth_tokens.py`
- Test: `services/api/tests/test_oauth_tokens.py` (add cases)

- [ ] **Step 1: Write the failing test**

```python
# append to services/api/tests/test_oauth_tokens.py
def test_token_carries_and_verifies_secret_id() -> None:
    _configure_oauth_env()  # reuse existing helper that sets keys/issuer/audience
    client = _make_client(client_id="hpc_a")  # reuse existing helper
    token = issue_client_access_token(client, ["persons:read"], secret_id="sec_1", expires_in_seconds=600)
    claims = verify_client_access_token(token)
    assert claims.secret_id == "sec_1"
    assert claims.client_id == "hpc_a"
```

> Reuse whatever env/client helpers already exist in this file (the existing `test_issue_and_verify_client_access_token` shows the pattern). If `issue_client_access_token` is called positionally elsewhere, keep `secret_id` keyword-only.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/api/tests/test_oauth_tokens.py -k secret_id -v`
Expected: FAIL — `secret_id` not accepted / not on claims.

- [ ] **Step 3: Add `secret_id` to claims, payload, issue, verify**

In `services/api/src/auth/oauth_tokens.py`:
- `OAuthClientClaims`: add `secret_id: str` (place before `entity_key`).
- `JwtPayload`: add `secret_id: str`.
- `issue_client_access_token`: add keyword-only param `secret_id: str` and set `payload["secret_id"] = secret_id`.
- `_verified_payload`: add `secret_id=_require_str(raw_payload, "secret_id")`.
- `verify_client_access_token`: pass `secret_id=payload["secret_id"]` into the returned `OAuthClientClaims`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest services/api/tests/test_oauth_tokens.py -v`
Expected: PASS (all token tests).

- [ ] **Step 5: Commit (only if authorized)**

```bash
git add services/api/src/auth/oauth_tokens.py services/api/tests/test_oauth_tokens.py
git commit -m "feat(auth): stamp and verify secret_id claim on machine tokens"
```

---

## Task 9: Verification kill-switch + last-used (`auth/deps.py`)

**Files:**
- Modify: `services/api/src/auth/deps.py:139-187`
- Test: `services/api/tests/test_oauth_routes.py` (add cases)

- [ ] **Step 1: Write the failing test**

```python
# append to services/api/tests/test_oauth_routes.py
@pytest.mark.asyncio
async def test_oauth_token_rejected_when_secret_id_no_longer_current() -> None:
    # _resolve_oauth_principal / _claims / _client are existing helpers in this file.
    with pytest.raises(HTTPException) as exc:
        await _resolve_oauth_principal(
            _claims(client_id="hpc_a", scopes=["persons:read"], entity_key=None, secret_id="sec_OLD"),
            _client(client_id="hpc_a", scopes=["persons:read"], current_secret_id="sec_NEW"),
        )
    assert exc.value.status_code == 401
```

> Update the file's `_claims` helper to accept `secret_id` (default `"sec_1"`) and `_client` to expose the current active secret id (default `"sec_1"`), so existing tests keep passing with matching ids.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/api/tests/test_oauth_routes.py -k secret_id_no_longer_current -v`
Expected: FAIL — no secret_id check yet.

- [ ] **Step 3: Add the checks to `get_current_user_or_oauth_client`**

After the `client.disabled_at` check and before scope reconciliation, add the current-secret check; after building the principal, touch last-used. Concretely:

```python
    if client.disabled_at is not None:
        raise http_error(403, "forbidden", "OAuth client is disabled.", request)

    current_secret_id = _current_secret_id(client)
    if current_secret_id is None or claims.secret_id != current_secret_id:
        raise http_error(
            401, "unauthorized",
            "OAuth client secret has been rotated; token is no longer valid.",
            request,
        )

    reconciled_scopes = _reconciled_oauth_scopes(claims.scopes, client, request)
    reconciled_entity_key = _reconciled_oauth_entity_key(claims.entity_key, client, request)
    await _touch_token_last_used(claims.jti, request)
    role: Role = "admin" if "admin" in reconciled_scopes else "employee"
    return OAuthClientUser(...)  # unchanged
```

Add helpers near the bottom of the OAuth section:

```python
def _current_secret_id(client: OAuthClient) -> str | None:
    active = [s for s in client.secrets if s.revoked_at is None]
    return active[0].secret_id if len(active) == 1 else (active[0].secret_id if active else None)


async def _touch_token_last_used(jti: str, request: Request) -> None:
    try:
        await touch_token(jti, last_used_ip=client_ip(request))
    except Exception:  # noqa: BLE001 — tracking is best-effort
        log.debug("Failed to update OAuth token last-used", exc_info=True)
```

Add imports: `from src.auth.oauth_token_registry import touch_token` and `from src.http_utils import client_ip, http_error`.

> The `is_token_revoked(claims.jti)` check already exists earlier in this function (deps.py:156) — keep it; it now does real work because manual revoke (Task 10) writes the jti.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest services/api/tests/test_oauth_routes.py -v`
Expected: PASS

- [ ] **Step 5: Commit (only if authorized)**

```bash
git add services/api/src/auth/deps.py services/api/tests/test_oauth_routes.py
git commit -m "feat(auth): reject rotated-secret tokens; record last-used IP"
```

---

## Task 10: Token endpoint registers tokens; uses per-client TTL (`routes/oauth.py`)

**Files:**
- Modify: `services/api/src/routes/oauth.py`
- Test: `services/api/tests/test_oauth_routes.py` (extend token endpoint test)

- [ ] **Step 1: Write the failing test**

```python
# append to services/api/tests/test_oauth_routes.py
def test_token_endpoint_uses_client_ttl_and_registers_token(monkeypatch) -> None:
    # Build a client with access_token_ttl_seconds=1800 and a known active secret.
    # Patch validate_client_credentials to return (client, scopes); patch register_token
    # to capture the call. Assert response expires_in == 1800 and register_token called
    # with secret_id == client's active secret id.
    ...
```

> Mirror the existing `test_token_endpoint_issues_access_token` setup (it already drives the FastAPI test client). Use `monkeypatch.setattr` on `src.routes.oauth.validate_client_credentials` and `src.routes.oauth.register_token`. Assert `data["expires_in"] == 1800`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/api/tests/test_oauth_routes.py -k uses_client_ttl -v`
Expected: FAIL — endpoint still uses global config TTL and does not register.

- [ ] **Step 3: Edit the token route**

In `services/api/src/routes/oauth.py`:
- `validate_client_credentials` already returns `(client, scopes)`; the client now carries `access_token_ttl_seconds` and a single active secret. Compute:

```python
    client, assigned_scopes = validated
    granted_scopes = requested_scopes_or_default(scope, assigned_scopes)
    if granted_scopes is None:
        return _oauth_error(status.HTTP_400_BAD_REQUEST, "invalid_scope",
                            "Requested scope is not assigned to this client.")

    active = [s for s in client.secrets if s.revoked_at is None]
    if len(active) != 1:
        return _oauth_error(status.HTTP_401_UNAUTHORIZED, "invalid_client",
                            "Invalid client credentials.")
    secret_id = active[0].secret_id

    expires_in = client.access_token_ttl_seconds
    now = int(time.time())
    jti = str(uuid.uuid4())
    access_token = issue_client_access_token(
        client, granted_scopes, secret_id=secret_id, expires_in_seconds=expires_in, jti=jti,
    )
    await register_token(
        jti=jti, client_id=client.client_id, secret_id=secret_id,
        scope=" ".join(granted_scopes), issued_at=now, expires_at=now + expires_in,
    )
```

> This requires `issue_client_access_token` to accept an optional `jti` so the route and registry share one id. Add `jti: str | None = None` (default to `str(uuid.uuid4())`) in Task 8's function and return it — OR have the route generate the jti and pass it in. Implement by adding `jti: str | None = None` keyword to `issue_client_access_token`; when provided, use it instead of generating. Update Task 8 accordingly if executing out of order.

- Add imports: `import time`, `import uuid`, `from src.auth.oauth_token_registry import register_token`.
- Remove the `min(config.oauth_access_token_expiry_minutes, config.oauth_max_access_token_expiry_minutes) * 60` computation.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest services/api/tests/test_oauth_routes.py -v`
Expected: PASS

- [ ] **Step 5: Commit (only if authorized)**

```bash
git add services/api/src/routes/oauth.py services/api/src/auth/oauth_tokens.py services/api/tests/test_oauth_routes.py
git commit -m "feat(oauth): per-client token TTL and registry on issuance"
```

---

## Task 11: Admin endpoints — rotate, patch, list tokens, revoke (`routes/oauth_clients.py`)

**Files:**
- Modify: `services/api/src/routes/oauth_clients.py`
- Test: `services/api/tests/test_oauth_admin_routes.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/test_oauth_admin_routes.py
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth.deps import require_human_admin
from src.auth.models import AuthUser
from src.auth.oauth_token_registry import TokenRecord
from src.routes import oauth_clients as routes


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_human_admin] = lambda: AuthUser(
        email="a@x", google_sub="a", role="admin", entity_key=None, display_name="A"
    )
    return TestClient(app)


def test_rotate_secret_returns_201(client: TestClient) -> None:
    from src.auth.oauth_client_models import RotateSecretResponse
    with patch.object(routes, "rotate_oauth_client_secret",
                      return_value=RotateSecretResponse(client_id="hpc_a", client_secret="hps_x",
                                                        secret_id="sec_2", secret_prefix="hps_x")):
        res = client.post("/v1/admin/oauth-clients/hpc_a/rotate-secret")
    assert res.status_code == 201
    assert res.json()["client_secret"] == "hps_x"


def test_rotate_secret_404_when_missing(client: TestClient) -> None:
    with patch.object(routes, "rotate_oauth_client_secret", return_value=None):
        res = client.post("/v1/admin/oauth-clients/ghost/rotate-secret")
    assert res.status_code == 404


def test_patch_client_204(client: TestClient) -> None:
    with patch.object(routes, "update_oauth_client", return_value=True):
        res = client.patch("/v1/admin/oauth-clients/hpc_a", json={"access_token_ttl_seconds": 1800})
    assert res.status_code == 204


def test_list_tokens_returns_views(client: TestClient) -> None:
    rec = TokenRecord(jti="t1", client_id="hpc_a", secret_id="sec_1", scope="persons:read",
                      issued_at=1, expires_at=2, last_used_at=None, last_used_ip="203.0.113.5")
    with patch.object(routes, "list_client_tokens", return_value=[rec]):
        res = client.get("/v1/admin/oauth-clients/hpc_a/tokens")
    assert res.status_code == 200
    body = res.json()
    assert body[0]["jti"] == "t1"
    assert body[0]["last_used_ip"] == "203.0.113.5"


def test_revoke_token_204(client: TestClient) -> None:
    with patch.object(routes, "revoke_token_entry", return_value=True), \
         patch.object(routes, "revoke_token", return_value=None):
        res = client.post("/v1/admin/oauth-clients/hpc_a/tokens/t1/revoke")
    assert res.status_code == 204
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest services/api/tests/test_oauth_admin_routes.py -v`
Expected: FAIL — routes/handlers not present.

- [ ] **Step 3: Rewrite the admin router**

In `services/api/src/routes/oauth_clients.py`, replace the secret-create/secret-revoke handlers with rotate/patch/tokens/revoke:

```python
from src.auth.oauth_client_models import (
    CreateOAuthClientRequest, OAuthAccessTokenView, OAuthClient,
    OAuthClientCreatedResponse, RotateSecretResponse, UpdateOAuthClientRequest,
)
from src.auth.oauth_clients import (
    create_oauth_client, delete_oauth_client, disable_oauth_client,
    list_oauth_clients, rotate_oauth_client_secret, update_oauth_client,
)
from src.auth.oauth_token_registry import TokenRecord, list_client_tokens, revoke_token_entry
from src.auth.revoke import revoke_token


@router.post("/{client_id}/rotate-secret", response_model=RotateSecretResponse, status_code=201)
async def rotate_secret_handler(
    client_id: str, request: Request, _user: AuthUser = Depends(require_human_admin),
) -> RotateSecretResponse:
    """Rotate a client's secret, invalidating tokens minted under the old one."""
    rotated = await rotate_oauth_client_secret(client_id)
    if rotated is None:
        raise http_error(404, "not_found", "OAuth client not found or disabled.", request)
    return rotated


@router.patch("/{client_id}", status_code=204)
async def update_client_handler(
    client_id: str, body: UpdateOAuthClientRequest, request: Request,
    _user: AuthUser = Depends(require_human_admin),
) -> None:
    """Update a client's name, scopes, or access-token TTL."""
    if not await update_oauth_client(client_id, body):
        raise http_error(404, "not_found", "OAuth client not found.", request)


@router.get("/{client_id}/tokens", response_model=list[OAuthAccessTokenView])
async def list_tokens_handler(
    client_id: str, _user: AuthUser = Depends(require_human_admin),
) -> list[OAuthAccessTokenView]:
    """List a client's currently-valid access tokens."""
    records: list[TokenRecord] = await list_client_tokens(client_id)
    return [
        OAuthAccessTokenView(
            jti=r.jti, scope=r.scope, issued_at=r.issued_at, expires_at=r.expires_at,
            last_used_at=r.last_used_at, last_used_ip=r.last_used_ip,
        )
        for r in records
    ]


@router.post("/{client_id}/tokens/{jti}/revoke", status_code=204)
async def revoke_token_handler(
    client_id: str, jti: str, request: Request,
    _user: AuthUser = Depends(require_human_admin),
) -> None:
    """Revoke one access token immediately."""
    records = await list_client_tokens(client_id)
    match = next((r for r in records if r.jti == jti), None)
    if match is None:
        raise http_error(404, "not_found", "Token not found.", request)
    await revoke_token(jti, match.expires_at)
    await revoke_token_entry(client_id, jti)
```

Keep `create_oauth_client_handler`, `list_oauth_clients_handler`, `disable_oauth_client_handler`, `delete_oauth_client_handler`. Remove the old `create_oauth_secret_handler` and `revoke_oauth_secret_handler`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest services/api/tests/test_oauth_admin_routes.py -v`
Expected: PASS

- [ ] **Step 5: Type-check and commit (only if authorized)**

Run: `uv run --package profile-unifier-api mypy --strict services/api/src/routes/oauth_clients.py`
```bash
git add services/api/src/routes/oauth_clients.py services/api/tests/test_oauth_admin_routes.py
git commit -m "feat(admin): rotate secret, patch client, list/revoke tokens"
```

---

## Task 12: Wipe-and-recreate migration at startup (`app.py`)

**Files:**
- Modify: `services/api/src/app.py:37-66`
- Modify: `services/api/src/auth/oauth_clients.py` (uses `wipe_oauth_clients` from Task 7)
- Test: `services/api/tests/test_oauth_migration.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/test_oauth_migration.py
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.app import _wipe_oauth_clients_on_startup


@pytest.mark.asyncio
async def test_startup_wipes_nodes_and_registry() -> None:
    with patch("src.app.wipe_oauth_clients", new=AsyncMock()) as wipe_nodes, \
         patch("src.app.clear_all_tokens", new=AsyncMock()) as wipe_redis:
        await _wipe_oauth_clients_on_startup()
    wipe_nodes.assert_awaited_once()
    wipe_redis.assert_awaited_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/api/tests/test_oauth_migration.py -v`
Expected: FAIL — `_wipe_oauth_clients_on_startup` not defined.

- [ ] **Step 3: Add the migration to lifespan**

In `services/api/src/app.py`:

```python
from src.auth.oauth_clients import ensure_oauth_client_constraints, wipe_oauth_clients
from src.auth.oauth_token_registry import clear_all_tokens


async def _wipe_oauth_clients_on_startup() -> None:
    """One-time remodel migration: drop legacy multi-secret OAuth data.

    Existing clients predate the single-active-secret + per-client-TTL model and
    have no clean upgrade path, so they are wiped; admins re-provision.
    """
    try:
        await wipe_oauth_clients()
        await clear_all_tokens()
        logger.info("Wiped legacy OAuth clients and token registry (remodel migration)")
    except Exception:  # noqa: BLE001 — best-effort; never block startup
        logger.exception("Failed to wipe legacy OAuth clients")
```

Call it in `_lifespan` after `_ensure_oauth_client_constraints()`:

```python
    await _ensure_oauth_client_constraints()
    await _wipe_oauth_clients_on_startup()
```

> Note: this wipe runs on every boot as written. That is acceptable for the remodel rollout (it only deletes OAuth clients, which are cheap to recreate) — but if the user wants it to run exactly once, gate it behind a one-shot marker node (`MERGE (:Migration {id:'oauth_remodel_wipe'}) ON CREATE SET ... RETURN created`) and only wipe when newly created. Confirm with the user during execution; default per spec is a clean wipe at rollout. **Recommended:** make it idempotent-once via a marker so a later legitimately-created client isn't wiped on the next deploy.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest services/api/tests/test_oauth_migration.py -v`
Expected: PASS

- [ ] **Step 5: Commit (only if authorized)**

```bash
git add services/api/src/app.py services/api/tests/test_oauth_migration.py
git commit -m "feat(migration): wipe legacy OAuth clients/tokens on startup"
```

---

## Task 13: Trust proxy headers in uvicorn (`main.py`)

**Files:**
- Modify: `services/api/src/main.py`
- Modify: `services/api/src/config.py` (add `forwarded_allow_ips` setting)

- [ ] **Step 1: Add the config setting**

In `services/api/src/config.py`, add:

```python
    forwarded_allow_ips: str = Field(default="*", alias="FORWARDED_ALLOW_IPS")
```

> `*` trusts XFF from any peer. Because the API is never exposed directly (only nginx reaches it on the docker network), this is acceptable; to tighten, set `FORWARDED_ALLOW_IPS` to the docker network CIDR (e.g. `172.16.0.0/12`) via compose. Document in the compose env.

- [ ] **Step 2: Pass proxy-header options to uvicorn**

In `services/api/src/main.py`:

```python
    uvicorn.run(
        "src.app:app",
        host="0.0.0.0",  # noqa: S104 — container service binds to all interfaces
        port=config.port,
        log_level=config.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips=config.forwarded_allow_ips,
    )
```

- [ ] **Step 3: Verify config import resolves**

Run: `uv run --package profile-unifier-api python -c "from src.config import config; print(config.forwarded_allow_ips)"`
Expected: prints `*`

- [ ] **Step 4: Add env var to compose files (sync rule)**

Add `FORWARDED_ALLOW_IPS` (optional) under the `api` service `environment:` in BOTH `docker-compose.yml` and `.docker/staging/docker-compose.yml`. If you set no value, the `*` default applies — only add the var if pinning to a CIDR. (If you add nothing, note that the default already works; skip this step.)

- [ ] **Step 5: Commit (only if authorized)**

```bash
git add services/api/src/main.py services/api/src/config.py docker-compose.yml .docker/staging/docker-compose.yml
git commit -m "feat(api): trust nginx proxy headers for client IP"
```

---

## Task 14: Frontend types + BFF routes (`frontend2`)

**Files:**
- Modify: `services/frontend2/src/lib/api-types-ops.ts:306-365`
- Create: `services/frontend2/src/app/bff/admin/oauth-clients/[clientId]/rotate-secret/route.ts`
- Create: `services/frontend2/src/app/bff/admin/oauth-clients/[clientId]/tokens/route.ts`
- Create: `services/frontend2/src/app/bff/admin/oauth-clients/[clientId]/tokens/[jti]/revoke/route.ts`
- Modify: `services/frontend2/src/app/bff/admin/oauth-clients/[clientId]/route.ts` (add PATCH)

- [ ] **Step 1: Update the types**

In `services/frontend2/src/lib/api-types-ops.ts`:

```typescript
export interface OAuthClientSecret {
  secret_id: string;
  secret_prefix: string;
  created_at: string | null;
  revoked_at: string | null;
  last_used_at: string | null;
}

export interface OAuthClient {
  client_id: string;
  name: string;
  entity_key: string | null;
  scopes: string[];
  created_by: string;
  created_at: string | null;
  disabled_at: string | null;
  last_used_at: string | null;
  access_token_ttl_seconds: number;
  secrets: OAuthClientSecret[];
}

export interface OAuthClientCreated {
  client_id: string;
  client_secret: string;
  secret_id: string;
  secret_prefix: string;
  name: string;
  scopes: string[];
}

export interface CreateOAuthClientRequest {
  name: string;
  entity_key: string | null;
  scopes: string[];
  access_token_ttl_seconds: number;
}

export interface UpdateOAuthClientRequest {
  name?: string;
  scopes?: string[];
  access_token_ttl_seconds?: number;
}

export interface RotateSecretResponse {
  client_id: string;
  client_secret: string;
  secret_id: string;
  secret_prefix: string;
}

export interface OAuthAccessToken {
  jti: string;
  scope: string;
  issued_at: number;
  expires_at: number;
  last_used_at: number | null;
  last_used_ip: string | null;
}
```

Remove `CreateOAuthClientSecretRequest` and `OAuthClientSecretCreated`.

- [ ] **Step 2: Add the BFF route handlers**

`rotate-secret/route.ts`:

```typescript
import type { NextResponse } from "next/server";
import { proxyToApi } from "@/lib/proxy";
import type { RotateSecretResponse } from "@/lib/api-types-ops";

export const dynamic = "force-dynamic";

interface RouteContext { params: Promise<{ clientId: string }>; }

export async function POST(_request: Request, context: RouteContext): Promise<NextResponse> {
  const { clientId } = await context.params;
  return proxyToApi<RotateSecretResponse>(
    `/v1/admin/oauth-clients/${encodeURIComponent(clientId)}/rotate-secret`,
    { method: "POST" },
  );
}
```

`tokens/route.ts`:

```typescript
import type { NextResponse } from "next/server";
import { proxyToApi } from "@/lib/proxy";
import type { OAuthAccessToken } from "@/lib/api-types-ops";

export const dynamic = "force-dynamic";

interface RouteContext { params: Promise<{ clientId: string }>; }

export async function GET(_request: Request, context: RouteContext): Promise<NextResponse> {
  const { clientId } = await context.params;
  return proxyToApi<OAuthAccessToken[]>(
    `/v1/admin/oauth-clients/${encodeURIComponent(clientId)}/tokens`,
  );
}
```

`tokens/[jti]/revoke/route.ts`:

```typescript
import type { NextResponse } from "next/server";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext { params: Promise<{ clientId: string; jti: string }>; }

export async function POST(_request: Request, context: RouteContext): Promise<NextResponse> {
  const { clientId, jti } = await context.params;
  return proxyToApi<null>(
    `/v1/admin/oauth-clients/${encodeURIComponent(clientId)}/tokens/${encodeURIComponent(jti)}/revoke`,
    { method: "POST" },
  );
}
```

Add PATCH to the existing `[clientId]/route.ts`:

```typescript
export async function PATCH(request: Request, context: RouteContext): Promise<NextResponse> {
  const { clientId } = await context.params;
  const body = (await request.json()) as unknown;
  return proxyToApi<null>(`/v1/admin/oauth-clients/${encodeURIComponent(clientId)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
    headers: { "Content-Type": "application/json" },
  });
}
```

> Confirm `proxyToApi`'s options signature supports `body`/`headers` — match the shape used by existing POST proxies in `bff/admin/oauth-clients/route.ts`.

- [ ] **Step 3: Typecheck**

Run: `cd services/frontend2 && npm run typecheck`
Expected: clean (0 errors).

- [ ] **Step 4: Commit (only if authorized)**

```bash
git add services/frontend2/src/lib/api-types-ops.ts services/frontend2/src/app/bff/admin/oauth-clients
git commit -m "feat(frontend2): OAuth remodel types and BFF routes"
```

---

## Task 15: Frontend UI — create form, rotate, TTL edit, tokens table

**Files:**
- Modify: `services/frontend2/src/app/admin/oauth/page.tsx`

- [ ] **Step 1: Update the create modal**

Replace the "Expires in (days)" field with an access-token lifetime field (minutes), default 15, min 5, max 1440:

```tsx
const [ttlMinutes, setTtlMinutes] = useState("15");
// ...in the form:
<div className={styles.formField}>
  <label className={styles.formLabel}>Access token lifetime <span className={styles.optional}>(minutes)</span></label>
  <input className={styles.formInput} type="number" min="5" max="1440" value={ttlMinutes}
    onChange={(e) => setTtlMinutes(e.target.value)} />
</div>
// ...in handleSubmit body:
const body: CreateOAuthClientRequest = {
  name: name.trim(),
  entity_key: null,
  scopes,
  access_token_ttl_seconds: Math.max(300, Math.min(86400, parseInt(ttlMinutes || "15", 10) * 60)),
};
```

- [ ] **Step 2: Add Rotate + token TTL + tokens table to the client card**

Extend `OAuthClientCard` with:
- A **Rotate secret** button calling `POST /bff/admin/oauth-clients/{id}/rotate-secret`, showing the returned one-time secret in a notice, with a `confirm()` warning: "Rotating revokes the current secret and invalidates all live access tokens. Continue?"
- An editable token-lifetime input that PATCHes `access_token_ttl_seconds` (minutes → seconds).
- A **Tokens** section that loads `GET /bff/admin/oauth-clients/{id}/tokens` on expand and renders a table with columns: Issued, Expires, Last used, IP, and a **Revoke** button per row (`POST .../tokens/{jti}/revoke`). Show `last_used_ip ?? "—"` and format epoch seconds via the existing `relativeTime` / date helpers (multiply by 1000 for `Date`). Refresh after revoke.

> Keep the card under ~150 lines per the TS standards — extract a `TokensTable` subcomponent and a `RotateSecretButton` subcomponent into the same file or sibling files if it grows. Reuse `styles` classes already in `admin.module.css`; add minimal classes only if needed.

- [ ] **Step 3: Remove dead references**

Delete the `activeSecrets` count logic that referenced multiple secrets / `revoked_at` filtering if it no longer matches the single-secret model (show the current secret prefix instead). Ensure no imports of removed types (`OAuthClientSecretCreated`, `CreateOAuthClientSecretRequest`).

- [ ] **Step 4: Typecheck + lint (zero net new warnings)**

Run: `cd services/frontend2 && npm run typecheck`
Expected: clean.
Run: `cd services/frontend2 && npm run lint`
Expected: no NEW warnings beyond the known ~18 `react-hooks/set-state-in-effect` baseline (compare against a stash of your changes, per CLAUDE.md).

- [ ] **Step 5: Commit (only if authorized)**

```bash
git add services/frontend2/src/app/admin/oauth/page.tsx
git commit -m "feat(frontend2): rotate secret, edit token TTL, live tokens table"
```

---

## Task 16: Full verification pass

**Files:** none (verification only)

- [ ] **Step 1: Backend lint + types**

Run:
```
uv run --package profile-unifier-api ruff check services/api/src
uv run --package profile-unifier-api ruff format --check services/api/src
uv run --package profile-unifier-api mypy --strict services/api/src
```
Expected: clean (no new errors; pre-existing `types_sales.py`/`types_requests.py` Any-warnings are known and not regressions).

- [ ] **Step 2: Full backend test suite**

Run: `uv run pytest services/api/tests -v`
Expected: PASS. Confirm no orphaned references to removed symbols (`create_oauth_client_secret`, `revoke_oauth_client_secret`, `secret_expires_in_days`, `CREATE_OAUTH_CLIENT_SECRET`, `REVOKE_OAUTH_CLIENT_SECRET`, `_REVOKED_SET`). Grep to be sure:

Run: `git grep -nE "create_oauth_client_secret|revoke_oauth_client_secret|secret_expires_in_days|CREATE_OAUTH_CLIENT_SECRET|REVOKE_OAUTH_CLIENT_SECRET|_REVOKED_SET" services/api/src`
Expected: no matches.

- [ ] **Step 3: Frontend typecheck + lint**

Run: `cd services/frontend2 && npm run typecheck && npm run lint`
Expected: typecheck clean; lint at or below the documented warning baseline.

- [ ] **Step 4: Docker smoke test (optional but recommended)**

Run: `docker compose build --no-cache api frontend2 && docker compose up -d api frontend2 web`
Then exercise: create a client (note TTL), request a token at `/api/oauth2/v1/token`, call a `persons:read` route, view the token in the admin UI (confirm last-used IP populates), rotate the secret, confirm the old token now 401s, revoke a token, confirm it disappears from the list.

- [ ] **Step 5: Commit any test/lint fixups (only if authorized)**

```bash
git add -A
git commit -m "test(oauth): remodel verification fixups"
```

---

## Self-Review Notes (author)

- **Spec coverage:** single active secret (T6/T7), rotation kills tokens (T6 query + T8 secret_id claim + T9 verify check), per-client TTL (T5/T6/T10/T15), token registry + last-used IP (T3/T4/T9), list/revoke tokens (T11/T14/T15), wipe migration (T12), proxy headers (T13), folded bug fixes (T1/T2). All spec sections map to tasks.
- **Cross-task type consistency:** `issue_client_access_token` gains keyword-only `secret_id` (T8) and optional `jti` (T10) — both noted in both tasks. `TokenRecord` (T4) consumed by T9/T11. `RotateSecretResponse`/`UpdateOAuthClientRequest`/`OAuthAccessTokenView` defined T5, used T11/T14.
- **Open execution decisions flagged inline:** (a) whether the startup wipe is one-shot via a marker node (T12 — recommended) vs. every-boot; (b) whether to pin `FORWARDED_ALLOW_IPS` to a CIDR (T13). Surface both to the user at execution.
- **Out of scope (per spec):** admin scope-narrowing fix; machine self-service token management; JWKS grace window; RFC 6749 `WWW-Authenticate`/Basic auth.
```
