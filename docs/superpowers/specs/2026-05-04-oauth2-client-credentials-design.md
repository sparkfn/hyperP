# OAuth2 Client Credentials Design

## Purpose

Replace HyperP's existing server-to-server API keys with OAuth2 client credentials. HyperP will issue short-lived signed JWT access tokens to registered machine clients, and API callers will use those tokens with the existing `Authorization: Bearer` header.

## Goals

- Hard-replace `X-Api-Key` based server-to-server authentication.
- Let admins create and manage OAuth clients from the existing admin surface.
- Support multiple hashed, rotatable client secrets per OAuth client.
- Issue HyperP-signed JWT access tokens from `POST /v1/oauth/token`.
- Publish public signing keys via JWKS so other services can validate HyperP-issued tokens.
- Preserve existing human Google ID-token authentication for browser users.
- Map OAuth clients to existing machine scopes and optional entity scoping.

## Non-goals

- No automatic migration of existing API keys.
- No authorization-code, refresh-token, password, device-code, or browser OAuth flows.
- No full OIDC discovery surface in the initial implementation.
- No retrievable plain client secrets after creation or rotation.

## Architecture

HyperP becomes its own OAuth2 client-credentials issuer for machine callers. Admins create OAuth clients, assign scopes and optional `entity_key`, and manage one or more client secrets. Machine clients exchange `client_id` and `client_secret` for a short-lived JWT access token at `POST /v1/oauth/token`.

The FastAPI auth layer will accept two Bearer-token families:

1. Existing Google ID tokens for human/browser users.
2. HyperP-issued client-credentials JWTs for machine callers.

Machine tokens resolve to an OAuth client principal with `source = "oauth_client"`, `client_id`, `scopes`, and optional `entity_key`. Existing route gates continue to work through the shared principal shape, but API-key-specific auth classes, routes, config, headers, and OpenAPI schemes are removed.

HyperP signs access tokens with RS256. The active private key signs tokens with a `kid` header, and public keys are exposed at `GET /v1/oauth/jwks` for verification by HyperP and other services.

## Backend Components

- `auth/oauth_clients.py`: create/list/disable/delete OAuth clients, validate client credentials, manage secret rotation, and update last-used timestamps.
- `auth/oauth_tokens.py`: issue and verify HyperP JWT access tokens, select signing keys by `kid`, build token claims, and expose JWKS data.
- `auth/deps.py`: replace the API-key path with OAuth client Bearer-token validation while keeping Google ID-token validation for human users.
- `routes/oauth.py`: unauthenticated OAuth endpoints: `POST /v1/oauth/token` and `GET /v1/oauth/jwks`.
- `routes/oauth_clients.py`: admin endpoints replacing `routes/api_keys.py`.
- `graph/queries/oauth_clients.py`: Neo4j constraints and queries for OAuth clients and client secrets.
- Frontend admin OAuth clients page: replaces API-key management UI and supports create, list, disable/delete, add secret, revoke secret.
- Docs/OpenAPI: remove API-key auth and document OAuth client credentials.

## Data Model

OAuth clients are stored as Neo4j nodes:

```cypher
(:OAuthClient {
  client_id,
  name,
  entity_key,
  scopes,
  created_by,
  created_at,
  disabled_at,
  last_used_at
})
```

Client secrets are separate child nodes:

```cypher
(:OAuthClientSecret {
  secret_id,
  secret_hash,
  secret_prefix,
  created_at,
  expires_at,
  revoked_at,
  last_used_at
})
```

Relationship:

```cypher
(:OAuthClient)-[:HAS_SECRET]->(:OAuthClientSecret)
```

Separate secret nodes support multiple active secrets, per-secret revocation, per-secret expiry, per-secret last-used tracking, and clear audit history. Plain secrets are returned once only when created or rotated.

## Token Endpoint Flow

1. Admin creates an OAuth client with `name`, `scopes`, optional `entity_key`, and optional secret expiry. HyperP returns `client_id` and the first `client_secret` once.
2. Machine caller sends `POST /v1/oauth/token` with `grant_type=client_credentials`, `client_id`, `client_secret`, and optional `scope`.
3. HyperP validates that auth is enabled, the grant type is exactly `client_credentials`, the client exists and is not disabled, the secret matches one active non-expired non-revoked secret, and requested scopes are a subset of assigned scopes.
4. HyperP issues a signed JWT access token.
5. Machine caller uses `Authorization: Bearer <access_token>` on API requests.
6. Auth dependency verifies signature and standard claims, then checks the client is still active and the token `jti` has not been revoked.

## JWT Claims

Access tokens include:

- `iss`: configured HyperP issuer.
- `aud`: configured API audience.
- `sub`: OAuth `client_id`.
- `client_id`: OAuth client identifier.
- `scope`: space-delimited OAuth scope string.
- `scopes`: list form for internal authorization convenience.
- `entity_key`: present when the client is entity-scoped.
- `iat`, `nbf`, `exp`: standard timestamps.
- `jti`: unique token identifier for revocation checks.

Access-token lifetime is configurable with a safe short default and a maximum allowed lifetime.

## Authorization

OAuth clients do not become unconditional admin users. A machine principal satisfies admin gates only when its token includes the `admin` scope. Otherwise, authorization is driven by explicit scopes such as `persons:read`, `persons:write`, and `ingest:write`, plus optional `entity_key` checks.

During implementation, routes that are meant to be callable by machines should gain explicit `require_scope(...)` dependencies where they currently rely only on broad active-user registration. Human role checks continue to apply to Google-authenticated users.

## Error Handling

`POST /v1/oauth/token` returns OAuth-compatible JSON errors rather than the project response envelope, because standards-compliant OAuth clients expect `error` and `error_description` at the top level:

- `invalid_request`
- `invalid_client`
- `invalid_scope`
- `unsupported_grant_type`

Successful token responses are also top-level OAuth JSON with `access_token`, `token_type: "Bearer"`, `expires_in`, and `scope`, plus `Cache-Control: no-store` and `Pragma: no-cache` headers.

Invalid client credentials return a generic 401 so callers cannot distinguish unknown `client_id` from an incorrect `client_secret`. Disabled clients, revoked secrets, expired secrets, revoked token JTIs, invalid audiences, expired tokens, and bad signatures fail closed.

Admin endpoints use the project's normal API errors such as `not_found`, `forbidden`, and `invalid_request`.

## Migration

This is a hard replacement. The implementation removes:

- `API_KEYS_ENABLED`
- `API_KEY_SECRET`
- `API_KEY_HEADER_NAME`
- `X-Api-Key` auth
- API-key routes and UI labels
- `apiKeyAuth` OpenAPI security scheme

The implementation adds OAuth config for issuer, audience, access-token default/max lifetime, signing key material, and active signing key ID. Startup creates OAuth client and secret constraints/indexes. Existing API keys are not migrated automatically; admins create OAuth clients and distribute new credentials.

## Testing

Backend tests should cover:

- client creation and one-time secret return
- secret hashing and credential validation
- secret rotation, revocation, expiry, and last-used updates
- client disable/delete behavior
- requested-scope subset checks
- JWT claim construction and signing-key `kid`
- token endpoint success and failure paths
- Bearer-token validation for HyperP JWTs
- disabled-client, revoked-token, invalid-audience, expired-token, and bad-signature rejection
- machine route authorization by scope and entity

Frontend checks should cover typecheck/lint plus the admin UI golden path: create client, copy one-time secret, add secret, revoke secret, disable/delete client.

Docs/OpenAPI checks should confirm API-key auth is gone and OAuth client credentials are documented.
