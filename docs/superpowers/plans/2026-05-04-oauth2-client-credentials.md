# OAuth2 Client Credentials Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace HyperP's server-to-server API keys with admin-managed OAuth2 client credentials that issue RS256 JWT Bearer tokens.

**Architecture:** Keep Google ID-token auth for human users and replace the API-key machine path with HyperP-issued OAuth client JWTs. OAuth clients and secrets live in Neo4j, secrets are hashed and rotatable, tokens are signed with RS256 and exposed through `/v1/oauth/jwks`, and route authorization uses explicit machine scopes plus optional `entity_key`.

**Tech Stack:** FastAPI, Pydantic v2, Neo4j async driver, Redis, hand-rolled JWT/JWKS using `cryptography`, pytest/pytest-asyncio, Next.js App Router, TypeScript, MUI.

**Commit policy:** The user explicitly said not to commit yet. Do not run any `git commit` step during implementation unless the user later asks for it.

---

## File Structure

### Backend create

- `services/api/src/auth/oauth_client_models.py`
  - Pydantic models for OAuth clients, secrets, create/update requests, token responses, and OAuth errors.
- `services/api/src/auth/oauth_clients.py`
  - Client-secret generation, HMAC hashing, client/secret CRUD, credential validation, scope checks, last-used updates.
- `services/api/src/auth/oauth_tokens.py`
  - RS256 JWT signing/verification, JWKS generation, token claim model, token revocation checks.
- `services/api/src/graph/queries/oauth_clients.py`
  - Cypher constraints and queries for `OAuthClient` and `OAuthClientSecret`.
- `services/api/src/routes/oauth.py`
  - Public `POST /v1/oauth/token` and `GET /v1/oauth/jwks`.
- `services/api/src/routes/oauth_clients.py`
  - Admin OAuth client management endpoints.
- `services/api/tests/test_oauth_clients.py`
  - Pure and service-level OAuth client tests using patched Neo4j/Redis boundaries.
- `services/api/tests/test_oauth_tokens.py`
  - JWT/JWKS signing, verification, expiry, audience, and revoked-JTI tests.
- `services/api/tests/test_oauth_routes.py`
  - FastAPI route tests for token endpoint and admin endpoints.

### Backend modify/remove

- Modify `services/api/src/config.py`
  - Add OAuth issuer/audience/lifetime/signing-key settings alongside the existing API-key settings. Keep legacy API-key config until Task 6 removes the API-key backend path.
- Modify `services/api/src/auth/deps.py`
  - Replace `ApiKeyUser` and `get_current_user_or_api_key` with OAuth client principal handling.
- Modify `services/api/src/app.py`
  - Include public OAuth token/JWKS routes without active auth; replace API-key admin router with OAuth-client admin router; call OAuth constraint setup.
- Modify `services/api/src/routes/ingest.py`, `services/api/src/routes/persons.py`, and other machine-callable routes as needed
  - Add explicit `require_scope(...)` dependencies where machine callers need access.
- Modify `services/api/pyproject.toml` and `uv.lock`
  - Add direct `cryptography` dependency.
- Delete `services/api/src/auth/api_key_models.py`
- Delete `services/api/src/auth/api_keys.py`
- Delete `services/api/src/graph/queries/api_keys.py`
- Delete `services/api/src/routes/api_keys.py`

### Frontend create/modify/remove

- Create `services/frontend/src/app/admin/oauth-clients/page.tsx`
  - Admin UI replacing API-key page, including create client, one-time secret display, add secret, revoke secret, disable/delete client.
- Create `services/frontend/src/app/bff/admin/oauth-clients/route.ts`
  - BFF list/create proxy.
- Create `services/frontend/src/app/bff/admin/oauth-clients/[clientId]/route.ts`
  - BFF disable/delete proxy.
- Create `services/frontend/src/app/bff/admin/oauth-clients/[clientId]/secrets/route.ts`
  - BFF add-secret proxy.
- Create `services/frontend/src/app/bff/admin/oauth-clients/[clientId]/secrets/[secretId]/route.ts`
  - BFF revoke-secret proxy.
- Modify `services/frontend/src/lib/api-types-ops.ts`
  - Replace API-key types/constants with OAuth client types/constants.
- Modify `services/frontend/src/app/admin/page.tsx`
  - Link to OAuth clients instead of API keys.
- Delete `services/frontend/src/app/admin/api-keys/page.tsx`
- Delete `services/frontend/src/app/bff/admin/api-keys/route.ts`
- Delete `services/frontend/src/app/bff/admin/api-keys/[keyId]/route.ts`

### Config/docs modify

- Modify `docker-compose.yml`
  - Keep API-key env vars and add OAuth issuer/audience/access-token/signing-key env vars. Legacy API-key env vars are removed in Task 6 when the API-key backend path is removed.
- If `.docker/staging/docker-compose.yml` exists in the working tree when implementing, apply equivalent compose changes there. It was absent during planning.
- Modify `services/api/tests/conftest.py`
  - Keep API-key env defaults and add OAuth env defaults and deterministic test RSA key.
- Modify `docs/profile-unifier-api-spec.md`
  - Replace API-key documentation with OAuth client credentials.
- Modify `docs/profile-unifier-openapi-3.1.yaml`
  - Remove `apiKeyAuth`; document `/v1/oauth/token`, `/v1/oauth/jwks`, admin OAuth-client endpoints, and Bearer JWT use.

---

## Task 1: Add OAuth config and direct crypto dependency

**Files:**
- Modify: `services/api/pyproject.toml`
- Modify: `services/api/src/config.py`
- Modify: `services/api/tests/conftest.py`
- Modify: `docker-compose.yml`
- Maybe modify: `.docker/staging/docker-compose.yml` if present during implementation

- [ ] **Step 1: Add failing config tests**

Create `services/api/tests/test_oauth_config.py` with:

```python
"""Tests for OAuth client-credentials configuration."""

from __future__ import annotations

from src.config import AppConfig


def test_oauth_config_defaults() -> None:
    cfg = AppConfig(NEO4J_PASSWORD="pw")

    assert cfg.oauth_issuer == "http://localhost/api"
    assert cfg.oauth_audience == "hyperp-api"
    assert cfg.oauth_access_token_expiry_minutes == 15
    assert cfg.oauth_max_access_token_expiry_minutes == 60
    assert cfg.oauth_active_key_id == "local-dev"


def test_oauth_config_reads_env_aliases() -> None:
    cfg = AppConfig(
        NEO4J_PASSWORD="pw",
        OAUTH_ISSUER="https://hyperp.example/api",
        OAUTH_AUDIENCE="profile-unifier",
        OAUTH_ACCESS_TOKEN_EXPIRY_MINUTES="10",
        OAUTH_MAX_ACCESS_TOKEN_EXPIRY_MINUTES="30",
        OAUTH_ACTIVE_KEY_ID="kid-2026-05",
        OAUTH_PRIVATE_KEY_PEM="private-pem",
        OAUTH_PUBLIC_KEY_PEM="public-pem",
    )

    assert cfg.oauth_issuer == "https://hyperp.example/api"
    assert cfg.oauth_audience == "profile-unifier"
    assert cfg.oauth_access_token_expiry_minutes == 10
    assert cfg.oauth_max_access_token_expiry_minutes == 30
    assert cfg.oauth_active_key_id == "kid-2026-05"
    assert cfg.oauth_private_key_pem == "private-pem"
    assert cfg.oauth_public_key_pem == "public-pem"
```

- [ ] **Step 2: Run config tests and verify they fail**

Run:

```bash
uv run pytest services/api/tests/test_oauth_config.py -v
```

Expected: FAIL because `oauth_issuer`, `oauth_audience`, and related fields do not exist.

- [ ] **Step 3: Add `cryptography` dependency**

Run:

```bash
uv add --package profile-unifier-api 'cryptography>=42,<47'
```

Expected: `services/api/pyproject.toml` and `uv.lock` update. Do not manually edit `uv.lock`.

- [ ] **Step 4: Add OAuth config alongside legacy API-key config**

In `services/api/src/config.py`, keep the existing legacy API-key fields:

```python
api_keys_enabled: bool = Field(default=False, alias="API_KEYS_ENABLED")
api_key_secret: str = Field(default="", alias="API_KEY_SECRET")
api_key_header_name: str = Field(default="X-Api-Key", alias="API_KEY_HEADER_NAME")
```

Add these fields inside `AppConfig`:

```python
oauth_issuer: str = Field(default="http://localhost/api", alias="OAUTH_ISSUER")
oauth_audience: str = Field(default="hyperp-api", alias="OAUTH_AUDIENCE")
oauth_access_token_expiry_minutes: int = Field(
    default=15, alias="OAUTH_ACCESS_TOKEN_EXPIRY_MINUTES"
)
oauth_max_access_token_expiry_minutes: int = Field(
    default=60, alias="OAUTH_MAX_ACCESS_TOKEN_EXPIRY_MINUTES"
)
oauth_active_key_id: str = Field(default="local-dev", alias="OAUTH_ACTIVE_KEY_ID")
oauth_private_key_pem: str = Field(default="", alias="OAUTH_PRIVATE_KEY_PEM")
oauth_public_key_pem: str = Field(default="", alias="OAUTH_PUBLIC_KEY_PEM")
oauth_secret_hash_key: str = Field(default="", alias="OAUTH_SECRET_HASH_KEY")
```

- [ ] **Step 5: Update API test environment defaults**

In `services/api/tests/conftest.py`, keep the API-key entries in `_MIN_ENV`:

```python
"API_KEYS_ENABLED": "false",
"API_KEY_SECRET": "",
"API_KEY_HEADER_NAME": "X-Api-Key",
```

Also add:

```python
"OAUTH_ISSUER": "http://testserver/api",
"OAUTH_AUDIENCE": "hyperp-api-test",
"OAUTH_ACCESS_TOKEN_EXPIRY_MINUTES": "15",
"OAUTH_MAX_ACCESS_TOKEN_EXPIRY_MINUTES": "60",
"OAUTH_ACTIVE_KEY_ID": "test-key",
"OAUTH_PRIVATE_KEY_PEM": "",
"OAUTH_PUBLIC_KEY_PEM": "",
"OAUTH_SECRET_HASH_KEY": "test-secret-hash-key",
```

