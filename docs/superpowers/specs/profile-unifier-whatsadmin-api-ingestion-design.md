# WhatsAdmin API Ingestion Design

## Objective

Replace HyperP's direct WhatsAdmin PostgreSQL dependency in API mode with an
authenticated, cursor-paginated extraction contract. Preserve the existing
database connector for batch mode and support incremental ingestion through a
`changedSince` watermark.

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

The connector enumerates ready sessions, pages through their changed chat
bundles, and converts them into the existing internal chat-bundle form. The
existing transcript formatting, LLM batch extraction, exclusions, tenant
mapping, and source-envelope builders then run unchanged.

Configuration supplies the WhatsAdmin base URL, API key, request timeout, page
size, and optional starting watermark. Secrets remain environment-backed and
must never be written to logs or source records.

## Incremental Watermark

The initial API run omits `changedSince` and imports a full snapshot. A
successful run stores each session's extraction-window upper timestamp as that
session's next exclusive `changedSince` watermark. Per-session watermarks are
required because sessions paged at different times have different snapshot
upper bounds; a single global maximum could skip updates. Watermarks advance
only after all pages and downstream records complete successfully. A failed run
retains the previous values so retrying cannot omit data.

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

## Validation

WhatsAdmin tests cover API-key authorization, organization isolation, ready
session filtering, participant resolution, message ordering, incremental
boundaries, stable pagination, snapshot isolation, and OpenAPI registration.

HyperP tests cover typed payload validation, pagination, session-to-tenant
mapping, bundle conversion, connector selection, initial full import,
incremental watermarks, safe retry behavior, and API error propagation.
Existing WhatsApp database connector tests remain green.
