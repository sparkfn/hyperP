# WhatsAdmin API Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add tenant-safe incremental WhatsAdmin extraction endpoints and a HyperP API-mode connector that consumes them without direct database access.

**Architecture:** WhatsAdmin owns a dedicated integration module that queries ready sessions and emits denormalized conversation bundles through stable snapshot cursors. HyperP owns typed API models, an authenticated client, a Redis-backed successful-run watermark, and an API connector that reuses the existing transcript and envelope pipeline.

**Tech Stack:** Fastify 5, TypeScript, Zod, Drizzle ORM, Vitest, Python 3.12, Pydantic 2, httpx, Redis, pytest.

---

## Repository boundaries

- WhatsAdmin repository: `/var/home/Corbomode/Corbu/ADA/whatsadmin/whatsappWebJs_api`
- HyperP repository: `/var/home/Corbomode/Corbu/ADA/worktrees/hyperP/scenic-coral/hyperP`
- Do not stage, commit, push, or open a PR without explicit user approval.
- WhatsAdmin work belongs to issue #465 and branch `issue-465-hyperp-ingestion-api`.
- HyperP work belongs to branch `feat/whatsadmin-api-ingestion`.

### Task 1: Define the WhatsAdmin extraction contract

**Files:**
- Create: `src/modules/hyperp-extraction/hyperp-extraction.schemas.ts`
- Test: `src/modules/hyperp-extraction/hyperp-extraction.schemas.test.ts`

- [ ] **Step 1: Write failing schema tests**

Add tests proving that an omitted `changedSince` is accepted, a valid timezone-aware
ISO timestamp is accepted, a timestamp without an offset is rejected, `limit` is
bounded to 1–100, and malformed opaque cursors are rejected by the cursor decoder.

```ts
expect(ChatExtractionQuerySchema.parse({ sessionId: 'ses_1' }).changedSince).toBeUndefined();
expect(ChatExtractionQuerySchema.parse({
  sessionId: 'ses_1',
  changedSince: '2026-07-17T10:00:00.000Z',
}).changedSince).toBe('2026-07-17T10:00:00.000Z');
expect(() => ChatExtractionQuerySchema.parse({
  sessionId: 'ses_1',
  changedSince: '2026-07-17T10:00:00',
})).toThrow();
```

- [ ] **Step 2: Verify RED**

Run `pnpm vitest run src/modules/hyperp-extraction/hyperp-extraction.schemas.test.ts`.
Expect failure because the schema module does not exist.

- [ ] **Step 3: Implement the contract schemas**

Export strict Zod schemas and inferred types for:

```ts
export const SessionExtractionQuerySchema = z.object({
  limit: z.number().int().min(1).max(100).default(50),
  cursor: z.string().min(1).optional(),
}).strict();

export const ChatExtractionQuerySchema = z.object({
  sessionId: z.string().min(1),
  changedSince: z.string().datetime({ offset: true }).optional(),
  limit: z.number().int().min(1).max(100).default(50),
  cursor: z.string().min(1).optional(),
}).strict();
```

Define response schemas for session rows, participants, messages, bundles, and
pagination. Message fields must cover `fromId`, `toId`, `authorId`, `body`,
`timestamp`, and `fromMe`; participant fields must cover `jid`, `phone`, `name`,
and `role`; every chat bundle must include `changedAt`.

- [ ] **Step 4: Verify GREEN**

Run the focused Vitest command and expect all schema tests to pass.

### Task 2: Query ready sessions with tenant and handle isolation

**Files:**
- Create: `src/modules/hyperp-extraction/hyperp-extraction.service.ts`
- Test: `src/modules/hyperp-extraction/hyperp-extraction.service.test.ts`

- [ ] **Step 1: Write failing service tests**

Seed two organizations, ready and non-ready sessions, and two access handles.
Assert that `listReadySessions()` returns only ready sessions belonging to the
authenticated organization and, for API keys, only session IDs attached to the
authenticated handle. Assert stable `(updatedAt, id)` pagination.

- [ ] **Step 2: Verify RED**

Run `pnpm vitest run src/modules/hyperp-extraction/hyperp-extraction.service.test.ts`.
Expect an import failure for the missing service.

- [ ] **Step 3: Implement session extraction**

Add an `ExtractionScope` type:

```ts
export interface ExtractionScope {
  orgId: string;
  allowedSessionIds?: readonly string[];
}
```

Implement `listReadySessions(scope, page)` with parameterized Drizzle predicates:
`sessions.orgId = scope.orgId`, `sessions.status = 'ready'`, non-null
`whatsappUserId`, and `inArray(sessions.id, allowedSessionIds)` when the caller is
an API key. Join `orgs` to return organization identity. Fetch `limit + 1`, sort
by `updatedAt` then `id`, and emit an opaque cursor through existing cursor
helpers.

- [ ] **Step 4: Verify GREEN**

Run the focused service test and expect all cases to pass.

### Task 3: Build consistent incremental conversation pages