- [ ] **Step 6: Update compose env vars**

In `docker-compose.yml`, keep lines under the `api.environment` block:

```yaml
      API_KEYS_ENABLED: ${API_KEYS_ENABLED:-false}
      API_KEY_SECRET: ${API_KEY_SECRET:-}
      API_KEY_HEADER_NAME: ${API_KEY_HEADER_NAME:-X-Api-Key}
```

Also add:

```yaml
      OAUTH_ISSUER: ${OAUTH_ISSUER:-http://localhost/api}
      OAUTH_AUDIENCE: ${OAUTH_AUDIENCE:-hyperp-api}
      OAUTH_ACCESS_TOKEN_EXPIRY_MINUTES: ${OAUTH_ACCESS_TOKEN_EXPIRY_MINUTES:-15}
      OAUTH_MAX_ACCESS_TOKEN_EXPIRY_MINUTES: ${OAUTH_MAX_ACCESS_TOKEN_EXPIRY_MINUTES:-60}
      OAUTH_ACTIVE_KEY_ID: ${OAUTH_ACTIVE_KEY_ID:-local-dev}
      OAUTH_PRIVATE_KEY_PEM: ${OAUTH_PRIVATE_KEY_PEM:-}
      OAUTH_PUBLIC_KEY_PEM: ${OAUTH_PUBLIC_KEY_PEM:-}
      OAUTH_SECRET_HASH_KEY: ${OAUTH_SECRET_HASH_KEY:-}
```

If `.docker/staging/docker-compose.yml` exists, apply the same service env change there.

- [ ] **Step 7: Run config tests and verify they pass**

Run:

```bash
uv run pytest services/api/tests/test_oauth_config.py -v
```

Expected: PASS.

- [ ] **Step 8: Checkpoint, no commit**

Run:

```bash
git diff -- services/api/pyproject.toml uv.lock services/api/src/config.py services/api/tests/conftest.py docker-compose.yml
```

Expected: only OAuth config/dependency changes with legacy API-key config/env still present. Do not commit.

---

## Task 2: Add OAuth client models and pure helpers

**Files:**
- Create: `services/api/src/auth/oauth_client_models.py`
- Create: `services/api/src/auth/oauth_clients.py`
- Create: `services/api/tests/test_oauth_clients.py`

- [ ] **Step 1: Write failing pure-helper tests**

Create `services/api/tests/test_oauth_clients.py` with:

```python
"""Tests for OAuth client credential helpers and service behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.auth.models import AuthUser
from src.auth.oauth_client_models import OAuthClient, OAuthClientSecret
from src.auth.oauth_clients import (
    check_scope,
    generate_client_id,
    generate_client_secret,
    hash_client_secret,
    is_secret_usable,
    requested_scopes_or_default,
    verify_client_secret,
)


def test_generate_client_id_has_expected_prefix() -> None:
    client_id = generate_client_id()

    assert client_id.startswith("hpc_")
    assert len(client_id) > 20


def test_generated_secret_is_returned_once_and_hash_verifies() -> None:
    secret = generate_client_secret()
    digest = hash_client_secret(secret, hash_key="test-key")

    assert secret.startswith("hps_")
    assert digest != secret
    assert verify_client_secret(secret, digest, hash_key="test-key")
    assert not verify_client_secret("hps_wrong", digest, hash_key="test-key")


def test_check_scope_treats_admin_as_superset() -> None:
    assert check_scope(["admin"], "persons:read")
    assert check_scope(["persons:read"], "persons:read")
    assert not check_scope(["persons:read"], "persons:write")


def test_requested_scopes_default_to_assigned_scopes() -> None:
    assert requested_scopes_or_default(None, ["persons:read", "ingest:write"]) == [
        "persons:read",
        "ingest:write",
    ]


def test_requested_scopes_must_be_subset() -> None:
    assert requested_scopes_or_default("persons:read", ["persons:read"]) == ["persons:read"]
    assert requested_scopes_or_default("persons:write", ["persons:read"]) is None


def test_secret_usable_requires_not_revoked_and_not_expired() -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    usable = OAuthClientSecret(
        secret_id="sec_1",
        secret_prefix="hps_abc",
        created_at=now,
        expires_at=now + timedelta(days=1),
        revoked_at=None,
        last_used_at=None,
    )
    expired = usable.model_copy(update={"expires_at": now - timedelta(seconds=1)})
    revoked = usable.model_copy(update={"revoked_at": now})

    assert is_secret_usable(usable, now=now)
    assert not is_secret_usable(expired, now=now)
    assert not is_secret_usable(revoked, now=now)


def test_oauth_client_model_supports_admin_actor_shape() -> None:
    actor = AuthUser(
        email="admin@example.com",
        google_sub="sub",
        role="admin",
        entity_key=None,
        display_name="Admin",
        first_time=False,
    )
    client = OAuthClient(
        client_id="hpc_123",
        name="POS sync",
        entity_key="fundbox",
        scopes=["persons:read"],
        created_by=actor.email,
        created_at=datetime.now(UTC).replace(tzinfo=None),
        disabled_at=None,
        last_used_at=None,
        secrets=[],
    )

    assert client.created_by == "admin@example.com"
    assert client.entity_key == "fundbox"
```

- [ ] **Step 2: Run helper tests and verify they fail**

Run:

```bash
uv run pytest services/api/tests/test_oauth_clients.py -v
```

Expected: FAIL because `oauth_client_models.py` and `oauth_clients.py` do not exist.

- [ ] **Step 3: Create OAuth client models**

Create `services/api/src/auth/oauth_client_models.py`:

```python
"""OAuth client-credentials domain models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


OAuthGrantType = Literal["client_credentials"]


class OAuthClientScope:
    """Well-known scopes for machine OAuth clients."""

    PERSONS_READ = "persons:read"
    PERSONS_WRITE = "persons:write"
    INGEST_WRITE = "ingest:write"
    ADMIN = "admin"


class OAuthClientSecret(BaseModel):
    """Stored metadata for one OAuth client secret; plain secret is never stored."""

    secret_id: str
    secret_prefix: str
    created_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None


class OAuthClient(BaseModel):
    """Machine OAuth client metadata."""

    client_id: str
    name: str
    entity_key: str | None = None
    scopes: list[str]
    created_by: str
    created_at: datetime | None = None
    disabled_at: datetime | None = None
    last_used_at: datetime | None = None
    secrets: list[OAuthClientSecret] = Field(default_factory=list)


class CreateOAuthClientRequest(BaseModel):
    """Request body for creating an OAuth client and its first secret."""

    name: str = Field(min_length=1, max_length=128)
    entity_key: str | None = None
    scopes: list[str] = Field(min_length=1)
    secret_expires_in_days: int | None = Field(default=365, ge=1, le=730)


class OAuthClientCreatedResponse(BaseModel):
    """OAuth client creation response; plain secret is shown once only."""

    client_id: str
    client_secret: str
    secret_id: str
    secret_prefix: str
    name: str
    scopes: list[str]
    secret_expires_at: datetime | None = None


class CreateOAuthClientSecretRequest(BaseModel):
    """Request body for rotating an OAuth client secret."""

    expires_in_days: int | None = Field(default=365, ge=1, le=730)


class OAuthClientSecretCreatedResponse(BaseModel):
    """Secret rotation response; plain secret is shown once only."""

    client_id: str
    client_secret: str
    secret_id: str
    secret_prefix: str
    expires_at: datetime | None = None


class OAuthTokenRequest(BaseModel):
    """Parsed OAuth token request."""

    grant_type: OAuthGrantType
    client_id: str
    client_secret: str
    scope: str | None = None


class OAuthTokenResponse(BaseModel):
    """OAuth access token response."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    scope: str


class OAuthErrorResponse(BaseModel):
    """Standards-compatible OAuth error response."""

    error: str
    error_description: str
```

- [ ] **Step 4: Create pure OAuth client helper functions**

Create `services/api/src/auth/oauth_clients.py` with these imports and helper functions first:

```python
"""OAuth client credential generation, storage, and validation."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime

from src.auth.oauth_client_models import OAuthClientSecret
from src.config import config


def generate_client_id() -> str:
    """Generate a public OAuth client id."""
    return f"hpc_{secrets.token_urlsafe(24)}"


def generate_client_secret() -> str:
    """Generate a one-time OAuth client secret."""
    return f"hps_{secrets.token_urlsafe(32)}"


def _hash_key(explicit_hash_key: str | None = None) -> bytes:
    key = explicit_hash_key or config.oauth_secret_hash_key or config.oauth_private_key_pem
    if not key:
        key = "change-me-in-env"
    return key.encode()


def hash_client_secret(secret: str, *, hash_key: str | None = None) -> str:
    """HMAC-SHA256 hash of a client secret."""
    return hmac.new(_hash_key(hash_key), secret.encode(), hashlib.sha256).hexdigest()


def verify_client_secret(secret: str, digest: str, *, hash_key: str | None = None) -> bool:
    """Return whether a plain secret matches a stored digest."""
    candidate = hash_client_secret(secret, hash_key=hash_key)
    return hmac.compare_digest(candidate, digest)


def check_scope(scopes: list[str], required: str) -> bool:
    """Return True if scopes include required or admin."""
    return "admin" in scopes or required in scopes


def requested_scopes_or_default(requested: str | None, assigned: list[str]) -> list[str] | None:
    """Return requested scopes when they are a subset of assigned scopes."""
    if requested is None or not requested.strip():
        return assigned
    requested_scopes = [scope for scope in requested.split(" ") if scope]
    assigned_set = set(assigned)
    if any(scope not in assigned_set for scope in requested_scopes):
        return None
    return requested_scopes


def is_secret_usable(secret: OAuthClientSecret, *, now: datetime | None = None) -> bool:
    """Return whether a secret is active, non-revoked, and non-expired."""
    current = now or datetime.now(UTC).replace(tzinfo=None)
    if secret.revoked_at is not None:
        return False
    if secret.expires_at is None:
        return True
    expires_at = secret.expires_at.replace(tzinfo=None) if secret.expires_at.tzinfo else secret.expires_at
    return current <= expires_at
```

- [ ] **Step 5: Run helper tests and verify they pass**

