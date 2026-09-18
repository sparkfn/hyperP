# WhatsAdmin API Ingestion Design

## Objective

Replace HyperP's direct WhatsAdmin PostgreSQL dependency in API mode with an
authenticated, cursor-paginated extraction contract. Preserve the existing
database connector for batch mode and support incremental ingestion through a
`changedSince` watermark. Keep Eko and Speedzone authentication and extraction
state strictly isolated.

The bounded adapter in this document adds a second, session-aware execution
path for `whatsapp_chat` that runs inside the durable bounded-ingestion runtime
(documented in the parent bounded-ingestion contract). It does not replace the
legacy whole-source iterator, and it stays activation-blocked until the
deployed WhatsAdmin contract proves the capabilities listed below.

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
- `snapshotAt`: the requested-as-of upper bound the page must honour;
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

## Bounded session-aware execution

The bounded adapter is a separate `whatsapp_chat` descriptor that runs one
durable unit at a time through the bounded runtime's admission, occurrence
deadline, reservation, fencing, receipt, and checkpoint contract. It never
wraps the legacy whole-source iterator and never self-schedules.

### Continuation state

One committed checkpoint preserves, in typed cursor subphases
`sessions -> chats -> extract -> commit -> chats/sessions -> terminal`:

- the entity key and a non-secret credential fingerprint;
- the immutable source window (contract version, stable window identity,
  completed lower bound, requested-as-of upper bound);
- the session-page position (opaque session cursor plus the remaining session
  queue of the fetched page);
- the current session, the stored chat page reference, and the chat position
  inside that page;
- the extraction/retry/commit subphase with its attempt count; and
- durable references to the stored chat page and the prepared extraction
  output.

The window is copied from the immutable run scope without any upstream I/O. The
first session or chat page read binds the returned snapshot; every later page
must echo the same snapshot, and a snapshot beyond the requested as-of bound is
refused rather than accepted. Eko and Speedzone state is isolated by entity key
and generation-scoped durable keys.

### Unit ordering

1. `sessions` — one bounded session page read; the returned sessions are queued
   with the page's continuation cursor.
2. `chats` — one bounded chat page read for the current session, stored durably
   under a content-addressed reference before any extraction.
3. `extract` — one chat version only. The chat's immutable content version is
   the hash of its rendered transcript and participant set, so message edits,
   deletions, and participant changes produce a new version while a pure
   timestamp change does not. An already committed version is skipped without
   an LLM call.
4. `commit` — the prepared envelopes are written through the pipeline's
   transaction-bound hook inside the fenced bounded transaction, the committed
   chat version is recorded, and the chat position advances atomically. The
   writer performs no source or LLM call.

Staging is a cache, never progress: only the commit transaction advances the
checkpoint that references a staged entry, so a lost or repeated staging write
can neither skip nor duplicate work. A graph failure leaves prepared output
durable, so retrying the graph commit performs zero new LLM calls.

### Bounded extraction retry

Extraction runs with an attempt-aware call control that reserves every provider
attempt — including transport retries, malformed-result retries, and summary
calls — before it starts, caps each request timeout at the remaining budget, and
bounds each backoff delay. A deadline or cancellation signal is never converted
into a per-chat failure.

The stable extraction replay identity is derived from entity, session, chat,
and immutable source version, excluding the attempt generation. When extraction
exhausts its bounded attempts the unit atomically persists a versioned retry
obligation, the checkpoint remains at `extract` with its attempt count advanced,
and the run pauses for the next eligible occurrence. Re-admission at the next
generation creates a new attempt-scoped receipt for the same replay identity, so
the chat is retried; a successful extraction resolves the logical-run obligation
and advances to `commit`. Same-attempt replay is not the retry mechanism.

### Terminal completion

Completed per-session watermarks publish only in the terminal unit, after the
whole session walk finished and every retry obligation resolved. The completed
watermark holds the window's requested-as-of bound, which becomes the next
window's exclusive lower bound for that session. A unit that cannot advance
publishes nothing: partial absence never implies deletion, and no unit reports
partial work as success.

### Bounded resource contract

The descriptor reserves one upstream request per unit, caps records per unit
(page bundles and extracted envelopes), caps bytes per unit, and caps extraction
calls per unit. Oversized pages, oversized extraction output, and extraction
output exceeding the envelope bound fail closed instead of being truncated.

Because bounded reservations are worst-case per unit and cumulative for the run,
activating this adapter also requires raising the run-level
`bounded_ingestion` caps (`max_records`, `max_pages`, `max_bytes`,
`max_extraction_calls`) in the ingestion config so a weekly window can complete
more than a handful of units. This is a configuration change, not a code change,
and it must be part of the activation review below.

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

## Capability contract and activation blockers

