# WhatsAdmin API Ingestion Design

## Objective

Replace HyperP's direct WhatsAdmin PostgreSQL dependency in API mode with an
authenticated, cursor-paginated extraction contract. Preserve the existing
database connector for batch mode and support incremental ingestion through a
`changedSince` watermark. Keep Eko and Speedzone authentication and extraction
state strictly isolated.

## Scope

This change adds dedicated HyperP integration endpoints to WhatsAdmin and an
API-backed `whatsapp_chat` connector to HyperP. It does not change chat LLM
extraction, source envelope identities, exclusion rules, or database-mode
ingestion.

## WhatsAdmin API

### Session discovery

`POST /api/integrations/hyperp/sessions/query` returns ready WhatsApp sessions
visible to the authenticated API key. Each item contains the stable session ID,
organization ID and name, WhatsApp user ID, and expected phone number. The
response uses the existing response envelope and cursor pagination conventions.

### Conversation extraction

`POST /api/integrations/hyperp/chats/query` accepts:

- `sessionId`: the session to extract;
- `changedSince`: an optional ISO 8601 exclusive lower bound;
- `cursor`: the opaque continuation cursor; and
- `limit`: a bounded page size.

Each result is a denormalized chat bundle containing chat metadata, ordered
non-empty messages, resolved participant identity data, and `changedAt`. A chat
qualifies when the chat row or any included message changed after
`changedSince`.

Pages use stable `(changedAt, chatId)` ordering. The first response fixes an
upper snapshot timestamp, which is encoded into subsequent cursors. Records
created or updated after that timestamp are deferred to the next run, so a page
sequence represents one consistent extraction window.

The API accepts handle API keys only and requires the explicit, non-default
`hyperp:extract` handle permission. Organization and handle-session isolation
prevent a caller from enumerating or extracting sessions outside its scope.

## HyperP Connector

HyperP adds typed request, response, and bundle models plus a WhatsAdmin API
client. `get_connector("whatsapp_chat", mode="api")` selects the API-backed
connector; batch mode continues to select the existing PostgreSQL connector.
API extraction jobs accept an optional existing HyperP `entity_key`. Passing
`eko` or `speedzone` extracts only that entity. Omitting `entity_key` resolves
both credentials before making any request and then extracts both entities
independently. Any other entity is rejected.

One typed resolver owns credential selection. It returns only the requested
entity's credential, rejects unknown entities, and fails without fallback when
the requested credential is absent or invalid. A combined job fails credential
resolution atomically, before either entity is queried, so it cannot silently
perform a partial cross-entity run. Errors and logs identify configuration
fields or entity keys but never include plaintext API keys.

For each resolved entity, the connector sends its `hk_...` credential in the
`X-API-Key` header, enumerates every ready session visible to that handle, and
pages through changed chat bundles for each returned `sessionId`. Multiple
sessions for one entity reuse that entity's handle key. The connector rejects a
session whose returned organization maps to a different HyperP entity. It then
converts valid bundles into the existing internal chat-bundle form. Existing
transcript formatting, LLM batch extraction, exclusions, entity mapping, and
source-envelope builders run unchanged.

Configuration supplies a shared WhatsAdmin base URL, separate Eko and Speedzone
API keys, per-entity enable flags, request timeout, and page size. Enabling an
entity activates startup validation for its base URL and credential. Secrets
remain environment-backed `SecretStr` values and must never be written to logs,
errors, source records, or committed files.

## Incremental Watermark

The initial API run omits `changedSince` and imports a full snapshot. A
successful run stores each entity-and-session extraction-window upper timestamp
as that session's next exclusive `changedSince` watermark. Per-entity,
per-session watermarks are required because session IDs are not treated as
globally unique and sessions paged at different times have different snapshot
upper bounds. A single global maximum could skip updates or let one entity
overwrite another's state. Watermarks advance only after all pages and
downstream records complete successfully. A failed run retains the previous
values so retrying cannot omit data.

Session and chat cursors remain opaque and in-memory. Each entity has an
independent session pagination sequence, and each session has an independent
chat pagination sequence. HyperP forwards cursor values unchanged and never
derives tenant, session, time, or ordering information from them.

Equal timestamps are safe because page cursors include `chatId`, while the next
run's watermark is the completed snapshot upper bound rather than the last
individual record timestamp.