Run:

```bash
uv run pytest services/api/tests/test_oauth_clients.py -v
```

Expected: PASS.

- [ ] **Step 6: Checkpoint, no commit**

Run:

```bash
git diff -- services/api/src/auth/oauth_client_models.py services/api/src/auth/oauth_clients.py services/api/tests/test_oauth_clients.py
```

Expected: new models and pure helper code only. Do not commit.

---

## Task 3: Add Neo4j OAuth client storage service

**Files:**
- Create: `services/api/src/graph/queries/oauth_clients.py`
- Modify: `services/api/src/auth/oauth_clients.py`
- Modify: `services/api/tests/test_oauth_clients.py`

- [ ] **Step 1: Extend tests for service functions with patched Neo4j session**

Append to `services/api/tests/test_oauth_clients.py`:

```python
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.auth.oauth_client_models import (
    CreateOAuthClientRequest,
    CreateOAuthClientSecretRequest,
    OAuthClientCreatedResponse,
    OAuthClientSecretCreatedResponse,
)
from src.auth.oauth_clients import (
    create_oauth_client,
    create_oauth_client_secret,
    disable_oauth_client,
    validate_client_credentials,
)


class _FakeResult:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self._records = records

    async def single(self) -> dict[str, object] | None:
        return self._records[0] if self._records else None

    def __aiter__(self) -> AsyncIterator[dict[str, object]]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[dict[str, object]]:
        for record in self._records:
            yield record


class _FakeSession:
    def __init__(self, result: _FakeResult | None = None) -> None:
        self.result = result or _FakeResult([])
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def run(self, query: str, **params: object) -> _FakeResult:
        self.calls.append((query, params))
        return self.result


def _admin_user() -> AuthUser:
    return AuthUser(
        email="admin@example.com",
        google_sub="sub",
        role="admin",
        entity_key=None,
        display_name="Admin",
        first_time=False,
    )


@pytest.mark.asyncio
async def test_create_oauth_client_stores_client_and_first_secret() -> None:
    session = _FakeSession()
    req = CreateOAuthClientRequest(
        name="POS sync",
        entity_key="fundbox",
        scopes=["persons:read"],
        secret_expires_in_days=30,
    )

    with patch("src.auth.oauth_clients.get_session", return_value=session):
        created = await create_oauth_client(req, _admin_user())

    assert isinstance(created, OAuthClientCreatedResponse)
    assert created.client_id.startswith("hpc_")
    assert created.client_secret.startswith("hps_")
    assert created.secret_id.startswith("sec_")
    assert session.calls[0][1]["name"] == "POS sync"
    assert session.calls[0][1]["entity_key"] == "fundbox"
    assert session.calls[0][1]["scopes"] == "persons:read"
    assert session.calls[0][1]["secret_hash"] != created.client_secret


@pytest.mark.asyncio
async def test_create_oauth_client_secret_returns_one_time_secret() -> None:
    session = _FakeSession()
    req = CreateOAuthClientSecretRequest(expires_in_days=60)

    with patch("src.auth.oauth_clients.get_session", return_value=session):
        created = await create_oauth_client_secret("hpc_123", req)

    assert isinstance(created, OAuthClientSecretCreatedResponse)
    assert created.client_id == "hpc_123"
    assert created.client_secret.startswith("hps_")
    assert session.calls[0][1]["client_id"] == "hpc_123"


@pytest.mark.asyncio
async def test_validate_client_credentials_rejects_unknown_client() -> None:
    session = _FakeSession(_FakeResult([]))

    with patch("src.auth.oauth_clients.get_session", return_value=session):
        assert await validate_client_credentials("hpc_missing", "hps_secret") is None


@pytest.mark.asyncio
async def test_disable_oauth_client_returns_false_for_missing_client() -> None:
    session = _FakeSession(_FakeResult([]))

    with patch("src.auth.oauth_clients.get_session", return_value=session):
        assert not await disable_oauth_client("hpc_missing")
```

- [ ] **Step 2: Run extended OAuth client tests and verify they fail**

Run:

```bash
uv run pytest services/api/tests/test_oauth_clients.py -v
```

Expected: FAIL because service functions and queries do not exist.

- [ ] **Step 3: Add Neo4j queries**

Create `services/api/src/graph/queries/oauth_clients.py`:

```python
"""Cypher constants for OAuth client management."""

from __future__ import annotations

CREATE_OAUTH_CLIENT_ID_CONSTRAINT = """
CREATE CONSTRAINT oauth_client_id_unique IF NOT EXISTS
FOR (c:OAuthClient) REQUIRE c.client_id IS UNIQUE
"""

CREATE_OAUTH_SECRET_ID_CONSTRAINT = """
CREATE CONSTRAINT oauth_secret_id_unique IF NOT EXISTS
FOR (s:OAuthClientSecret) REQUIRE s.secret_id IS UNIQUE
"""

CREATE_OAUTH_CLIENT_WITH_SECRET = """
MERGE (c:OAuthClient {client_id: $client_id})
ON CREATE SET
  c.name         = $name,
  c.entity_key   = $entity_key,
  c.scopes       = $scopes,
  c.created_by   = $created_by,
  c.created_at   = datetime($created_at),
  c.disabled_at  = NULL,
  c.last_used_at = NULL
CREATE (s:OAuthClientSecret {
  secret_id: $secret_id,
  secret_hash: $secret_hash,
  secret_prefix: $secret_prefix,
  created_at: datetime($secret_created_at),
  expires_at: CASE WHEN $secret_expires_at IS NOT NULL THEN datetime($secret_expires_at) ELSE NULL END,
  revoked_at: NULL,
  last_used_at: NULL
})
CREATE (c)-[:HAS_SECRET]->(s)
"""

GET_OAUTH_CLIENTS_FOR_ADMIN = """
MATCH (c:OAuthClient)
OPTIONAL MATCH (c)-[:HAS_SECRET]->(s:OAuthClientSecret)
WITH c, s ORDER BY s.created_at DESC
RETURN c {
  .client_id, .name, .entity_key, .scopes, .created_by,
  .created_at, .disabled_at, .last_used_at,
  secrets: collect(s {
    .secret_id, .secret_prefix, .created_at, .expires_at,
    .revoked_at, .last_used_at
  })
} AS client
ORDER BY c.created_at DESC
"""

GET_OAUTH_CLIENT_FOR_VALIDATION = """
MATCH (c:OAuthClient {client_id: $client_id})-[:HAS_SECRET]->(s:OAuthClientSecret)
RETURN c {
  .client_id, .name, .entity_key, .scopes, .created_by,
  .created_at, .disabled_at, .last_used_at,
  secret: s {
    .secret_id, .secret_hash, .secret_prefix, .created_at,
    .expires_at, .revoked_at, .last_used_at
  }
} AS client
"""

GET_OAUTH_CLIENT_BY_ID = """
MATCH (c:OAuthClient {client_id: $client_id})
OPTIONAL MATCH (c)-[:HAS_SECRET]->(s:OAuthClientSecret)
WITH c, s ORDER BY s.created_at DESC
RETURN c {
  .client_id, .name, .entity_key, .scopes, .created_by,
  .created_at, .disabled_at, .last_used_at,
  secrets: collect(s {
    .secret_id, .secret_prefix, .created_at, .expires_at,
    .revoked_at, .last_used_at
  })
} AS client
"""

CREATE_OAUTH_CLIENT_SECRET = """
MATCH (c:OAuthClient {client_id: $client_id})
WHERE c.disabled_at IS NULL
CREATE (s:OAuthClientSecret {
  secret_id: $secret_id,
  secret_hash: $secret_hash,
  secret_prefix: $secret_prefix,
  created_at: datetime($created_at),
  expires_at: CASE WHEN $expires_at IS NOT NULL THEN datetime($expires_at) ELSE NULL END,
  revoked_at: NULL,
  last_used_at: NULL
})
CREATE (c)-[:HAS_SECRET]->(s)
RETURN s.secret_id AS secret_id
"""

REVOKE_OAUTH_CLIENT_SECRET = """
MATCH (:OAuthClient {client_id: $client_id})-[:HAS_SECRET]->(s:OAuthClientSecret {secret_id: $secret_id})
WHERE s.revoked_at IS NULL
SET s.revoked_at = datetime()
RETURN s.secret_id AS secret_id
"""

DISABLE_OAUTH_CLIENT = """
MATCH (c:OAuthClient {client_id: $client_id})
WHERE c.disabled_at IS NULL
SET c.disabled_at = datetime()
RETURN c.client_id AS client_id
"""

DELETE_OAUTH_CLIENT = """
MATCH (c:OAuthClient {client_id: $client_id})
OPTIONAL MATCH (c)-[:HAS_SECRET]->(s:OAuthClientSecret)
DETACH DELETE s, c
"""

UPDATE_OAUTH_CLIENT_LAST_USED = """
MATCH (c:OAuthClient {client_id: $client_id})
SET c.last_used_at = datetime()
"""

UPDATE_OAUTH_SECRET_LAST_USED = """
MATCH (:OAuthClient {client_id: $client_id})-[:HAS_SECRET]->(s:OAuthClientSecret {secret_id: $secret_id})
SET s.last_used_at = datetime()
"""
```

- [ ] **Step 4: Extend OAuth client service implementation**

Append these service imports and functions to `services/api/src/auth/oauth_clients.py`:

```python
import asyncio
import uuid
from datetime import timedelta

from src.auth.models import AuthUser
from src.auth.oauth_client_models import (
    CreateOAuthClientRequest,
    CreateOAuthClientSecretRequest,
    OAuthClient,
    OAuthClientCreatedResponse,
    OAuthClientSecretCreatedResponse,
)
from src.graph.client import get_session
from src.graph.converters import to_datetime, to_optional_str, to_str
from src.graph.queries.oauth_clients import (
    CREATE_OAUTH_CLIENT_ID_CONSTRAINT,
    CREATE_OAUTH_CLIENT_SECRET,
    CREATE_OAUTH_CLIENT_WITH_SECRET,
    CREATE_OAUTH_SECRET_ID_CONSTRAINT,
    DELETE_OAUTH_CLIENT,
    DISABLE_OAUTH_CLIENT,
    GET_OAUTH_CLIENT_FOR_VALIDATION,
    GET_OAUTH_CLIENTS_FOR_ADMIN,
    REVOKE_OAUTH_CLIENT_SECRET,
    UPDATE_OAUTH_CLIENT_LAST_USED,
    UPDATE_OAUTH_SECRET_LAST_USED,
)


def _scopes_from_record(value: object) -> list[str]:
    if value is None:
        return []
    return [scope for scope in to_str(value).split(",") if scope]


def _secret_from_record(record: object) -> OAuthClientSecret:
    if not isinstance(record, dict):
        raise ValueError("unexpected oauth secret record shape")
    return OAuthClientSecret(
        secret_id=to_str(record.get("secret_id")),
        secret_prefix=to_str(record.get("secret_prefix")),
        created_at=to_datetime(record.get("created_at")),
        expires_at=to_datetime(record.get("expires_at")),
        revoked_at=to_datetime(record.get("revoked_at")),
        last_used_at=to_datetime(record.get("last_used_at")),
    )


def _client_from_record(record: object) -> OAuthClient:
    if not isinstance(record, dict):
        raise ValueError("unexpected oauth client record shape")
    secrets_raw = record.get("secrets", [])
    secrets: list[OAuthClientSecret] = []
    if isinstance(secrets_raw, list):
        for item in secrets_raw:
            if isinstance(item, dict) and item.get("secret_id") is not None:
                secrets.append(_secret_from_record(item))
    return OAuthClient(
        client_id=to_str(record.get("client_id")),
        name=to_str(record.get("name")),
        entity_key=to_optional_str(record.get("entity_key")),
        scopes=_scopes_from_record(record.get("scopes")),
        created_by=to_str(record.get("created_by")),
        created_at=to_datetime(record.get("created_at")),
        disabled_at=to_datetime(record.get("disabled_at")),
        last_used_at=to_datetime(record.get("last_used_at")),
        secrets=secrets,
    )


async def ensure_oauth_client_constraints() -> None:
    """Create OAuth client constraints if they do not exist."""
    try:
        async with get_session(write=True) as session:
            await session.run(CREATE_OAUTH_CLIENT_ID_CONSTRAINT)
            await session.run(CREATE_OAUTH_SECRET_ID_CONSTRAINT)
    except Exception:  # noqa: BLE001
        pass


async def create_oauth_client(
    req: CreateOAuthClientRequest, actor: AuthUser
) -> OAuthClientCreatedResponse:
    """Create an OAuth client and its first secret."""
    client_id = generate_client_id()
    client_secret = generate_client_secret()
    secret_id = f"sec_{uuid.uuid4()}"
    secret_prefix = client_secret[:10]
    secret_hash = hash_client_secret(client_secret)
    now = datetime.now(UTC).replace(tzinfo=None)
    expires_at = None if req.secret_expires_in_days is None else now + timedelta(days=req.secret_expires_in_days)

    async with get_session(write=True) as session:
        await session.run(
            CREATE_OAUTH_CLIENT_WITH_SECRET,
            client_id=client_id,
            name=req.name,
            entity_key=req.entity_key,
            scopes=",".join(req.scopes),
            created_by=actor.email,
            created_at=now.isoformat(),
            secret_id=secret_id,
            secret_hash=secret_hash,
            secret_prefix=secret_prefix,
            secret_created_at=now.isoformat(),
            secret_expires_at=expires_at.isoformat() if expires_at else None,
        )

    return OAuthClientCreatedResponse(
        client_id=client_id,
        client_secret=client_secret,
        secret_id=secret_id,
        secret_prefix=secret_prefix,
        name=req.name,
        scopes=req.scopes,
        secret_expires_at=expires_at,
    )


async def list_oauth_clients() -> list[OAuthClient]:
    """Return OAuth clients for the admin UI."""
    async with get_session() as session:
        result = await session.run(GET_OAUTH_CLIENTS_FOR_ADMIN)
        clients: list[OAuthClient] = []
        async for record in result:
            try:
                clients.append(_client_from_record(record["client"]))
            except (ValueError, KeyError):
                continue
        return clients


async def create_oauth_client_secret(
    client_id: str, req: CreateOAuthClientSecretRequest
) -> OAuthClientSecretCreatedResponse:
    """Create an additional secret for an OAuth client."""
    client_secret = generate_client_secret()
    secret_id = f"sec_{uuid.uuid4()}"
    secret_prefix = client_secret[:10]
    secret_hash = hash_client_secret(client_secret)
    now = datetime.now(UTC).replace(tzinfo=None)
    expires_at = None if req.expires_in_days is None else now + timedelta(days=req.expires_in_days)

    async with get_session(write=True) as session:
        await session.run(
            CREATE_OAUTH_CLIENT_SECRET,
            client_id=client_id,
            secret_id=secret_id,
            secret_hash=secret_hash,
            secret_prefix=secret_prefix,
            created_at=now.isoformat(),
            expires_at=expires_at.isoformat() if expires_at else None,
        )

    return OAuthClientSecretCreatedResponse(
        client_id=client_id,
        client_secret=client_secret,
        secret_id=secret_id,
        secret_prefix=secret_prefix,
        expires_at=expires_at,
    )


async def revoke_oauth_client_secret(client_id: str, secret_id: str) -> bool:
    """Revoke one OAuth client secret."""
    async with get_session(write=True) as session:
        result = await session.run(
            REVOKE_OAUTH_CLIENT_SECRET, client_id=client_id, secret_id=secret_id
        )
        return await result.single() is not None


async def disable_oauth_client(client_id: str) -> bool:
    """Disable an OAuth client without deleting its audit trail."""
    async with get_session(write=True) as session:
        result = await session.run(DISABLE_OAUTH_CLIENT, client_id=client_id)
        return await result.single() is not None


async def delete_oauth_client(client_id: str) -> bool:
    """Delete an OAuth client and its secrets."""
    async with get_session(write=True) as session:
        await session.run(DELETE_OAUTH_CLIENT, client_id=client_id)
    return True


async def validate_client_credentials(client_id: str, client_secret: str) -> tuple[OAuthClient, list[str]] | None:
    """Validate OAuth client credentials and return client plus assigned scopes."""
    async with get_session() as session:
        result = await session.run(GET_OAUTH_CLIENT_FOR_VALIDATION, client_id=client_id)
        async for record in result:
            raw = record["client"]
            if not isinstance(raw, dict):
                continue
            secret_raw = raw.get("secret")
            if not isinstance(secret_raw, dict):
                continue
            if raw.get("disabled_at") is not None:
                return None
            if not verify_client_secret(client_secret, to_str(secret_raw.get("secret_hash"))):
                continue
            secret = _secret_from_record(secret_raw)
            if not is_secret_usable(secret):
                return None
            client = _client_from_record({**raw, "secrets": [secret_raw]})
            _touch_last_used(client.client_id, secret.secret_id)
            return client, client.scopes
    return None


def _touch_last_used(client_id: str, secret_id: str) -> None:
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_update_last_used(client_id, secret_id))
    except RuntimeError:
        pass


async def _update_last_used(client_id: str, secret_id: str) -> None:
    try:
        async with get_session(write=True) as session:
            await session.run(UPDATE_OAUTH_CLIENT_LAST_USED, client_id=client_id)
            await session.run(
                UPDATE_OAUTH_SECRET_LAST_USED, client_id=client_id, secret_id=secret_id
            )
    except Exception:  # noqa: BLE001
        pass
```

- [ ] **Step 5: Run OAuth client tests**

Run:

```bash
uv run pytest services/api/tests/test_oauth_clients.py -v
```

Expected: PASS.

- [ ] **Step 6: Run lint on new backend files**

Run:

```bash
uv run --package profile-unifier-api ruff check services/api/src/auth/oauth_client_models.py services/api/src/auth/oauth_clients.py services/api/src/graph/queries/oauth_clients.py services/api/tests/test_oauth_clients.py
```

Expected: PASS or only fixable formatting/import issues. Apply `ruff format` if needed.

- [ ] **Step 7: Checkpoint, no commit**

Run:

```bash
git diff -- services/api/src/auth/oauth_clients.py services/api/src/graph/queries/oauth_clients.py services/api/tests/test_oauth_clients.py
```

Expected: OAuth client service and tests only. Do not commit.

---

## Task 4: Add RS256 JWT/JWKS token service

**Files:**
- Create: `services/api/src/auth/oauth_tokens.py`
- Create: `services/api/tests/test_oauth_tokens.py`

- [ ] **Step 1: Write failing token tests**

Create `services/api/tests/test_oauth_tokens.py`:

```python
"""Tests for HyperP-issued OAuth JWT access tokens."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from unittest.mock import patch

from src.auth.oauth_client_models import OAuthClient
from src.auth.oauth_tokens import (
    OAuthClientClaims,
    build_jwks,
    issue_client_access_token,
    verify_client_access_token,
)


def _pem_pair() -> tuple[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


def _client() -> OAuthClient:
    return OAuthClient(
        client_id="hpc_test",
        name="Test client",
        entity_key="fundbox",
        scopes=["persons:read", "ingest:write"],
        created_by="admin@example.com",
        created_at=datetime.now(UTC).replace(tzinfo=None),
        disabled_at=None,
        last_used_at=None,
        secrets=[],
    )


def test_issue_and_verify_client_access_token() -> None:
    private_pem, public_pem = _pem_pair()

    with patch("src.auth.oauth_tokens.config.oauth_private_key_pem", private_pem), patch(
        "src.auth.oauth_tokens.config.oauth_public_key_pem", public_pem
    ), patch("src.auth.oauth_tokens.config.oauth_active_key_id", "kid-test"), patch(
        "src.auth.oauth_tokens.config.oauth_issuer", "http://issuer"
    ), patch("src.auth.oauth_tokens.config.oauth_audience", "hyperp-api"):
        token = issue_client_access_token(_client(), ["persons:read"], expires_in_seconds=900)
        claims = verify_client_access_token(token)

    assert isinstance(claims, OAuthClientClaims)
    assert claims.sub == "hpc_test"
    assert claims.client_id == "hpc_test"
    assert claims.scopes == ["persons:read"]
    assert claims.entity_key == "fundbox"
    assert claims.aud == "hyperp-api"


def test_build_jwks_exposes_public_key_with_kid() -> None:
    _private_pem, public_pem = _pem_pair()

    with patch("src.auth.oauth_tokens.config.oauth_public_key_pem", public_pem), patch(
        "src.auth.oauth_tokens.config.oauth_active_key_id", "kid-test"
    ):
        jwks = build_jwks()

    assert jwks["keys"][0]["kid"] == "kid-test"
    assert jwks["keys"][0]["kty"] == "RSA"
    assert jwks["keys"][0]["use"] == "sig"
    assert jwks["keys"][0]["alg"] == "RS256"


def test_expired_token_is_rejected() -> None:
    private_pem, public_pem = _pem_pair()

    with patch("src.auth.oauth_tokens.config.oauth_private_key_pem", private_pem), patch(
        "src.auth.oauth_tokens.config.oauth_public_key_pem", public_pem
    ), patch("src.auth.oauth_tokens.config.oauth_issuer", "http://issuer"), patch(
        "src.auth.oauth_tokens.config.oauth_audience", "hyperp-api"
    ):
        token = issue_client_access_token(_client(), ["persons:read"], expires_in_seconds=-1)
        with pytest.raises(ValueError, match="expired"):
            verify_client_access_token(token)


def test_wrong_audience_is_rejected() -> None:
    private_pem, public_pem = _pem_pair()

    with patch("src.auth.oauth_tokens.config.oauth_private_key_pem", private_pem), patch(
        "src.auth.oauth_tokens.config.oauth_public_key_pem", public_pem
    ), patch("src.auth.oauth_tokens.config.oauth_issuer", "http://issuer"), patch(
        "src.auth.oauth_tokens.config.oauth_audience", "expected"):
        token = issue_client_access_token(_client(), ["persons:read"], expires_in_seconds=900)
        with patch("src.auth.oauth_tokens.config.oauth_audience", "different"):
            with pytest.raises(ValueError, match="audience"):
                verify_client_access_token(token)
```

- [ ] **Step 2: Run token tests and verify they fail**

Run:

```bash
uv run pytest services/api/tests/test_oauth_tokens.py -v
```

Expected: FAIL because `oauth_tokens.py` does not exist.

- [ ] **Step 3: Implement token service**

Create `services/api/src/auth/oauth_tokens.py`:

```python
"""HyperP-issued OAuth access tokens and JWKS support."""

from __future__ import annotations

import base64
import json
import time
import uuid
from typing import TypedDict

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import BaseModel

from src.auth.oauth_client_models import OAuthClient
from src.config import config


class OAuthClientClaims(BaseModel):
    """Verified claims from a HyperP client-credentials access token."""

    iss: str
    aud: str
    sub: str
    client_id: str
    scope: str
    scopes: list[str]
    entity_key: str | None = None
    iat: int
    nbf: int
    exp: int
    jti: str


class JsonWebKey(TypedDict):
    kty: str
    use: str
    kid: str
    alg: str
    n: str
    e: str


class JsonWebKeySet(TypedDict):
    keys: list[JsonWebKey]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(value: str) -> bytes:
    padding_len = (-len(value)) % 4
    return base64.urlsafe_b64decode(value + ("=" * padding_len))


def _json_b64(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return _b64url(raw)


def _private_key() -> rsa.RSAPrivateKey:
    if not config.oauth_private_key_pem:
        raise ValueError("OAUTH_PRIVATE_KEY_PEM is required")
    key = serialization.load_pem_private_key(config.oauth_private_key_pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("OAUTH_PRIVATE_KEY_PEM must be an RSA private key")
    return key


def _public_key() -> rsa.RSAPublicKey:
    if not config.oauth_public_key_pem:
        raise ValueError("OAUTH_PUBLIC_KEY_PEM is required")
    key = serialization.load_pem_public_key(config.oauth_public_key_pem.encode())
    if not isinstance(key, rsa.RSAPublicKey):
        raise ValueError("OAUTH_PUBLIC_KEY_PEM must be an RSA public key")
    return key


def issue_client_access_token(
    client: OAuthClient, scopes: list[str], *, expires_in_seconds: int
) -> str:
    """Issue an RS256 JWT access token for an OAuth client."""
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": config.oauth_issuer,
        "aud": config.oauth_audience,
        "sub": client.client_id,
        "client_id": client.client_id,
        "scope": " ".join(scopes),
        "scopes": scopes,
        "iat": now,
        "nbf": now,
        "exp": now + expires_in_seconds,
        "jti": str(uuid.uuid4()),
    }
    if client.entity_key is not None:
        payload["entity_key"] = client.entity_key

    header = {"alg": "RS256", "typ": "JWT", "kid": config.oauth_active_key_id}
    signing_input = f"{_json_b64(header)}.{_json_b64(payload)}".encode()
    signature = _private_key().sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input.decode()}.{_b64url(signature)}"


def verify_client_access_token(token: str) -> OAuthClientClaims:
    """Verify a HyperP-issued client access token."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("invalid token format")
    header_raw, payload_raw, sig_raw = parts
    header = json.loads(_b64url_decode(header_raw))
    if header.get("alg") != "RS256":
        raise ValueError("unsupported token algorithm")
    if header.get("kid") != config.oauth_active_key_id:
        raise ValueError("unknown signing key")

    signing_input = f"{header_raw}.{payload_raw}".encode()
    signature = _b64url_decode(sig_raw)
    _public_key().verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())

    payload = json.loads(_b64url_decode(payload_raw))
    now = int(time.time())
    if payload.get("iss") != config.oauth_issuer:
        raise ValueError("invalid issuer")
    if payload.get("aud") != config.oauth_audience:
        raise ValueError("invalid audience")
    exp = payload.get("exp")
    if not isinstance(exp, int) or now > exp:
        raise ValueError("token expired")
    nbf = payload.get("nbf")
    if not isinstance(nbf, int) or now + 300 < nbf:
        raise ValueError("token not yet valid")
    return OAuthClientClaims.model_validate(payload)


def build_jwks() -> JsonWebKeySet:
    """Return the configured OAuth public key as JWKS."""
    public_numbers = _public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "kid": config.oauth_active_key_id,
                "alg": "RS256",
                "n": _b64url(public_numbers.n.to_bytes((public_numbers.n.bit_length() + 7) // 8, "big")),
                "e": _b64url(public_numbers.e.to_bytes((public_numbers.e.bit_length() + 7) // 8, "big")),
            }
        ]
    }
```

- [ ] **Step 4: Run token tests**

Run:

```bash
uv run pytest services/api/tests/test_oauth_tokens.py -v
```

Expected: PASS.

- [ ] **Step 5: Run lint on token files**

Run:

```bash
uv run --package profile-unifier-api ruff check services/api/src/auth/oauth_tokens.py services/api/tests/test_oauth_tokens.py
```

Expected: PASS. Run `ruff format` if needed.

- [ ] **Step 6: Checkpoint, no commit**

Run:

```bash
git diff -- services/api/src/auth/oauth_tokens.py services/api/tests/test_oauth_tokens.py
```

Expected: token service and tests only. Do not commit.

---

## Task 5: Add OAuth public and admin API routes

**Files:**
- Create: `services/api/src/routes/oauth.py`
- Create: `services/api/src/routes/oauth_clients.py`
- Modify: `services/api/src/app.py`
- Create: `services/api/tests/test_oauth_routes.py`

- [ ] **Step 1: Write failing route tests**

Create `services/api/tests/test_oauth_routes.py`:

```python
"""FastAPI route tests for OAuth client credentials."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from src.app import build_app
from src.auth.models import AuthUser
from src.auth.oauth_client_models import OAuthClient, OAuthClientCreatedResponse


def _client() -> OAuthClient:
    return OAuthClient(
        client_id="hpc_test",
        name="POS sync",
        entity_key=None,
        scopes=["persons:read"],
        created_by="admin@example.com",
        created_at=datetime.now(UTC).replace(tzinfo=None),
        disabled_at=None,
        last_used_at=None,
        secrets=[],
    )


def test_token_endpoint_rejects_unsupported_grant_type() -> None:
    app = build_app()
    client = TestClient(app)

    res = client.post(
        "/v1/oauth/token",
        data={"grant_type": "password", "client_id": "hpc", "client_secret": "secret"},
    )

    assert res.status_code == 400
    assert res.json()["error"] == "unsupported_grant_type"


def test_token_endpoint_returns_oauth_error_for_bad_credentials() -> None:
    app = build_app()
    client = TestClient(app)

    with patch("src.routes.oauth.validate_client_credentials", new=AsyncMock(return_value=None)):
        res = client.post(
            "/v1/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "hpc_missing",
                "client_secret": "bad",
            },
        )

    assert res.status_code == 401
    assert res.json()["error"] == "invalid_client"


def test_token_endpoint_issues_access_token() -> None:
    app = build_app()
    client = TestClient(app)

    with patch(
        "src.routes.oauth.validate_client_credentials",
        new=AsyncMock(return_value=(_client(), ["persons:read"])),
    ), patch("src.routes.oauth.issue_client_access_token", return_value="jwt-token"):
        res = client.post(
            "/v1/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "hpc_test",
                "client_secret": "hps_secret",
                "scope": "persons:read",
            },
        )

    assert res.status_code == 200
    assert res.json() == {
        "access_token": "jwt-token",
        "token_type": "Bearer",
        "expires_in": 900,
        "scope": "persons:read",
    }


def test_jwks_endpoint_returns_keys() -> None:
    app = build_app()
    client = TestClient(app)

    with patch("src.routes.oauth.build_jwks", return_value={"keys": []}):
        res = client.get("/v1/oauth/jwks")

    assert res.status_code == 200
    assert res.json() == {"keys": []}
```

- [ ] **Step 2: Run route tests and verify they fail**

Run:

```bash
uv run pytest services/api/tests/test_oauth_routes.py -v
```

Expected: FAIL because OAuth routes are not registered.

- [ ] **Step 3: Create public OAuth routes**

Create `services/api/src/routes/oauth.py`:

