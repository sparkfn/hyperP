# Machine OAuth2 Remodel — Design

**Date:** 2026-06-10
**Status:** Approved (design); implementation plan pending
**Scope:** Machine (server-to-server) OAuth2 client credentials only. Google/human auth is out of scope except for two adjacent bug fixes noted below.

## Goal

Remodel the machine OAuth2 subsystem so that:

1. **Single active secret per client.** Rotating a secret revokes the previous one. Secrets no longer carry an expiry.
2. **Rotation kills outstanding tokens.** When a secret is rotated, every access token minted under the old secret is immediately invalid.
3. **Admin-configurable access-token lifetime per client.** Set on creation, editable later, bounded 5 minutes – 24 hours.
4. **Token tracking + manual revocation.** Admins can list a client's currently-valid access tokens with issued/expires/last-used timestamps and last-used IP address, and revoke any token manually.

All token-management surfaces are **human-admin only** (`require_human_admin`), consistent with the rest of OAuth client management.

## Decisions (from brainstorming)

| Question | Decision |
|---|---|
| Rotation vs. outstanding tokens | Tokens die with the secret. |
| Token-lifetime bounds | 300–86400 seconds (5 min – 24 h), per client. |
| Who can view/revoke tokens | Human admins only. |
| Token list contents | Valid tokens only, auto-cleanup of expired/revoked. |
| Existing-client migration | Wipe and recreate (no backfill). |
| Last-used IP source | `X-Forwarded-For` (already forwarded by nginx), trusted via uvicorn proxy headers. |
| Admin scope-narrowing fix | Out of scope — flagged as a follow-up. |

## Data Model

### `(:OAuthClient)`

Adds one field:

- `access_token_ttl_seconds: int` — per-client access-token lifetime. Set on creation, editable via `PATCH`. Validated **300 ≤ x ≤ 86400**.

The global env vars `OAUTH_ACCESS_TOKEN_EXPIRY_MINUTES` / `OAUTH_MAX_ACCESS_TOKEN_EXPIRY_MINUTES` are demoted: the former becomes only the **default seed** for the create form; the latter is removed as an enforcement point (the 86400 hard bound lives in the model validator). Token `exp` is computed from `client.access_token_ttl_seconds`, never the global config.

### `(:OAuthClientSecret)`

- **Drop `expires_at`.** Secrets do not expire.
- Keep `secret_id`, `secret_hash`, `secret_prefix`, `created_at`, `revoked_at`, `last_used_at`.
- **Invariant: at most one secret per client has `revoked_at IS NULL`.** Enforced by the rotation transaction (revoke-then-create), not a DB constraint.
- `is_secret_usable` collapses to `revoked_at IS NULL` (the expiry branch is deleted).

### Access-token registry (Redis, no Neo4j nodes)

On issuance:
- `HSET oauth_token:{jti}` → `{ client_id, secret_id, issued_at, expires_at, scope, last_used_at, last_used_ip }`
- `EXPIRE oauth_token:{jti} {ttl_seconds}` — TTL auto-cleanup gives "valid only" for free.
- `SADD oauth_client_tokens:{client_id} {jti}` — per-client index for listing. (Stale jtis whose hash has expired are skipped/pruned on read.)

The access-token **JWT gains a `secret_id` claim** naming the minting secret. This is the kill-switch mechanic (see Auth Flow).

## Auth Flow Changes

### Issuance (`POST /v1/oauth/token`)

1. Validate credentials (unchanged path), obtain client + current active secret.
2. `exp = now + client.access_token_ttl_seconds`.
3. Stamp `secret_id` into the JWT payload.
4. Write the Redis registry entry + add jti to `oauth_client_tokens:{client_id}`.

### Verification (`get_current_user_or_oauth_client`)

Existing live-client re-validation (client exists, not disabled, scopes still assigned, entity_key match) **plus**:

1. **`secret_id` still current** — the token's `secret_id` claim must equal the client's current active (non-revoked) secret. A rotated/revoked secret → reject. *This delivers "tokens die with the secret" with no enumeration.*
2. **Not individually revoked** — `is_token_revoked(jti)` (currently dead code for machine tokens) now fires because manual revocation writes the jti to the revoked store.
3. **Touch last-used** — refresh `last_used_at` and `last_used_ip` on the registry hash (best-effort, non-blocking; failures logged, never fatal).

### Client IP capture

`client_ip(request)` helper in `http_utils.py`: return the left-most entry of `X-Forwarded-For` if present, else `request.client.host`. nginx already forwards `X-Forwarded-For`/`X-Real-IP` on the `/api/oauth2/` block, so no nginx change. Uvicorn is configured to trust proxy headers (see Infra).

## API Surface

All under `/v1/admin/oauth-clients`, all `require_human_admin`.