## Errors and Retry Behavior

WhatsAdmin returns the established structured error envelope for invalid
cursors, timestamps, authorization failures, missing sessions, and server
errors. HyperP treats malformed payloads as contract failures, includes bounded
request timeouts, and surfaces HTTP errors without exposing credentials.

The connector does not advance its watermark on partial pagination, validation
failure, extraction failure, or downstream ingestion failure.

Production enables the required entities explicitly. Startup configuration
validation fails when an enabled entity lacks its own key; it never substitutes
the other entity's key or the retired global credential.

## Migration from the global credential

Replace `WHATSADMIN_API_KEY` with organization-scoped configuration. First
identify whether the old handle key belongs to Eko or Speedzone. Move it only to
the matching variable and provision a different handle key for the other
organization:

```dotenv
WHATSADMIN_EKO_API_KEY=hk_replace_with_eko_handle_key
WHATSADMIN_SPEEDZONE_API_KEY=hk_replace_with_speedzone_handle_key
WHATSADMIN_EKO_ENABLED=true
WHATSADMIN_SPEEDZONE_ENABLED=true
# Set to the organization that owned the old WHATSADMIN_API_KEY.
WHATSADMIN_LEGACY_ENTITY=eko
```

Keep `WHATSADMIN_API_BASE_URL` when both handles use the same WhatsAdmin host.
Remove `WHATSADMIN_API_KEY` after the tenant values are installed; HyperP does
not read it or use it as a fallback. Never copy one organization's key into
both variables. The example values above are placeholders, not credentials.

`WHATSADMIN_LEGACY_ENTITY` is migration metadata, not a credential. Set it to
`eko` or `speedzone` according to the organization that owned the retired
global key, and keep it configured so that tenant continues using its existing
source-record identities. HyperP reads that tenant's legacy per-session
watermarks and writes new tenant-scoped watermarks after successful runs. The
other tenant always uses entity-scoped identities and state. If HyperP finds a
legacy watermark without this setting, extraction will fail closed rather than
replay data under new identities. Fresh installations must leave it unset.

### Staging host migration

Staging uses the host-managed `.docker/staging/docker-compose.yml`, so update
its worker and beat environment contract before rebuilding either service.
Remove the `WHATSADMIN_API_KEY` mapping and forward the tenant-era settings:

```yaml
WHATSADMIN_API_BASE_URL: ${WHATSADMIN_API_BASE_URL:-}
WHATSADMIN_EKO_API_KEY: ${WHATSADMIN_EKO_API_KEY:-}
WHATSADMIN_SPEEDZONE_API_KEY: ${WHATSADMIN_SPEEDZONE_API_KEY:-}
WHATSADMIN_EKO_ENABLED: ${WHATSADMIN_EKO_ENABLED:-false}
WHATSADMIN_SPEEDZONE_ENABLED: ${WHATSADMIN_SPEEDZONE_ENABLED:-false}
WHATSADMIN_LEGACY_ENTITY: ${WHATSADMIN_LEGACY_ENTITY:-}
WHATSADMIN_API_PAGE_SIZE: ${WHATSADMIN_API_PAGE_SIZE:-50}
WHATSADMIN_API_TIMEOUT_SECONDS: ${WHATSADMIN_API_TIMEOUT_SECONDS:-30.0}
```

The staging workflow validates these names and rejects the retired global
mapping before any image build. Actual `hk_...` values remain exclusively in
the host's secret-management environment and must not be added to Compose.

Run a single-entity extraction by passing `--entity-key eko` or
`--entity-key speedzone`. Omit `--entity-key` to resolve both credentials before
making the first request and extract both entities in one job.

## Validation

WhatsAdmin tests cover API-key authorization, organization isolation, ready
session filtering, participant resolution, message ordering, incremental
boundaries, stable pagination, snapshot isolation, and OpenAPI registration.

HyperP tests cover typed payload validation, opaque pagination, single-entity
and default combined jobs, Eko/Speedzone header isolation, multi-session key
reuse, unknown and missing credential failures, absence of cross-entity
fallback and secret leakage, organization mismatch rejection, bundle
conversion, connector selection, initial full import, entity-and-session
watermark isolation, safe retry behavior, and API error propagation. Existing
WhatsApp database connector tests remain green.