```python
"""OAuth2 client-credentials endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Form, status
from fastapi.responses import JSONResponse

from src.auth.oauth_client_models import OAuthTokenResponse
from src.auth.oauth_clients import requested_scopes_or_default, validate_client_credentials
from src.auth.oauth_tokens import build_jwks, issue_client_access_token
from src.config import config

router = APIRouter(prefix="/v1/oauth", tags=["OAuth"])


def _oauth_error(status_code: int, error: str, description: str) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description}, status_code=status_code
    )


@router.post("/token", response_model=OAuthTokenResponse)
async def token(
    grant_type: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    scope: str | None = Form(default=None),
) -> OAuthTokenResponse | JSONResponse:
    """Issue an access token using OAuth2 client credentials."""
    if grant_type != "client_credentials":
        return _oauth_error(
            status.HTTP_400_BAD_REQUEST,
            "unsupported_grant_type",
            "Only grant_type=client_credentials is supported.",
        )
    validated = await validate_client_credentials(client_id, client_secret)
    if validated is None:
        return _oauth_error(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_client",
            "Invalid client credentials.",
        )
    client, assigned_scopes = validated
    granted_scopes = requested_scopes_or_default(scope, assigned_scopes)
    if granted_scopes is None:
        return _oauth_error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_scope",
            "Requested scope is not assigned to this client.",
        )
    expires_in = min(
        config.oauth_access_token_expiry_minutes,
        config.oauth_max_access_token_expiry_minutes,
    ) * 60
    access_token = issue_client_access_token(
        client, granted_scopes, expires_in_seconds=expires_in
    )
    return OAuthTokenResponse(
        access_token=access_token,
        expires_in=expires_in,
        scope=" ".join(granted_scopes),
    )


@router.get("/jwks")
async def jwks() -> dict[str, object]:
    """Return public signing keys for HyperP-issued access tokens."""
    return build_jwks()
```

- [ ] **Step 4: Create admin OAuth client routes**

Create `services/api/src/routes/oauth_clients.py`:

```python
"""Admin endpoints for managing OAuth machine clients."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from src.auth.deps import OAuthClientUser, require_admin
from src.auth.models import AuthUser
from src.auth.oauth_client_models import (
    CreateOAuthClientRequest,
    CreateOAuthClientSecretRequest,
    OAuthClient,
    OAuthClientCreatedResponse,
    OAuthClientSecretCreatedResponse,
)
from src.auth.oauth_clients import (
    create_oauth_client,
    create_oauth_client_secret,
    delete_oauth_client,
    disable_oauth_client,
    list_oauth_clients,
    revoke_oauth_client_secret,
)
from src.http_utils import http_error

router = APIRouter(prefix="/v1/admin/oauth-clients", tags=["Admin"])


@router.post("", response_model=OAuthClientCreatedResponse, status_code=201)
async def create_oauth_client_handler(
    body: CreateOAuthClientRequest,
    user: AuthUser | OAuthClientUser = Depends(require_admin),
) -> OAuthClientCreatedResponse:
    """Create an OAuth client and return its first secret once."""
    return await create_oauth_client(body, user)


@router.get("", response_model=list[OAuthClient])
async def list_oauth_clients_handler(
    _user: AuthUser | OAuthClientUser = Depends(require_admin),
) -> list[OAuthClient]:
    """List OAuth clients for admin management."""
    return await list_oauth_clients()


@router.post("/{client_id}/secrets", response_model=OAuthClientSecretCreatedResponse, status_code=201)
async def create_oauth_secret_handler(
    client_id: str,
    body: CreateOAuthClientSecretRequest,
    _user: AuthUser | OAuthClientUser = Depends(require_admin),
) -> OAuthClientSecretCreatedResponse:
    """Create another one-time secret for an OAuth client."""
    return await create_oauth_client_secret(client_id, body)


@router.post("/{client_id}/secrets/{secret_id}/revoke", status_code=204)
async def revoke_oauth_secret_handler(
    client_id: str,
    secret_id: str,
    request: Request,
    _user: AuthUser | OAuthClientUser = Depends(require_admin),
) -> None:
    """Revoke one OAuth client secret."""
    if not await revoke_oauth_client_secret(client_id, secret_id):
        raise http_error(404, "not_found", "OAuth client secret not found.", request)


@router.post("/{client_id}/disable", status_code=204)
async def disable_oauth_client_handler(
    client_id: str,
    request: Request,
    _user: AuthUser | OAuthClientUser = Depends(require_admin),
) -> None:
    """Disable an OAuth client."""
    if not await disable_oauth_client(client_id):
        raise http_error(404, "not_found", "OAuth client not found.", request)


@router.delete("/{client_id}", status_code=204)
async def delete_oauth_client_handler(
    client_id: str,
    _user: AuthUser | OAuthClientUser = Depends(require_admin),
) -> None:
    """Delete an OAuth client and its secrets."""
    await delete_oauth_client(client_id)
```

- [ ] **Step 5: Register routes in app**

In `services/api/src/app.py`:

1. Replace imports:

```python
from src.routes import api_keys as api_keys_routes
```

with:

```python
from src.routes import oauth, oauth_clients as oauth_client_routes
```

2. Include public OAuth routes near health/auth/public:

```python
app.include_router(oauth.router)
```

3. Replace:

```python
app.include_router(api_keys_routes.router, dependencies=active)
```

with:

```python
app.include_router(oauth_client_routes.router, dependencies=active)
```

- [ ] **Step 6: Run route tests**

Run:

```bash
uv run pytest services/api/tests/test_oauth_routes.py -v
```

Expected: PASS. If FastAPI reports missing `python-multipart`, add `python-multipart>=0.0.9,<1` to `services/api/pyproject.toml` using `uv add --package profile-unifier-api 'python-multipart>=0.0.9,<1'` and rerun.

- [ ] **Step 7: Run focused route lint**

Run:

```bash
uv run --package profile-unifier-api ruff check services/api/src/routes/oauth.py services/api/src/routes/oauth_clients.py services/api/src/app.py services/api/tests/test_oauth_routes.py
```

Expected: PASS or only fixable formatting/import issues. Apply `ruff format` if needed.

- [ ] **Step 8: Checkpoint, no commit**

Run:

```bash
git diff -- services/api/src/routes/oauth.py services/api/src/routes/oauth_clients.py services/api/src/app.py services/api/tests/test_oauth_routes.py
```

Expected: OAuth route additions/registration only. Do not commit.

---

## Task 6: Replace API-key dependency path with OAuth client Bearer auth

**Files:**
- Modify: `services/api/src/auth/deps.py`
- Modify: `services/api/src/app.py`
- Modify: `services/api/tests/test_oauth_routes.py`
- Delete: `services/api/src/auth/api_key_models.py`
- Delete: `services/api/src/auth/api_keys.py`
- Delete: `services/api/src/graph/queries/api_keys.py`
- Delete: `services/api/src/routes/api_keys.py`

- [ ] **Step 0: Remove legacy API-key config/env with backend path**

When replacing the API-key backend path in this task, remove the legacy API-key settings and environment defaults that Task 1 intentionally kept for incremental safety:

- `api_keys_enabled`, `api_key_secret`, and `api_key_header_name` from `services/api/src/config.py`
- `API_KEYS_ENABLED`, `API_KEY_SECRET`, and `API_KEY_HEADER_NAME` from API test env defaults
- `API_KEYS_ENABLED`, `API_KEY_SECRET`, and `API_KEY_HEADER_NAME` from compose `api.environment`

- [ ] **Step 1: Add failing dependency tests**

Append to `services/api/tests/test_oauth_routes.py`:

```python
from src.auth.deps import OAuthClientUser, get_current_user_or_oauth_client


def test_oauth_client_user_admin_scope_sets_admin_role() -> None:
    principal = OAuthClientUser(
        email="oauth:hpc_admin",
        google_sub="hpc_admin",
        role="admin",
        entity_key=None,
        display_name="Admin client",
        first_time=False,
        source="oauth_client",
        client_id="hpc_admin",
        key_scopes=["admin"],
    )

    assert principal.role == "admin"
    assert principal.key_scopes == ["admin"]


def test_oauth_client_user_non_admin_scope_is_employee_role() -> None:
    principal = OAuthClientUser(
        email="oauth:hpc_reader",
        google_sub="hpc_reader",
        role="employee",
        entity_key="fundbox",
        display_name="Reader client",
        first_time=False,
        source="oauth_client",
        client_id="hpc_reader",
        key_scopes=["persons:read"],
    )

    assert principal.role == "employee"
    assert principal.entity_key == "fundbox"
```

- [ ] **Step 2: Run dependency tests and verify they fail**

Run:

```bash
uv run pytest services/api/tests/test_oauth_routes.py::test_oauth_client_user_admin_scope_sets_admin_role services/api/tests/test_oauth_routes.py::test_oauth_client_user_non_admin_scope_is_employee_role -v
```

Expected: FAIL because `OAuthClientUser` does not exist.

- [ ] **Step 3: Modify auth dependency imports**

In `services/api/src/auth/deps.py`, replace:

```python
from src.auth.api_keys import check_scope, validate_api_key
```

with:

```python
from src.auth.oauth_clients import check_scope
from src.auth.oauth_tokens import verify_client_access_token
```

Remove all `Security`/header usage for `config.api_key_header_name` if no longer needed.

- [ ] **Step 4: Replace API-key principal with OAuth client principal**

Replace the `ApiKeyUser` class with:

```python
class OAuthClientUser(AuthUser):
    """AuthUser subclass for OAuth-client-authenticated callers."""

    source: str = "oauth_client"
    client_id: str
    key_scopes: list[str] = Field(default_factory=list)
```

- [ ] **Step 5: Replace `get_current_user_or_api_key`**

Replace `get_current_user_or_api_key` with `get_current_user_or_oauth_client` that uses only the Bearer token:

```python
async def get_current_user_or_oauth_client(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthUser | OAuthClientUser:
    """Authenticate either a Google ID token or a HyperP OAuth client token."""
    if credentials is None:
        raise http_error(401, "unauthorized", "Missing Authorization bearer token.", request)
    token = credentials.credentials

    try:
        claims = verify_client_access_token(token)
    except ValueError:
        return await get_current_user(request, credentials)

    role = "admin" if "admin" in claims.scopes else "employee"
    return OAuthClientUser(
        email=f"oauth:{claims.client_id}",
        google_sub=claims.client_id,
        role=role,
        entity_key=claims.entity_key,
        display_name=claims.client_id,
        first_time=False,
        source="oauth_client",
        client_id=claims.client_id,
        key_scopes=claims.scopes,
    )
```