**Files:**
- Modify: `src/modules/hyperp-extraction/hyperp-extraction.service.ts`
- Modify: `src/modules/hyperp-extraction/hyperp-extraction.service.test.ts`

- [ ] **Step 1: Write failing incremental extraction tests**

Cover these cases independently:

- chat updated after `changedSince` is included;
- message created or updated after `changedSince` includes its parent chat;
- boundary equal to `changedSince` is excluded;
- messages are returned oldest-first and blank bodies are omitted;
- contacts resolve `jid`, `lidId`, and `cusId` participant identities;
- page ordering is `(changedAt, chatId)` and equal timestamps do not skip rows;
- an update after the first page's `snapshotAt` is deferred to the next run.

- [ ] **Step 2: Verify RED**

Run the focused service test. Expect failures because chat extraction is absent.

- [ ] **Step 3: Implement snapshot cursor parsing**

Use a typed cursor payload:

```ts
interface ChatCursor {
  snapshotAt: string;
  changedAt: string;
  chatId: string;
}
```

On the first page, set `snapshotAt` from the database clock. On later pages,
decode and validate all three fields. Reject a cursor whose embedded values are
invalid rather than silently restarting pagination.

- [ ] **Step 4: Implement bundle selection and assembly**

Calculate each candidate's `changedAt` as the greatest of the chat update time
and its messages' create/update times. Apply the exclusive lower bound and
inclusive snapshot upper bound before keyset pagination. Fetch participants and
messages in batches for the selected chat IDs, assemble them in memory, and
avoid one query per chat. Use existing ID resolution utilities for `@lid` and
`@c.us` identities.

- [ ] **Step 5: Verify GREEN**

Run the focused service test and expect all incremental, ordering, and snapshot
cases to pass.

### Task 4: Expose and document authenticated extraction routes

**Files:**
- Create: `src/modules/hyperp-extraction/hyperp-extraction.routes.ts`
- Create: `src/modules/hyperp-extraction/hyperp-extraction.routes.test.ts`
- Modify: `src/app.ts`
- Modify: `src/modules/auth/rbac.middleware.ts`
- Modify: `README.md`
- Modify: `package.json`

- [ ] **Step 1: Write failing route tests**

Build the app and assert both routes return 401 without credentials, reject an
API key scoped to another handle/session, return the standard success envelope,
and appear in generated OpenAPI. Assert normal user JWTs are rejected and the
handle must opt in with the non-default `hyperp:extract` permission.

- [ ] **Step 2: Verify RED**

Run the route test plus `tests/integration/openapi.test.ts`; expect missing-route
and missing-permission failures.

- [ ] **Step 3: Implement routes and permission**

Register `hyperpExtractionRoutes` at `/api/integrations/hyperp`. Apply
`apiKeyPreHandler`, reject callers without API-key context, and require
`hyperp:extract` in the handle permissions. Build the service scope from the
API-key organization and session IDs. Respond only through `sendPaginated()`
and existing structured errors.

- [ ] **Step 4: Update living documentation and version**

Document both endpoints, authentication, `changedSince`, snapshot semantics,
and cursor opacity in `README.md`. Apply the issue's `version:minor` decision by
incrementing the minor version in `package.json` without changing unrelated
dependencies.

- [ ] **Step 5: Verify GREEN**

Run focused tests, `pnpm build`, and the repository's errors-only lint command.
Expect zero TypeScript errors, test failures, and ESLint errors.

### Task 5: Add typed HyperP API models and client

**Files:**
- Create: `services/ingestion/src/connectors/whatsadmin_api/models.py`
- Create: `services/ingestion/src/connectors/whatsadmin_api/client.py`
- Create: `services/ingestion/src/connectors/whatsadmin_api/__init__.py`
- Create: `services/ingestion/tests/test_whatsadmin_api_client.py`

- [ ] **Step 1: Write failing client tests**

Use `httpx.MockTransport` to verify `X-API-Key`, session pagination, chat
pagination, forwarding of `changedSince`, preservation of the server-provided
cursor, strict Pydantic rejection of malformed bundles, and propagation of 401,
429, and 500 responses without logging the API key.

- [ ] **Step 2: Verify RED**

Run `uv run pytest services/ingestion/tests/test_whatsadmin_api_client.py -q`.
Expect import failure for the missing package.

- [ ] **Step 3: Implement strict models**

Create `BaseModel` types with `ConfigDict(extra="forbid")` for envelope metadata,
pagination, sessions, participants, messages, and chat bundles. Use
timezone-aware `datetime` fields and validate that server timestamps contain an
offset.

- [ ] **Step 4: Implement the client**

Add `WhatsAdminApiClient` with `iter_sessions()` and
`iter_chat_pages(session_id, changed_since)`. POST JSON bodies to the two
contract paths, apply a bounded timeout, call `raise_for_status()`, validate each
response, and close its injected or owned `httpx.Client` idempotently.

- [ ] **Step 5: Verify GREEN**