Synthetic fixtures define and exercise the required upstream contract; they are
not upstream capability evidence. The production descriptor reports a blocked
capability state — through the registry readiness check, so bounded dispatch
fails closed before any connector is constructed — until the deployed WhatsAdmin
contract proves every guarantee:

| Guarantee | Required behaviour |
|---|---|
| `resumable_session_enumeration` | Stable session enumeration that survives mutation and weekly pauses |
| `requested_as_of_snapshot_binding` | Requested-as-of or cross-session snapshot binding (or an approved durable per-session-bound equivalent) |
| `cursor_retention_window` | Cursor and snapshot retention of at least 30 days, or identity-preserving renewal |
| `explicit_expiry_and_drift_recovery` | Explicit expiry/drift responses with safe bounded recovery that preserves the last committed lower bound |
| `bounded_chat_message_continuation` | Bounded chat/message bytes and counts, or message continuation |
| `old_message_edit_delete_tombstones` | Effective old-message edits and deletions with replayable tombstones |
| `participant_change_tombstones` | Effective participant changes with replayable tombstones |
| `chat_session_removal_tombstones` | Effective chat/session removals with replayable tombstones |
| `removed_versus_unavailable_distinction` | A clear distinction between removed and temporarily unavailable/not-ready sessions |

No fabricated response fields, deletion by partial absence, replacement
snapshot, automatic rescan, or false success is acceptable. Activation also
requires the bounded configuration review described above.

### Synthetic contract fixtures

The contract is exercised by synthetic request/response fixtures that fix the
exact shapes the adapter relies on:

- session page: `{"success": true, "data": [session rows], "meta": {timestamp,
  requestId, snapshotAt, pagination{hasMore, nextCursor}}}` where every session
  row carries `id`, `orgId`, `orgName`, `whatsappUserId`,
  `expectedPhoneNumber`, `updatedAt`;
- chat page request: `{"sessionId", "changedSince"?, "snapshotAt"?, "cursor"?,
  "limit"}` — `snapshotAt` is omitted only on the first chat page of a window
  whose snapshot was not already bound by the session page;
- chat page response: the same envelope with denormalized bundles carrying
  `chatId`, `chatName`, `sessionId`, `whatsappUserId`, `changedAt`,
  `participants[]`, and `messages[]`; and
- error behaviour: any page that omits `nextCursor` while `hasMore` is true, that
  changes `snapshotAt` mid-window, or that returns a snapshot beyond the
  requested as-of bound is refused instead of being interpreted.

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
watermarks from durable application storage and writes new tenant-scoped
watermarks atomically with successful runs. Redis may retain only ephemeral
coordination and legacy-migration duties. The
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
WHATSADMIN_API_PAGE_SIZE: ${WHATSADMIN_API_PAGE_SIZE:-25}
WHATSADMIN_API_TIMEOUT_SECONDS: ${WHATSADMIN_API_TIMEOUT_SECONDS:-120.0}
WHATSADMIN_API_MAX_ATTEMPTS: ${WHATSADMIN_API_MAX_ATTEMPTS:-5}
WHATSADMIN_API_RETRY_BASE_DELAY_SECONDS: ${WHATSADMIN_API_RETRY_BASE_DELAY_SECONDS:-1.0}
```

The staging workflow validates these names and rejects the retired global
mapping before any image build. Actual `hk_...` values remain exclusively in
the host's secret-management environment and must not be added to Compose.

Run production extraction as independent jobs by passing `--entity-key eko` or
`--entity-key speedzone`. This keeps an upstream failure for one organization
from blocking or delaying the other. Omit `--entity-key` only for an intentional
combined maintenance run; HyperP resolves both credentials before making the
first request.

Chat extraction defaults to 25 records per page, a 120-second client timeout,
and five attempts with exponential backoff. Terminal failure checkpoints include
the effective page size, timeout, attempt limit, session, and cursor. Successful
pages store their next cursor, so a single-entity retry resumes from the latest
page checkpoint.

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

Bounded-adapter tests cover the descriptor readiness gate (unproven capability,
contract-version drift, unreadable readiness, and capability reporting through
dispatch), the typed cursor contract, content-version retry identity with legacy
timestamp fallback, transaction-bound checkpoint reads and writes (including
refused un-authorized watermarks and reported non-durable writes), attempt-aware
LLM call control (per-attempt reservation, per-request timeout, bounded backoff,
cancellation propagation, and best-effort summaries), the full
sessions/chats/extract/commit walk, durable retry and re-admission resolution,
unchanged-version skipping, graph-failure reuse of prepared output with zero new
LLM calls, zero-person processed outcomes, absence-not-deletion, snapshot drift
refusal, and the pipeline transaction hook.