- [ ] **Step 6: Update dependency function signatures**

Replace every `AuthUser | ApiKeyUser` annotation in `services/api/src/auth/deps.py` with `AuthUser | OAuthClientUser`, and replace `Depends(get_current_user_or_api_key)` with `Depends(get_current_user_or_oauth_client)`.

Keep `require_scope` behavior:

```python
scopes = getattr(user, "key_scopes", None) or []
if not check_scope(scopes, required):
    raise http_error(403, "forbidden", "This action requires an API scope.", request)
```

The error message can say OAuth scope instead of API scope:

```python
"This action requires an OAuth scope."
```

- [ ] **Step 7: Update startup constraint call**

In `services/api/src/app.py`, replace API-key startup import/call with:

```python
from src.auth.oauth_clients import ensure_oauth_client_constraints
```

and call `await ensure_oauth_client_constraints()` wherever `ensure_api_key_constraint()` was called.

- [ ] **Step 8: Delete API-key modules**

Delete:

```text
services/api/src/auth/api_key_models.py
services/api/src/auth/api_keys.py
services/api/src/graph/queries/api_keys.py
services/api/src/routes/api_keys.py
```

- [ ] **Step 9: Run grep to catch stale API-key backend references**

Run:

```bash
python - <<'PY'
from pathlib import Path
roots = [Path('services/api/src'), Path('services/api/tests')]
needles = ['ApiKey', 'api_key', 'API_KEY', 'X-Api-Key', 'api_keys']
for root in roots:
    for path in root.rglob('*'):
        if path.is_file() and path.suffix in {'.py', '.toml', '.yaml', '.yml'}:
            text = path.read_text(encoding='utf-8')
            for needle in needles:
                if needle in text:
                    print(f'{path}: contains {needle}')
PY
```

Expected: no backend source references except historical docs not in this grep. If references remain in active backend files, replace with OAuth naming.

- [ ] **Step 10: Run dependency and route tests**

Run:

```bash
uv run pytest services/api/tests/test_oauth_routes.py services/api/tests/test_oauth_tokens.py services/api/tests/test_oauth_clients.py -v
```

Expected: PASS.

- [ ] **Step 11: Run focused mypy/ruff**

Run:

```bash
uv run --package profile-unifier-api ruff check services/api/src/auth services/api/src/routes/oauth.py services/api/src/routes/oauth_clients.py services/api/src/app.py
uv run --package profile-unifier-api mypy --strict services/api/src
```

Expected: ruff PASS. Mypy may report pre-existing `types_sales.py`/`types_requests.py` failures noted in CLAUDE.md; fix OAuth-related failures only.

- [ ] **Step 12: Checkpoint, no commit**

Run:

```bash
git diff -- services/api/src/auth services/api/src/routes services/api/src/graph/queries services/api/src/app.py services/api/tests
```

Expected: API-key path removed and OAuth Bearer path in place. Do not commit.

---

## Task 7: Add explicit machine scopes to machine-callable routes

**Files:**
- Modify: `services/api/src/routes/ingest.py`
- Modify: `services/api/src/routes/persons.py`
- Modify: other route files only if they are intentionally server-to-server callable
- Modify: `services/api/tests/test_oauth_routes.py`

- [ ] **Step 1: Identify current machine-callable endpoints**

Use current API-key scope intent as the replacement target:

- `ingest:write`: ingestion submission and ingestion run mutation routes.
- `persons:read`: person list/detail/search/read-only person relationship endpoints.
- `persons:write`: person merge/link/share or write routes only when intended for machines.
- `admin`: admin management routes.

Do not add machine scopes to browser-only review or survivorship routes unless the current code already allowed API-key access intentionally.

- [ ] **Step 2: Add failing test for scope helper on a route dependency**

Append this focused test to `services/api/tests/test_oauth_routes.py`:

```python
from src.auth.deps import require_scope


def test_require_scope_accepts_oauth_client_with_matching_scope() -> None:
    dep = require_scope("persons:read")
    principal = OAuthClientUser(
        email="oauth:hpc_reader",
        google_sub="hpc_reader",
        role="employee",
        entity_key=None,
        display_name="Reader client",
        first_time=False,
        source="oauth_client",
        client_id="hpc_reader",
        key_scopes=["persons:read"],
    )

    assert dep is not None
    assert principal.key_scopes == ["persons:read"]
```

This test mainly locks the public API shape for `require_scope`; route-level behavior should be verified in integration tests once existing route fixtures are available.

- [ ] **Step 3: Add route dependencies for ingestion**

In `services/api/src/routes/ingest.py`, import:

```python
from src.auth.deps import require_scope
```

For ingestion write endpoints, add dependency parameters like:

```python
_scope_user: AuthUser | OAuthClientUser = Depends(require_scope("ingest:write"))
```

If the endpoint already has a user dependency, replace it with this scoped dependency only where machine access should be allowed. Keep human admin/employee entity checks intact by calling existing entity-specific dependencies where needed.

- [ ] **Step 4: Add route dependencies for person read/write endpoints**

In `services/api/src/routes/persons.py`, import:

```python
from src.auth.deps import require_scope
```

For list/detail/read-only endpoints intended for machines, add:

```python
_scope_user: AuthUser | OAuthClientUser = Depends(require_scope("persons:read"))
```

For person write endpoints intended for machines, add:

```python
_scope_user: AuthUser | OAuthClientUser = Depends(require_scope("persons:write"))
```

- [ ] **Step 5: Run route-level lint and tests**

Run:

```bash
uv run pytest services/api/tests/test_oauth_routes.py -v
uv run --package profile-unifier-api ruff check services/api/src/routes/ingest.py services/api/src/routes/persons.py services/api/src/auth/deps.py
```

Expected: PASS after adjusting imports and unused variables.

- [ ] **Step 6: Checkpoint, no commit**

Run:

```bash
git diff -- services/api/src/routes/ingest.py services/api/src/routes/persons.py services/api/tests/test_oauth_routes.py
```

Expected: explicit OAuth scope gates on intended machine-callable routes. Do not commit.

---

## Task 8: Replace frontend API-key admin surface with OAuth clients

**Files:**
- Create: `services/frontend/src/app/admin/oauth-clients/page.tsx`
- Create: `services/frontend/src/app/bff/admin/oauth-clients/route.ts`
- Create: `services/frontend/src/app/bff/admin/oauth-clients/[clientId]/route.ts`
- Create: `services/frontend/src/app/bff/admin/oauth-clients/[clientId]/secrets/route.ts`
- Create: `services/frontend/src/app/bff/admin/oauth-clients/[clientId]/secrets/[secretId]/route.ts`
- Modify: `services/frontend/src/lib/api-types-ops.ts`
- Modify: `services/frontend/src/app/admin/page.tsx`
- Delete: `services/frontend/src/app/admin/api-keys/page.tsx`
- Delete: `services/frontend/src/app/bff/admin/api-keys/route.ts`
- Delete: `services/frontend/src/app/bff/admin/api-keys/[keyId]/route.ts`

- [ ] **Step 1: Replace TypeScript API-key types**

In `services/frontend/src/lib/api-types-ops.ts`, replace the API-key section with:

```typescript
// --- OAuth clients (server-to-server auth) ---
// Mirrors services/api/src/auth/oauth_client_models.py

export interface OAuthClientSecret {
  secret_id: string;
  secret_prefix: string;
  created_at: string | null;
  expires_at: string | null;
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
  secrets: OAuthClientSecret[];
}

export interface OAuthClientCreated {
  client_id: string;
  client_secret: string;
  secret_id: string;
  secret_prefix: string;
  name: string;
  scopes: string[];
  secret_expires_at: string | null;
}

export interface CreateOAuthClientRequest {
  name: string;
  entity_key: string | null;
  scopes: string[];
  secret_expires_in_days: number | null;
}

export interface CreateOAuthClientSecretRequest {
  expires_in_days: number | null;
}

export interface OAuthClientSecretCreated {
  client_id: string;
  client_secret: string;
  secret_id: string;
  secret_prefix: string;
  expires_at: string | null;
}

export const OAUTH_CLIENT_SCOPES: readonly string[] = [
  "persons:read",
  "persons:write",
  "ingest:write",
  "admin",
] as const;
```

- [ ] **Step 2: Create BFF list/create route**

Create `services/frontend/src/app/bff/admin/oauth-clients/route.ts`:

```typescript
import type { NextResponse } from "next/server";

import type {
  CreateOAuthClientRequest,
  OAuthClient,
  OAuthClientCreated,
} from "@/lib/api-types-ops";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function GET(): Promise<NextResponse> {
  return proxyToApi<OAuthClient[]>("/admin/oauth-clients");
}

export async function POST(request: Request): Promise<NextResponse> {
  const body: CreateOAuthClientRequest = await request.json();
  return proxyToApi<OAuthClientCreated>("/admin/oauth-clients", {
    method: "POST",
    body,
  });
}
```

- [ ] **Step 3: Create BFF disable/delete route**

Create `services/frontend/src/app/bff/admin/oauth-clients/[clientId]/route.ts`:

```typescript
import type { NextResponse } from "next/server";

import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function DELETE(
  _request: Request,
  context: { params: Promise<{ clientId: string }> },
): Promise<NextResponse> {
  const { clientId } = await context.params;
  return proxyToApi<void>(`/admin/oauth-clients/${encodeURIComponent(clientId)}`, {
    method: "DELETE",
  });
}

export async function POST(
  _request: Request,
  context: { params: Promise<{ clientId: string }> },
): Promise<NextResponse> {
  const { clientId } = await context.params;
  return proxyToApi<void>(
    `/admin/oauth-clients/${encodeURIComponent(clientId)}/disable`,
    { method: "POST" },
  );
}
```

- [ ] **Step 4: Create BFF secret rotation route**

Create `services/frontend/src/app/bff/admin/oauth-clients/[clientId]/secrets/route.ts`:

```typescript
import type { NextResponse } from "next/server";

import type {
  CreateOAuthClientSecretRequest,
  OAuthClientSecretCreated,
} from "@/lib/api-types-ops";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function POST(
  request: Request,
  context: { params: Promise<{ clientId: string }> },
): Promise<NextResponse> {
  const { clientId } = await context.params;
  const body: CreateOAuthClientSecretRequest = await request.json();
  return proxyToApi<OAuthClientSecretCreated>(
    `/admin/oauth-clients/${encodeURIComponent(clientId)}/secrets`,
    { method: "POST", body },
  );
}
```

- [ ] **Step 5: Create BFF secret revoke route**

Create `services/frontend/src/app/bff/admin/oauth-clients/[clientId]/secrets/[secretId]/route.ts`:

```typescript
import type { NextResponse } from "next/server";

import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function POST(
  _request: Request,
  context: { params: Promise<{ clientId: string; secretId: string }> },
): Promise<NextResponse> {
  const { clientId, secretId } = await context.params;
  return proxyToApi<void>(
    `/admin/oauth-clients/${encodeURIComponent(clientId)}/secrets/${encodeURIComponent(secretId)}/revoke`,
    { method: "POST" },
  );
}
```

- [ ] **Step 6: Create OAuth clients admin page**

Copy `services/frontend/src/app/admin/api-keys/page.tsx` to `services/frontend/src/app/admin/oauth-clients/page.tsx`, then make these exact transformations:

- Rename component `ApiKeysAdminPage` to `OAuthClientsAdminPage`.
- Replace `ApiKey` with `OAuthClient`.
- Replace `ApiKeyCreated` with `OAuthClientCreated | OAuthClientSecretCreated` where the one-time secret dialog is shared.
- Replace `CreateApiKeyRequest` with `CreateOAuthClientRequest`.
- Replace `API_KEY_SCOPES` with `OAUTH_CLIENT_SCOPES`.
- Replace fetch path `/bff/admin/api-keys` with `/bff/admin/oauth-clients`.
- Rename visible text:
  - `API keys` → `OAuth clients`
  - `API key` → `OAuth client`
  - `key` → `client secret` when referring to plain secret material
  - `key_prefix` → `secret_prefix`
- Render client id in monospace:

```tsx
<Typography variant="body2" fontWeight={600} fontFamily="monospace">
  {props.client.client_id}
</Typography>
```

- Render secrets as nested rows using `props.client.secrets.map((secret) => ...)` with revoke buttons calling:

```typescript
await bffFetch<void>(
  `/bff/admin/oauth-clients/${encodeURIComponent(props.client.client_id)}/secrets/${encodeURIComponent(secret.secret_id)}`,
  { method: "POST" },
);
```

- Add a rotate-secret button that calls:

```typescript
const created = await bffFetch<OAuthClientSecretCreated>(
  `/bff/admin/oauth-clients/${encodeURIComponent(props.client.client_id)}/secrets`,
  { method: "POST", body: { expires_in_days: 365 } },
);
setNewSecret(created);
```

- Disable client by POSTing to `/bff/admin/oauth-clients/${client_id}`.
- Delete client by DELETEing `/bff/admin/oauth-clients/${client_id}`.

- [ ] **Step 7: Update admin landing page link**

In `services/frontend/src/app/admin/page.tsx`, replace the API-key card/link text/path with OAuth clients:

```typescript
href: "/admin/oauth-clients",
title: "OAuth clients",
description: "Manage server-to-server OAuth client credentials and scopes.",
```

- [ ] **Step 8: Delete old frontend API-key files**

Delete:

```text
services/frontend/src/app/admin/api-keys/page.tsx
services/frontend/src/app/bff/admin/api-keys/route.ts
services/frontend/src/app/bff/admin/api-keys/[keyId]/route.ts
```

- [ ] **Step 9: Run frontend checks**

Run:

```bash
cd services/frontend && npm run typecheck
cd services/frontend && npm run lint
```

Expected: PASS. If lint warnings exceed the budget, fix new OAuth client page warnings rather than increasing the warning budget.

- [ ] **Step 10: Checkpoint, no commit**

Run:

```bash
git diff -- services/frontend/src/lib/api-types-ops.ts services/frontend/src/app/admin services/frontend/src/app/bff/admin
```

Expected: API-key UI/BFF removed and OAuth client UI/BFF added. Do not commit.

---

## Task 9: Update docs and OpenAPI

**Files:**
- Modify: `docs/profile-unifier-api-spec.md`
- Modify: `docs/profile-unifier-openapi-3.1.yaml`
- Modify: `docs/superpowers/specs/2026-05-04-oauth2-client-credentials-design.md` only if implementation decisions changed

- [ ] **Step 1: Update prose API spec**

In `docs/profile-unifier-api-spec.md`, replace server-to-server API-key sections with OAuth client credentials documentation:

```markdown
## Server-to-server authentication

Machine callers use OAuth2 client credentials. Admins create OAuth clients under `/v1/admin/oauth-clients`, assign scopes, and receive a one-time `client_secret`.

Token request:

```http
POST /v1/oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id=hpc_...&client_secret=hps_...&scope=persons:read
```

Successful token response:

```json
{
  "access_token": "eyJ...",
  "token_type": "Bearer",
  "expires_in": 900,
  "scope": "persons:read"
}
```

Use the access token on API calls:

```http
Authorization: Bearer eyJ...
```

Public signing keys are available at `GET /v1/oauth/jwks`.
```

Also document the supported scopes: `persons:read`, `persons:write`, `ingest:write`, `admin`.

- [ ] **Step 2: Update OpenAPI security schemes**

In `docs/profile-unifier-openapi-3.1.yaml`:

1. Remove all `apiKeyAuth` security requirements.
2. Remove the `apiKeyAuth` security scheme.
3. Keep `bearerAuth` and describe it as Google ID tokens for humans or HyperP-issued OAuth client JWTs for machines.
4. Add `/v1/oauth/token` with `application/x-www-form-urlencoded` request body and `OAuthTokenResponse` response.
5. Add `/v1/oauth/jwks` returning a JWKS object.
6. Replace `/v1/admin/api-keys` paths with `/v1/admin/oauth-clients` paths.

- [ ] **Step 3: Search docs for stale API-key auth references**

Run:

```bash
python - <<'PY'
from pathlib import Path
for path in Path('docs').rglob('*.md'):
    text = path.read_text(encoding='utf-8')
    for needle in ['X-Api-Key', 'apiKeyAuth', 'API key', 'API keys']:
        if needle in text:
            print(f'{path}: {needle}')
PY
```

Expected: no active auth-contract references except historical design notes if any. Update active docs to OAuth wording.

- [ ] **Step 4: Checkpoint, no commit**

Run:

```bash
git diff -- docs/profile-unifier-api-spec.md docs/profile-unifier-openapi-3.1.yaml docs/superpowers/specs/2026-05-04-oauth2-client-credentials-design.md
```

Expected: docs match the implemented OAuth client credentials contract. Do not commit.

---

## Task 10: Final verification and cleanup

**Files:**
- All changed files

- [ ] **Step 1: Remove stale API-key files/references**

Run:

```bash
python - <<'PY'
from pathlib import Path
roots = [Path('services/api/src'), Path('services/frontend/src'), Path('docs')]
needles = ['X-Api-Key', 'apiKeyAuth', 'API_KEYS_ENABLED', 'API_KEY_SECRET', 'API_KEY_HEADER_NAME', 'ApiKey', 'api_keys']
for root in roots:
    for path in root.rglob('*'):
        if path.is_file() and path.suffix in {'.py', '.ts', '.tsx', '.md', '.yaml', '.yml'}:
            text = path.read_text(encoding='utf-8')
            for needle in needles:
                if needle in text:
                    print(f'{path}: contains {needle}')
PY
```

Expected: no stale API-key references in active code. If docs intentionally mention old API keys as removed history, keep only if clearly marked as migration history.

- [ ] **Step 2: Run backend unit tests**

Run:

```bash
uv run pytest services/api/tests/test_oauth_config.py services/api/tests/test_oauth_clients.py services/api/tests/test_oauth_tokens.py services/api/tests/test_oauth_routes.py -v
```

Expected: PASS.

- [ ] **Step 3: Run full API tests**

Run:

```bash
uv run pytest services/api/tests -v
```

Expected: PASS.

- [ ] **Step 4: Run backend lint and typecheck**

Run:

```bash
uv run --package profile-unifier-api ruff check services/api/src services/api/tests
uv run --package profile-unifier-api mypy --strict services/api/src
```

Expected: ruff PASS. Mypy may show the known pre-existing failures in `types_sales.py` and `types_requests.py`; any OAuth-related failures must be fixed.

- [ ] **Step 5: Run frontend checks**

Run:

```bash
cd services/frontend && npm run typecheck
cd services/frontend && npm run lint
```

Expected: PASS.

- [ ] **Step 6: Docker rebuild smoke test**

Because Python and TypeScript changed, rebuild with no cache per repo instructions:

```bash
docker compose build --no-cache api frontend
docker compose up -d api frontend
```

Expected: both services build and start.

- [ ] **Step 7: Manual OAuth golden path smoke test**

With services running, use the admin UI to:

1. Open `/admin/oauth-clients`.
2. Create a client with `persons:read`.
3. Copy the one-time secret.
4. Request a token:

```bash
curl -s -X POST http://localhost/api/v1/oauth/token \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode 'grant_type=client_credentials' \
  --data-urlencode 'client_id=<client_id>' \
  --data-urlencode 'client_secret=<client_secret>' \
  --data-urlencode 'scope=persons:read'
```

Expected: JSON response with `access_token`, `token_type: Bearer`, `expires_in`, and `scope`.

Then call a read endpoint with the token:

```bash
curl -s http://localhost/api/v1/persons \
  -H "Authorization: Bearer <access_token>"
```

Expected: authorized response for `persons:read`.

- [ ] **Step 8: Manual revocation smoke test**

In the admin UI:

1. Rotate a secret.
2. Revoke the old secret.
3. Confirm the old secret can no longer obtain a token.
4. Confirm the new secret can obtain a token.
5. Disable the client.
6. Confirm no secret for that client can obtain a token.

- [ ] **Step 9: Final git status and no commit**

Run:

```bash
git status --short
git diff --stat
```

Expected: all intended files changed, no unexpected generated files, no commits created.