| Method & path | Purpose | Notes |
|---|---|---|
| `POST ""` | Create client + first secret | Body drops `secret_expires_in_days`; adds `access_token_ttl_seconds`. Returns one-time secret. |
| `PATCH /{client_id}` | Edit client | `access_token_ttl_seconds`, `name`, `scopes`. |
| `POST /{client_id}/rotate-secret` | Rotate secret | Mints new secret, revokes previous, in one transaction. Returns one-time plaintext. Replaces `POST /{client_id}/secrets`. |
| `GET /{client_id}/tokens` | List live tokens | `jti`, `issued_at`, `expires_at`, `last_used_at`, `last_used_ip`, `scope`. Valid-only. |
| `POST /{client_id}/tokens/{jti}/revoke` | Revoke one token | Writes jti to revoked store + deletes registry hash + removes from client set. |
| `POST /{client_id}/disable` | Disable client | Unchanged. |
| `DELETE /{client_id}` | Delete client | Unchanged; also clears the client's Redis token registry. |

**Removed:** `POST /{client_id}/secrets` and `POST /{client_id}/secrets/{secret_id}/revoke` (rotation subsumes both).

### Model changes (`oauth_client_models.py`)

- `CreateOAuthClientRequest`: remove `secret_expires_in_days`; add `access_token_ttl_seconds: int = Field(default=<seed>, ge=300, le=86400)`.
- New `UpdateOAuthClientRequest` (all-optional `name`, `scopes`, `access_token_ttl_seconds`).
- `OAuthClientSecret`: drop `expires_at`.
- New `OAuthAccessTokenView` (jti, issued_at, expires_at, last_used_at, last_used_ip, scope).
- `OAuthClient`: add `access_token_ttl_seconds`; `secrets` now holds 0–1 active + historical revoked (or simplify to a single `secret` projection — decided in plan).

## UI (`services/frontend2/src/app/admin/oauth`)

- **Create modal:** replace the "Expires in (days)" input with **"Access token lifetime"** (minutes; default seeded from config; client-validated 5 min – 24 h, i.e. 5–1440 min).
- **Client card → expand/detail view:**
  - Current secret prefix + **Rotate secret** button (confirm dialog warning that rotation invalidates all live tokens).
  - Editable **token lifetime** field (PATCH).
  - **Tokens** table: issued / expires / last used / last-used IP / **Revoke** per row. Valid-only, refreshable.
- **Types (`api-types-ops.ts`):** mirror all model changes; add `OAuthAccessTokenView`, `UpdateOAuthClientRequest`; drop `*expires*` secret fields.
- **BFF routes:** add handlers for rotate-secret, PATCH client, list tokens, revoke token under `src/app/bff/admin/oauth-clients/[clientId]/`.

> Note: `frontend` (legacy v1) is not updated unless it currently renders OAuth client management. Confirm during planning; default is frontend2-only.

## Migration & Infra

- **Migration (startup):** a routine in the OAuth constraints/setup path deletes all `(:OAuthClient)` and `(:OAuthClientSecret)` nodes and clears Redis keys matching `oauth_token:*` and `oauth_client_tokens:*`. Wipe-and-recreate; admins re-provision. Runs once and is idempotent (no-op when already empty).
- **Uvicorn proxy headers (`src/main.py`):** `uvicorn.run(..., proxy_headers=True, forwarded_allow_ips="*")` (or the docker network CIDR), so `X-Forwarded-For` from nginx is trusted. `forwarded_allow_ips` scoped to the internal proxy, since the API is never exposed directly.
- **docker-compose sync:** if any env var is added/removed, apply to both root `docker-compose.yml` and `.docker/staging/docker-compose.yml` in the same commit.

## Adjacent Bug Fixes (folded in)

Both are in code this remodel already touches:

1. **Redis revocation un-revocation bug** (`auth/revoke.py`): replace the shared `revoked_tokens` SET + overwriting `EXPIREAT` with **one key per jti** (`SET revoked:{jti} 1 EXAT {exp}`). `is_token_revoked` becomes an `EXISTS revoked:{jti}` check. Eliminates the bug where revoking a sooner-expiring token deletes the whole set and un-revokes others.
2. **`_USER_CACHE` token comparison** (`auth/deps.py`): the cache read must compare the stored raw token (`cached[0] == token`) before returning the cached user, closing the jti-collision auth-bypass window. (Affects the Google path but is adjacent and one line.)

## Out of Scope (follow-ups)

- **Admin scope-narrowing** (review finding #2): an admin-scoped client requesting a narrowed token still gets escalated to `admin` at verify time. Tracked separately.
- Self-service token listing/revocation for machine clients (only admins manage tokens here).
- Two-key JWKS grace window for signing-key rotation.
- RFC 6749 niceties (`WWW-Authenticate` header, HTTP Basic client auth).

## Testing

- Rotation revokes previous secret and **rejects tokens minted under it** on the next request (secret_id mismatch).
- `access_token_ttl_seconds` bound validation (reject <300, >86400) at model and API layer.
- Token registry reflects issuance, manual revocation, and TTL expiry; list returns valid-only.
- Manual revoke → `is_token_revoked` rejects the jti.
- `client_ip` parses `X-Forwarded-For` left-most, falls back to socket host.
- Migration wipes existing nodes and Redis keys; idempotent on empty.
- Revocation-store per-jti keys: revoking an earlier-expiry token does **not** un-revoke a later one (regression test for the folded-in bug).
- Remove/rewrite existing multi-secret and secret-expiry tests.