Run the focused pytest module and expect all client tests to pass.

### Task 6: Reuse the existing WhatsApp extraction pipeline in API mode

**Files:**
- Create: `services/ingestion/src/connectors/whatsadmin_api/connector.py`
- Modify: `services/ingestion/src/connectors/whatsapp/connector.py`
- Modify: `services/ingestion/src/connectors/__init__.py`
- Create: `services/ingestion/tests/test_whatsadmin_api_connector.py`
- Modify: `services/ingestion/tests/test_phppos_api_connectors.py`

- [ ] **Step 1: Write failing connector tests**

Inject a fake client and assert that API bundles produce the same formatted
transcript, participants, message endpoints, observed timestamp, tenant key,
source record IDs, and LLM/exclusion behavior as equivalent database bundles.
Assert `get_connector('whatsapp_chat', mode='api')` selects the API connector and
batch mode still selects `WhatsAppChatConnector`.

- [ ] **Step 2: Verify RED**

Run both focused connector test modules. Expect API mode selection to fail with
the existing “API mode” error.

- [ ] **Step 3: Extract shared pipeline helpers**

Make the existing participant and chat bundle dataclasses public within the
WhatsApp connector package or move them to a small shared module. Extract a
function that accepts an iterator of bundles and performs the existing LLM
batch, exclusions, and envelope building. Keep database fetching behavior
unchanged.

- [ ] **Step 4: Implement the API connector**

Map organization names through the existing `ORG_TO_ENTITY` precision allowlist.
Skip unknown organizations with an informational log. Convert each typed API
bundle into the shared bundle type and pass the complete iterator into the
shared extraction function.

- [ ] **Step 5: Verify GREEN**

Run both connector modules and the existing
`services/ingestion/tests/test_whatsapp_connector.py`; expect all tests to pass.

### Task 7: Persist watermarks only after successful runs

**Files:**
- Create: `services/ingestion/src/connectors/whatsadmin_api/watermark.py`
- Modify: `services/ingestion/src/connectors/whatsadmin_api/connector.py`
- Modify: `services/ingestion/src/config.py`
- Modify: `services/ingestion/tests/test_whatsadmin_api_connector.py`
- Create: `services/ingestion/tests/test_whatsadmin_watermark.py`
- Modify: `config/ingestion-config.example.json`

- [ ] **Step 1: Write failing watermark tests**

Use a fake Redis implementation to prove the first run returns no watermark,
successful completion stores the maximum server `snapshotAt`, partial page
failure leaves the old value untouched, and reruns reuse the old exclusive lower
bound.

- [ ] **Step 2: Verify RED**

Run both watermark-focused pytest modules. Expect import and behavior failures.

- [ ] **Step 3: Implement watermark storage**

Define a `WatermarkStore` protocol with `get()`, `set(datetime)`, and `close()`.
Implement Redis storage under
`profile_unifier:whatsadmin-api:whatsapp_chat:watermark`. Parse stored values as
timezone-aware ISO timestamps and fail closed on malformed state.

- [ ] **Step 4: Add API configuration**

Add `whatsadmin_api_base_url`, `whatsadmin_api_key: SecretStr`,
`whatsadmin_api_page_size`, and `whatsadmin_api_timeout_seconds`. Do not reuse
the outbound birthday-message API settings because they represent a different
service contract and credential lifecycle.

- [ ] **Step 5: Couple commit to successful iterator completion**

The connector reads the watermark before requesting pages, tracks every page's
common `snapshotAt`, and calls `set()` only after all bundles have been consumed
and transformed without an exception. Close the API client and watermark store
in `finally` blocks.

- [ ] **Step 6: Verify GREEN**

Run the watermark and connector tests. Expect successful-run advancement and all
failure-path non-advancement assertions to pass.

### Task 8: Cross-repository verification and hostile review

**Files:**
- Review all files changed by Tasks 1–7.

- [ ] **Step 1: Run WhatsAdmin validation**

Run focused tests, full `pnpm test`, `pnpm build`, and lint. Record commands and
results; do not weaken checks or suppress warnings.

- [ ] **Step 2: Run HyperP validation**

Run focused pytest modules, the full ingestion test suite, Ruff, strict mypy,
`git diff --check`, and any repository-prescribed frontend/API checks affected by
configuration documentation.

- [ ] **Step 3: Perform hostile review**

Inspect tenant escape paths, cursor tampering, timestamp precision, simultaneous
updates, empty pages, duplicate delivery, watermark advancement timing, secret
redaction, response-contract drift, N+1 queries, module size, and compatibility
with database mode. Add a failing regression test before fixing any issue found.

- [ ] **Step 4: Inspect worktree state**

Run `git status -sb` and `git diff` independently in both repositories. Confirm
only task-related files changed and no generated artifacts, processes, or
containers remain.

- [ ] **Step 5: Stop before Git mutations**

Report validation evidence and changed files. Request explicit approval before
staging, committing, pushing, opening a PR, or invoking Woodpecker for a pushed
branch.
