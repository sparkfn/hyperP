# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

HyperP is a customer profile unification and relationship intelligence platform. It resolves the same person across systems (POS, Bitrix CRM, third-party apps) for use cases like contact tracing and sales. Code lives in `services/api/` and `services/ingestion/`; design docs live in `docs/`.

## Development Commands

### Shell
Use Bash as the default shell/terminal for Claude Code commands in this project.

### Docker (primary workflow)
```bash
docker compose up -d                                        # start all services
docker compose build --no-cache api frontend2              # rebuild images (always use --no-cache for code changes)
docker compose up -d api frontend2 web                     # restart after rebuild
docker compose logs -f api                                 # stream logs from a service
docker compose stop                                        # stop containers while preserving them for log inspection
docker compose down                                        # remove containers and network only when explicitly requested
```
**Single active frontend:** `frontend2` is the active app served at the web root; all UI work happens in `services/frontend2`. The legacy v1 app under `services/frontend/` is retired (no longer built or routed); its source is kept for reference only.
If the user says "stop containers", run `docker compose stop`, not `docker compose down`. Only remove containers when the user explicitly says to remove containers.
Always pass `--no-cache` when rebuilding after Python or TypeScript changes — Docker layer caching can serve stale source even when files change.

### Python — linting and type-checking
Run from the repo root. The `uv` workspace resolves both services from the single root `uv.lock`.
```bash
# API service
uv run --package profile-unifier-api ruff check services/api/src
uv run --package profile-unifier-api ruff format services/api/src
uv run --package profile-unifier-api mypy --strict services/api/src

# Ingestion service
uv run --package profile-unifier-ingestion ruff check services/ingestion/src
uv run --package profile-unifier-ingestion ruff format services/ingestion/src
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src
```

### Python — tests
Run per-package — the root workspace venv (`uv sync`) only carries lint/type tools
(ruff/mypy/pytest), not runtime deps, so `uv run pytest` at the root cannot import
`src.celery_app`/fastapi/neo4j and fails at collection. Each service's own venv has
its runtime + dev deps.
```bash
uv run --package profile-unifier-api pytest services/api/tests        # API tests
uv run --package profile-unifier-ingestion pytest services/ingestion/tests  # ingestion tests
uv run --package profile-unifier-api pytest services/api/tests/test_foo.py  # single file
```
Test paths are configured in the root `pyproject.toml` (`testpaths` lists both
services), so a path may be passed to scope a run; the `--package` flag selects
which venv runs it.

### Frontend
`services/frontend2` is the active app (web root). The legacy `services/frontend/` (v1) is retired and no longer built or routed; do not run its commands.
```bash
cd services/frontend2
npm install          # already done in Docker; run locally for typecheck/lint only
npm run dev         # dev server (frontend2 :3001)
npm run typecheck   # tsc --noEmit
npm run lint       # eslint src (ESLint 9 flat config, max-warnings 9)
npm run build      # production build (runs in Docker for deployment)
```
**Note:** `next lint` was removed in Next.js 15 and replaced with direct ESLint. If `npm run lint` fails, check that `eslint` and `eslint-config-next` are in `devDependencies` and that `eslint.config.mjs` exists.
Both frontend Dockerfiles use `npm install --legacy-peer-deps` because `@mui/x-date-pickers@7` has a peer dependency range that conflicts with `@mui/material@6`. Do not remove this flag.
**ESLint warning budget**: `--max-warnings 9` is enforced, but `frontend2`'s `npm run lint` **currently exceeds it on a clean tree** (~18 pre-existing `react-hooks/set-state-in-effect` warnings from the React-Compiler rule, 0 errors) — independent of your change. Verify your change adds **zero net warnings** (stash and compare counts), not a green exit. Don't add `eslint-disable-next-line react-hooks/set-state-in-effect` for a `useEffect` that only calls a callback prop — it doesn't trip the rule (see `SalesTab`).

---

## Service Topology

**Intelligence runtime:** one non-root, CLI-only `intelligence` container has no port or service
dependencies. It persists only via `intelligence-data` at `/var/lib/intelligence`, starts idle, and
keeps mutation execution disabled by default. Use fixed CLI controls only; do not add HTTP, a
scheduler, a sidecar, or arbitrary command execution.

Eight Docker containers defined in `docker-compose.yml`:

| Service | Image / Build | Internal address | Notes |
|---|---|---|---|
| `neo4j` | `neo4j:5.26-community` | `bolt://neo4j:7687` | HTTP browser at `:7474`; 5.11+ required for vector index support |
| `redis` | `redis:7-alpine` | `redis://redis:6379` | Celery broker (db 0) + results (db 1) + token revocation store + public share-link tokens (TTL auto-cleanup) |
| `api` | `services/api/Dockerfile` | `http://api:3000` | FastAPI/uvicorn; not exposed directly |
| `frontend2` | `services/frontend2/Dockerfile` | `http://frontend2:3001` | Next.js **v2** (active app); served at the web root; not exposed directly |
| `web` | `nginx:1.27-alpine` | exposed on `:80` | Reverse proxy. Longest-prefix routing: `/api/app/*` and `/api/oauth2/*` → FastAPI mounts (path preserved, no strip — mounts need the `/api` prefix kept); `/api/*` → FastAPI (strips `/api`, root_path `/api`); `/` (catch-all) → `frontend2` |
| `ingestion-worker` | `services/ingestion/Dockerfile` | — | Celery worker restricted to the `ingestion` queue; concurrency fixed at 2 in code |
| `lifecycle-worker` | `services/ingestion/Dockerfile` | — | Celery worker for `lifecycle` and `miscellaneous`; concurrency fixed at 2 in code |
| `beat` | `services/ingestion/Dockerfile` | — | Celery beat scheduler; fixed weekly ingestion groups run at 01:00 UTC |

**FastAPI mounts & root surface:** the root app (`src/app.py`) registers only cross-cutting and unauthenticated routes — `GET /api/health`, the machine OAuth2 token flow (`/api/v1/oauth/{token,jwks}`), and the public share-link pages (`/api/v1/public/...`). Every **authenticated business route is mount-only** — the root app no longer serves `/api/v1/persons`, `/api/v1/entities`, etc. Two sub-apps built from the same `src/routes/*` routers carry the authenticated contract: `/app/v2` (the active frontend2 UI contract, `/v1` stripped — `frontend_app.py`) and `/oauth2/v1` (machine OAuth2 — token/jwks + read-only persons list/detail, client-credentials only, `/v1/oauth` → `/oauth2/v1/{token,jwks}` — `oauth2_app.py`). The legacy `/app/v1` frontend contract has been retired along with the v1 frontend. `src/router_copy.py` centralizes the route-copying (strip-`/v1` by default, with optional path filter/transform). To add or remove an endpoint from a mount, change the router membership in the relevant builder, not `app.py`.

**Frontend ↔ API wiring:** frontend2's BFF calls `buildApiUrl` (`src/lib/api-url.ts`), prefixing the FastAPI mount for its contract (`/app/v2`) onto `API_BASE_URL` (`…/api`) → `/api/app/v2/...`. This is **independent** of the UI's web base path: `frontend2` serves at the web root (`NEXT_PUBLIC_BASE_PATH=""`) while still calling `/app/v2`. Public (unauthenticated) endpoints route to `/api/v1/public/...` instead. `NEXT_PUBLIC_BASE_PATH` (`src/lib/route-paths.ts`) is the single knob for the UI base path, driving `next.config.ts` `basePath`, NextAuth, and middleware; nginx `location` blocks and FastAPI mounts are kept in sync separately.

**MCP contract:** The authenticated Streamable HTTP MCP endpoint is `/mcp`, mounted in the API process and exposed by nginx without an `/api` prefix. `src.mcp_app` generates tools from the route catalog: every operation on `FRONTEND_ROUTERS` plus the schema-visible `ROOT_ROUTERS` is included exactly once, while OAuth2's duplicated machine mount does not create duplicate tools. The MCP transport requires `require_active_user`, forwards the Authorization header into tool calls, and retains each endpoint's own role and scope dependencies. When adding an endpoint, use a stable unique operation ID and update the API-to-MCP parity test; an exclusion must be explicit and documented for security or transport reasons.

**Startup:** `logging.basicConfig(level=...)` in `src/app.py` also silences the `neo4j.notifications` logger (Cypher deprecation warnings) so they don't flood the API container logs. Real Neo4j errors at ERROR level are unaffected.

Auth flow: browser → next-auth (Google OAuth) → `googleIdToken` in JWT session → Next.js BFF attaches `Authorization: Bearer` → FastAPI verifies via `require_active_user`. Revocation is Redis-backed: `POST /v1/auth/logout` adds the token's `jti` to a Redis SET (TTL auto-cleanup) and evicts the in-process user cache immediately; Google refresh tokens are revoked via Google's revocation endpoint.

The Google provider requests `access_type=offline` + `prompt=consent` (refresh tokens issued only on consent; tokens live solely in the JWT cookie). The jwt callback silently renews the Google ID token ~60 s before its 1-hour expiry — a Google 4xx (`invalid_grant`) sets `session.error = "RefreshTokenError"` and auto-redirects to `/login`, while network errors/5xx keep the session and retry. The session cookie (`hyperP_refresh`) has a **rolling idle window** (`AUTH_SESSION_MAX_AGE_SECONDS`; compose default 18000 = 5 h, code fallback 1 h) — an inactivity timeout, not an absolute cap.

**Auth bypass**: routes under `/public/**` and `/login`, `/api/health`, and `/bff/auth/**` are explicitly allowed through the NextAuth middleware without a session. The public person pages (`/public/persons/[token]`) are served entirely unauthenticated — they call `apiFetch` with `authToken: null` so no Bearer header is sent.

---

## Recurring Code Patterns

### Repository layer (database abstraction)

All database access goes through a repository layer at `services/api/src/repositories/`. Routes **never** call `get_session()` or import from `src.graph.*` directly — only the Neo4j implementations do.

```
repositories/
  protocols/    # typing.Protocol definitions — the contracts
  neo4j/        # Neo4j implementations of each protocol
  deps.py       # FastAPI Depends() wiring — one singleton per domain
```

**Route pattern** — inject via `Depends`, depend only on the Protocol type:
```python
from src.repositories.deps import get_person_repo
from src.repositories.protocols.person import PersonRepository

@router.get("/{person_id}")
async def get_person(
    person_id: str,
    request: Request,
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[Person]:
    person = await repo.get_by_id(person_id)
    if person is None:
        raise http_error(404, "person_not_found", "Person not found.", request)
    return envelope(person, request)
```

**To swap the database backend**: write a new implementation class (e.g. `repositories/postgres/`) and change the singleton in `deps.py` — route code stays untouched.

**Protocol method naming**: avoid naming Protocol methods `list` — it shadows the `list` builtin in the class body, causing mypy `valid-type` errors. Use `get_page` (paginated) or `get_all` (non-paginated) instead.

**Protocol types**: protocols use `dataclass`/`TypedDict` for domain types, not Pydantic `BaseModel` (`BaseModel.__init__` uses `Any` internally, failing `mypy --strict` outside the `src.routes.*` override list). Key domain types:
- `protocols/person.py` — `PersonListFilters` (TypedDict)
- `protocols/merge.py` — `MergeOutcome` (dataclass)
- `protocols/review.py` — `ReviewListFilters`, `AssignResult`, `ActionResult` (TypedDict)
- `protocols/admin.py` — `SourceSystemInfo`, `FieldTrustResponse` (dataclass)
- `protocols/ingest.py` — `IngestRecordsResponse`, `IngestRunResponse`, `IngestRunDetailResponse`, `IngestRecordResult` (dataclass)

**Pagination return conventions** (all implementations):
- With a count query: `tuple[list[T], int]` (items, total) — route computes `has_more = skip + limit < total`.
- Without a count query: `tuple[list[T], bool]` (items, has_more) — the implementation fetches `limit + 1` internally.

### Response envelope
All FastAPI endpoints return `ApiResponse[T]`. Use `envelope()` from `src/http_utils.py`:
```python
return envelope(data, request, cursor=next_cur, total_count=count)
```
`ResponseMeta` carries `request_id`, `next_cursor`, and `total_count`. Frontend reads these via `bffFetchEnvelope` in `src/lib/api-client.ts`.

**Exception — bare responses**: admin management endpoints (e.g. `GET /v1/admin/oauth-clients`) intentionally return bare `list[T]` or bare objects without `envelope()` for machine-to-machine callers. `apiFetch` in `api-server.ts` handles all three non-envelope shapes automatically:
- `null` body (HTTP 204 No Content) → `{ data: null, meta: ... }`
- bare array → `{ data: [...], meta: ... }`
- bare object with no `"data"` key → `{ data: {...}, meta: ... }`

### Cursor-based pagination (backend)
`page_window(cursor, raw_limit)` in `src/http_utils.py` decodes the base64 cursor to a skip offset. Pattern for every list endpoint:
```python
skip, page_limit = page_window(cursor, limit)
items, total = await repo.get_page(filters, skip, page_limit)   # count-based
has_more = skip + page_limit < total
return envelope(items, request, next_cursor(skip, page_limit, has_more), total_count=total)

# OR for non-count methods (search, matches):
items, has_more = await repo.get_page(q, status, skip, page_limit)
return envelope(items, request, next_cursor(skip, page_limit, has_more))
```

### Cursor-based pagination (frontend)
Use `usePaginatedFetch<T>(basePath)` from `src/lib/usePaginatedFetch.ts`. It manages `cursor`/`prevStack`/`nextCursor` state and exposes `{ rows, loading, error, from, to, total, hasPrev, hasNext, goNext, goPrev }`. BFF route handlers must forward `limit`/`cursor` from `searchParams` using `searchParamsToQuery(searchParams)` from `src/lib/proxy.ts`.

### BFF proxy
Every browser→API call goes through a Next.js route handler under `src/app/bff/`. Standard thin handler:
```typescript
export async function GET(request: Request, context: RouteContext): Promise<NextResponse> {
  const { personId } = await context.params;
  const { searchParams } = new URL(request.url);
  return proxyToApi<SomeType[]>(
    `/persons/${encodeURIComponent(personId)}/endpoint`,
    { query: searchParamsToQuery(searchParams) },
  );
}
```
Key BFF route groups: `bff/persons/` (list, search, profile sub-resources), `bff/review-cases/` (list, actions, assign), `bff/entities/`, `bff/ingest/`, `bff/admin/oauth-clients/`, `bff/auth/`, `bff/reports/`, `bff/dumps/`.

### Graph query modules
All Cypher strings live as module-level string constants **or dynamic builder functions** in `services/api/src/graph/queries/` and `services/ingestion/src/graph/queries/` (the `E501` line-length rule is disabled here so queries aren't artificially wrapped). Neither routes nor repository implementations embed Cypher inline — they import query constants and builders by name from `src.graph.queries`.

Dynamic builders (e.g. `build_list_persons_query`, `build_review_action_cypher`) live alongside the constants and are exported via `__init__.py`. Use a builder only when the query structure itself varies by input; parameterised values always go through Cypher parameters, not string interpolation.

### Mappers vs converters
- `graph/converters.py`: primitive type coercions (`to_str`, `to_int`, `to_float`, `to_optional_*`, `to_iso_or_none`, `to_datetime`, `to_str_list`, `encode_cursor`/`decode_cursor`). Also exports type aliases `GraphScalar`, `GraphValue`, `GraphRecord` — use these instead of `Any` when typing raw Neo4j records. Used by mappers.
- `graph/mappers*.py`: Neo4j `Record` → Pydantic model. One mapper file per domain (persons, entities, sales, reports).
- `repositories/neo4j/_utils.py`: shared `record_to_dict` and `to_total` helpers used by all Neo4j implementations.

**Neo4j type gotchas** (apply to all mapper code):
- **Booleans**: Cypher map projections return Python `bool`, not a string. Use `bool(record.get("field", False))` directly — never `to_str(record.get("field", "false")) == "true"`, which produces `"False"` / `"True"` and the comparison always fails.
- **Datetimes**: `datetime()` in Cypher stores with timezone and `.to_native()` may return a timezone-aware Python `datetime`. When doing arithmetic against a naive `now`, strip timezone first: `expires_at.replace(tzinfo=None) if expires_at.tzinfo else expires_at`. The reference pattern is `is_secret_usable` in `auth/oauth_clients.py`.

### API-side display formatting & v2 presentation models
The API formats human-facing strings (dates, percentages) so the frontend renders them verbatim — no client-side locale/number formatting. Helpers live in `src/display_format.py` (`format_display_date` → `"02 Apr 2026"`, `format_display_datetime` → `"02 Apr 2026, 03:14 AM"`, `format_confidence_pct` → `"82%"`; all UTC, return `""`/`None` on empty/invalid input). Reuse these rather than reformatting in TS.

When an endpoint needs presentation fields that the shared/public domain model must not carry, add a **v2 presentation model** that subclasses the domain model and is returned only by the authenticated route. Reference: `SourceRecordView(SourceRecord)` in `types.py` adds `*_display` fields (and a parsed `chat_transcript`) — the authenticated `GET /persons/{id}/source-records` returns `SourceRecordView` while the public `/persons/{token}/source-records` keeps `SourceRecord`. Map domain → view in the route (`_to_source_record_view` in `routes/persons.py`) via `**item.model_dump()` plus computed fields (relies on `src.routes.*` being in the mypy strict override list).

Conversation source records (`bitrix_chat`, `whatsapp_chat`) store the chat as a single transcript string under `raw_payload.conversation_text`/`messages_text`. `src/chat_transcript.py:parse_chat_transcript` turns it into a typed `list[ChatMessage]` (splits `[timestamp] Speaker (+phone): text` lines, joins continuations, strips BBCode, maps speaker→role via `chat_members`); returns `None` for non-chat payloads. The frontend renders bubbles from this — it doesn't parse transcripts itself.

### JWT / Google ID token verification
`services/api/src/auth/verify.py` uses a **self-contained** RS256 verifier with a 300-second clock-skew tolerance (drift vs. Google's token servers) — not `google-auth`'s `verify_oauth2_token`, whose strict `nbf` check causes spurious 401s. Signature is checked against Google's public cert endpoint.
Revocation (`auth/revoke.py`): the raw JWT is decoded (no verification) for `jti`/`exp`, checked against Redis before signature verification. The in-process user cache (`auth/deps.py:_USER_CACHE`) is keyed by `jti` and evicted immediately on `POST /v1/auth/logout`.

### Server-to-server OAuth2 client credentials
Machine callers use OAuth2 client credentials, not API keys (humans still use Google ID tokens). `POST /v1/oauth/token` with `grant_type=client_credentials`, `client_id`, `client_secret`, optional `scope` returns a short-lived RS256 JWT for `Authorization: Bearer`.

OAuth clients are stored as `(:OAuthClient)` nodes with `(:OAuthClientSecret)` child nodes. Plain client secrets are returned once only, on creation or rotation; stored secrets are HMAC-SHA256 hashes using `OAUTH_SECRET_HASH_KEY`. Each client has **at most one active secret** — rotation revokes the previous one (`active_secret` in `auth/oauth_clients.py`). Issued tokens are tracked in a Redis registry (last-used IP, manual revoke) with a per-client TTL. Token signing uses `OAUTH_PRIVATE_KEY_PEM`/`OAUTH_PUBLIC_KEY_PEM`, published at `GET /v1/oauth/jwks`.

`auth/deps.py` defines `OAuthClientUser(AuthUser)` and `get_current_user_or_oauth_client()` — HyperP OAuth JWTs are verified first, falling back to Google ID-token verification. OAuth JWT validation checks signature, issuer, audience, expiry, `jti` revocation, client existence/disabled state, current scopes, and `entity_key`, so old tokens can't retain removed privileges.

Machine access is explicit: machine routes use `require_scope("persons:read"/"persons:write"/"ingest:write")` (`admin` is a superset); human-only routes use `require_human_user`/`require_human_admin`. `/v1/admin/oauth-clients` (create/rotate/revoke/disable/delete) is human-admin only — even admin-scoped machine clients can't call it.

### Public (unauthenticated) API endpoints
Public (no-Bearer) endpoints register on a separate router included in `app.py` **without** the `active` dependency list. The auth-gated action that produces the public resource (e.g. generating a share link) uses the normal `person_links_router` with `require_active_user`. Example from `src/routes/public_pages.py`:
```python
public_router = APIRouter(prefix="/v1/public")      # no auth — included bare
person_links_router = APIRouter(prefix="/v1/persons")  # included with active deps

# In app.py:
app.include_router(public_router)                          # no auth
app.include_router(person_links_router, dependencies=active)
```
Public share-link tokens are UUID strings in Redis with a TTL (`public_link:{token}` → `person_id`), expiry set by `PUBLIC_PAGE_EXPIRY_MINUTES` (default 30, `config.py` + `docker-compose.yml`).

On the frontend, a Server Component page under `/public/**` fetches directly from the API with `authToken: null`:
```typescript
const res = await apiFetch<Person>(`/public/persons/${token}`, { authToken: null });
```
`authToken: null` skips `auth()` in `apiFetch` and sends no Authorization header. Interactive sections (e.g. expandable sales rows) must be extracted to a separate `"use client"` file since the page is a Server Component.

### Ingestion dispatch
Always dispatch via Celery — never call `run_ingestion()` directly:
```python
# Live source (pulls from configured SSH gateway / external DB):
run_ingestion_task.delay(source_key, mode="batch")

# File-based (dump): dump_path MUST be relative to DUMPS_ROOT
# (/app/dumps inside the ingestion-worker container)
run_ingestion_task.delay(source_key, mode="dump", dump_path="limited-100/fundbox_users_100.sql")
```
The task enforces a fixed Redis-backed cluster-wide concurrency cap of 2 and
retries automatically if a slot is busy. Celery worker concurrency is also
fixed at 2 in `src/celery_app.py`; it is not environment-configurable.

**⚠️ `limited-100` dumps are for local and development environments only — never use them in staging or production.**

**Source keys** (from `src/connectors/dumps/connectors.py` factories dict and live connectors):
| source_key | mode | limited-100 dump file (local/dev only) |
|---|---|---|
| `fundbox` | dump | `limited-100/fundbox_users_100.sql` |
| `fundbox:legacy` | dump | `limited-100/fundbox_legacy_100.sql` |
| `fundbox:merged` | dump | `limited-100/fundbox_merged_100.sql` |
| `fundbox:contacts` | dump | `limited-100/fundbox_contacts_100.sql` |
| `fundbox:sales` | dump | `limited-100/fundbox_sales_100.sql` |
| `eko_phppos` | dump | `limited-100/eko_customers_100.sql` |
| `eko_phppos:sales` | dump | `limited-100/eko_sales_100.sql` |
| `speedzone_phppos` | dump | `limited-100/speedzone_customers_100.sql` |
| `speedzone_phppos:sales` | dump | `limited-100/speedzone_sales_100.sql` |
| `bitrix_chat` | dump | `limited-100/bitrix_chat_100.sql` (also supports `api` and `backfill`) |
| `whatsapp_chat` | dump | `limited-100/whatsapp_chat_100.sql` |
| `sgbankruptcy` | dump | `limited-100/sgbankruptcy_100.sql` |
| `sgrentalflats` | dump | `limited-100/sgrentalflats_100.sql` |
| `onediver` | dump | `limited-100/onediver_100.sql` |
| `onediver:sales` | dump | `limited-100/onediver_sales_100.sql` |

`eko_phppos`, `bitrix_chat`, and `whatsapp_chat` need an SSH gateway in `batch` mode — without one, use `mode='dump'` with `dump_path`. `bitrix_chat` additionally supports incremental `api` and manual `backfill` modes through the Bitrix Open Lines REST client.

Weekly API-ingestion chain dispatch is disabled by default. Set
`scheduled_ingestion.enabled` to `true` in the mounted ingestion-config JSON to allow the
scheduled dispatcher task to publish its chains. The dispatcher reads this switch at task
start, before it claims an idempotency marker or publishes any ingestion task.

`sgbankruptcy` and `sgrentalflats` are **dump-only by design** — they have no live/batch connector (they exist only in the dump connectors factory), so dispatching them with `mode='batch'` raises `KeyError`/`ValueError` and the Celery task Rejects immediately. Always dispatch them with `mode='dump'` and a `dump_path`.

### Date picker fields
Date range filters use `DatePickerField` (wraps `@mui/x-date-pickers@7` `DatePicker` + `dayjs`, `en-gb` locale). Display format `DD MMM YYYY` matches `formatDob`/`formatDate` from `display.ts`; values are stored internally as ISO `YYYY-MM-DD`. Use it for any date input matching table-row date format — don't fall back to `<TextField type="date">`.

---

## Repository Structure

All documents live in `docs/` and follow the naming convention `profile-unifier-*.md` (plus one `.yaml`). Recommended reading order: PRD → Glossary → Architecture → Matching Spec → Policy Decisions → Graph Schema → Graph Model Diagram → API Spec → OpenAPI 3.1 → Reviewer Workflow → Sequence Diagrams → Roadmap → Scaffold Architecture.

## Key Design Decisions

- **Database**: Neo4j (decided) — chosen for native graph storage and multi-hop relationship queries.
- **Database abstraction**: a `repositories/` layer with `typing.Protocol` contracts and Neo4j implementations; swap backends by changing `deps.py`.
- **Precision over recall**: optimize for low false-merge rates.
- **Immutable source facts**: source records are never modified after ingestion; all changes create new records.
- **Explainable decisions**: every merge/no-match has traceable reasons.
- **4-layer matching**: deterministic rules → heuristic scoring → LLM adjudication (shadow-only in MVP) → human review.
- **Confidence bands**: ≥0.90 auto-merge, 0.60–0.89 human review, <0.60 no-match (thresholds to be calibrated).
- **Sensitive data**: NRIC and Singpass-linked data require special handling; govt IDs stored as salted hashes.
- **Controlled rollout**: LLM starts in shadow/assist mode only — no autonomous production merges in MVP.
- **Merge lineage**: merge history stored as `MERGED_INTO` relationships; path compression keeps canonical lookups to ≤1 hop.
- **Multi-match resolution (link-to-all, no person merge)**: the engine evaluates every candidate. If a record reaches the merge band against multiple active persons, pick a primary (highest confidence, ties by `person_id`) and link evidence to the primary **and every other matched person**, recomputing each golden profile without merging them. Carried on `MatchResult.additional_linked_person_ids`.
- **Unmerge**: post-merge source records stay with the surviving person but are flagged for review.
- **Concurrency**: ingestion partitioned by blocking key to avoid race conditions.
- **Cardinality caps**: blocking keys with too many matches are skipped, per identifier type.
- **Pair ordering**: `NO_MATCH_LOCK` relationships enforce `left.person_id < right.person_id` to prevent duplicates.
- **Golden profile recomputation**: synchronous within the merge transaction (Neo4j ACID).
- **Downstream events**: polling endpoint (`GET /v1/events?since=`) for now.
- **Identifier aging**: time-based deactivation via `last_confirmed_at`, per-type configurable.
- **Retention**: `retention_expires_at` on relevant nodes; NULL for legal holds.
- **Scoring model**: conditional weighting/capping, not simple additive weights.
- **Graph-native candidate generation**: candidates found by traversing shared Identifier nodes; composite blocking (DOB + name) falls back to index queries.
- **Source record types**: every `SourceRecord` has a `record_type` (`identity`, `bankruptcy`, `relationship`, `rental_flat`, `conversation`, `sales`). `rental_flat` is routed via `ingest_address_record` and never creates a Person. Conversation records are never deterministic auto-merge; they promote to auto-merge only with an independent, non-conversation-corroborated identifier match plus a second signal and no hard conflict. `bankruptcy` and `identity` share the same deterministic exact-NRIC hard-merge (name is never consulted for the government-ID check). `relationship` auto-merges on phone + partial name via Layer-2 promotion. A startup migration reclassifies legacy `system`/`public_record` records by `source_system`.
- **Social Person-to-Person relationship** (`KNOWS`): directed `(:Person)-[:KNOWS]->(:Person)` edges mirror the Fundbox `contacts` table; sourced, never inferred, and does not affect identity resolution.
- **Interaction model** (post-MVP): Interaction nodes for contact tracing will connect to Person nodes.
- **Data deletion**: graph deletion requires detaching all relationships before removing a Person node. Shared Identifier nodes survive individual person deletion.

## Architecture Summary

Ingestion → Normalization → Candidate Generation (graph traversal through shared Identifier and Address nodes) → Match Engine → Person Graph (Neo4j) → Golden Profile → Review Operations → APIs.

Core graph nodes: `Person` (golden profile inline), `Identifier`, `Address`, `SourceRecord`, `MatchDecision`, `MergeEvent`, `ReviewCase`, `SourceSystem`, `IngestRun`. Key relationships: `IDENTIFIED_BY`, `LIVES_AT`, `LINKED_TO`, `MERGED_INTO`, `NO_MATCH_LOCK`, `HAS_FACT`, `FOR_DECISION`, `KNOWS`.

Person statuses: `active`, `merged`, `suppressed` (review state lives on `review_case`).

## Implementation Roadmap

- Phase 0: Source inventory, benchmark labeling
- Phase 1: Schema, ingestion framework, normalization library
- Phase 2: Deterministic matching, basic golden profile, basic review UI
- Phase 3: Heuristic matching, candidate generation, scoring engine
- Phase 4: Full review operations, unmerge
- Phase 5: LLM shadow evaluation
- Phase 6: LLM assist mode
- Phase 7: Monitoring, alerting, observability

## Working with This Repo

### Coding workflow
Before reporting work complete, perform a hostile review of the changed code: correctness regressions, edge cases, brittle tests, security issues, overfitting to the immediate bug. Also run a DRY check — centralize duplicated parsing, mapping, validation, or UI state logic into the existing appropriate layer rather than adding near-copy helpers.

**Local dev commands vs. CI:** do not run ruff/mypy/pytest/`npm run typecheck|lint|build` on the host to verify changes — that duplicates the Woodpecker PR/MAIN pipelines and leaves project venvs/build artifacts on the host. Push to a PR branch and read the verdict via `wpci home`. See *Agent rules (PR + MAIN)* below for the full policy. Exception: a one-shot deterministic run to *generate* a fix the verdict cannot show (e.g. `ruff check --fix` for an I001 import sort) is generating, not verifying — still push and let the pipeline verify.

### Commit discipline
Agents may stage and commit completed changes without asking first. They may also create or switch branches, push non-protected branches, open or update PRs, and perform routine GitHub issue and review operations as part of the requested workflow. Preserve unrelated changes and use focused, descriptive commits.

Two operations remain explicit user gates: **merging any PR**, and **pushing to `main` or `staging`**. Opening, updating, reviewing, or preparing a PR does not authorize its merge, and general approval to implement, commit, push, or manage a PR does not authorize a push to either protected branch. Never force-push `main` or `staging`.

### Worktrees
**Hard rule:** when creating a worktree — manually (`git worktree add`), via the `EnterWorktree` tool, or via any skill/agent — always branch it from the **current branch/HEAD**, never from `origin/main` (or `main`). This preserves in-progress branch context and avoids basing new work on a stale production branch. Do not pass a base ref that resolves to `main`/`origin/main`; if a tool or skill defaults to `main`, override it to the current branch. If a worktree was already created from `main` by mistake, recreate it from the current branch before doing any work in it.

### docker-compose.yml sync rule
`.docker/staging/docker-compose.yml` is the tracked, authoritative staging
Compose contract. Any root `docker-compose.yml` change must include the
corresponding staging change in the same commit. Review the finite exact
exception registry in `services/api/tests/test_compose_contract.py` and update
it only when an approved root/staging exception is added, removed, or changed.
The registry defines every permitted root/staging difference; all other service
definitions, images, commands, queues, environment values, mounts, networks,
and resources must remain semantically equivalent.

When editing or adding documentation:
- Follow the existing `profile-unifier-*.md` naming convention.
- Use the glossary terms consistently (Person, Source Record, Identifier, Address, Match Decision, Golden Profile, Merge Lineage, etc.).
- Keep the README document map and reading order in sync with any new files.
- Sequence diagrams use Mermaid syntax.
- The API contract is defined in both prose (`api-spec.md`) and machine-readable (`openapi-3.1.yaml`) — keep them consistent.
- The `review_action_type` enum includes both API-submitted actions and system-recorded actions — the API layer exposes only the API-submitted subset.

## DevOps / CI Validation

HyperP uses a **hybrid CI** setup: local **Woodpecker** for PR + MAIN validation, and GitHub Actions (`.github/workflows/deploy-staging.yml`) for the staging deploy. Woodpecker runs on the local `corbu` host (`corbu-woodpecker-server`/`corbu-woodpecker-agent`, docker backend, `https://ci.corbu.dev`). Inspect pipelines only through `wpci home` — never open the Woodpecker UI, paste tokens, or run legacy wrapper scripts.

### Branch boundaries (Woodpecker)
| Boundary | File | Event | Branch | Purpose |
|---|---|---|---|---|
| PR | `.woodpecker/pr.yaml` | `pull_request` | any | Fast feedback: ruff, mypy --strict, pytest, frontend2 typecheck + eslint errors-only. Green = candidate for merge to the PR branch's base branch. |
| MAIN/post-merge | `.woodpecker/main.yaml` | `push` | `main` | Materially stronger: re-runs python checks on the merged commit + frontend2 production `next build` + production `uv sync --frozen --no-dev` install. |
| Staging deploy | `.github/workflows/deploy-staging.yml` | GitHub Actions | `staging` | Not Woodpecker; unchanged. |

Woodpecker workflows must declare explicit `when:` targeting. PR is `event: pull_request` only (no `push`, no `main`, no deploy, no secrets). MAIN is `event: push` + `branch: main` only, and remains validation-only (no deploy or production secrets). The repo is **untrusted** in Woodpecker (`volumes/network/security: false`), so step containers cannot mount host volumes or the Docker socket — do not add `volumes:` or `docker compose up` steps to PR/MAIN workflows. Branch filters are safe isolation here only because no privileged syntax exists; if a privileged deployment workflow is ever added to `.woodpecker/`, it must be isolated by config-path or moved out of the autodiscovered path.

### Agent rules (PR + MAIN)
- New PRs default to the branch their PR branch was based on; do not assume `development` is the target branch.
- **Do not run project package/test/build/migration/app-server commands on the host** — no `uv run pytest`, `npm run build`, `npm test`, `venv`, migrations, or long-lived processes. Validate by pushing to a PR branch and reading the Woodpecker result via `wpci home`.
- Agents may inspect/edit files, run Git commands, stage and commit changes, push non-protected branches, open or update PRs, manage routine GitHub issue/review metadata, and run safe structural checks (`git diff --check`, `git status -sb`) without asking first.
- **Merging any PR requires explicit user instruction. Pushing to `main` or `staging` also requires explicit user instruction.** General implementation or PR-management instructions do not satisfy either gate; never force-push those protected branches.
- **Do not report PR work complete without PR pipeline evidence**: repo, branch/PR, commit SHA, pipeline number, status, and step names (from `wpci home pipeline show sparkfn/hyperP <n>`).
- **Do not report merged work complete without `main`-branch pipeline evidence.** MAIN validation runs after an explicitly authorized PR merge into `main`. A direct push to `main` is allowed only when the user explicitly instructs it. Until the post-merge pipeline finishes, report the merge with MAIN validation pending.
- Missing, skipped, removed, or failing PR/MAIN checks are blockers unless the user explicitly accepts partial/blocked adoption with a follow-up issue.
- Do not recreate git-runner / GitHub runner / host-local dependency/test workflows. The existing GitHub Actions staging deploy stays as-is.

### Frontend lint gate
`npm run lint` is `eslint src --max-warnings 9`, but the clean tree carries ~18 pre-existing `react-hooks/set-state-in-effect` warnings (0 errors), so it is **red on a clean tree**. PR/MAIN CI therefore run `npx eslint src` (errors only) — green on a clean tree, catches new errors. The repo-local `npm run lint` budget stays the authoritative developer check; getting the warning count back under 9 is a tracked follow-up. Verify your changes add **zero net warnings** (stash and compare), not a green `npm run lint` exit.

### CI lint findings (Python — observed, avoid during implementation)
The PR pipeline runs `ruff check` + `ruff format --check` + `mypy --strict` + `pytest` on both `services/api/src` and `services/ingestion/src`. A long-lived branch that was never PR'd accumulated lint debt that CI surfaced all at once on first push — avoid this by **pushing to a PR branch early** so CI catches drift incrementally rather than at merge time. Specific findings to avoid while writing code:

- **Trailing newline (W292):** every Python file MUST end with a trailing newline. The `Write`/`Edit` tools do **not** add one automatically — add it explicitly, or run `ruff check --fix` on changed files (a one-shot *generate* per the Coding workflow exception, not a verify).
- **Line length (E501, limit 100):** keep lines ≤100 chars. `ruff format` wraps code lines but does **not** reformat docstrings/comments — keep docstring summary lines ≤100 manually.
- **Closure-in-loop (B023):** a `def` inside a `for` loop that references loop variables captures them by reference (late binding). Bind **every** loop variable the closure uses as a default arg (`def f(tx, _pk=pk, _ssk=source_system_key, ...)`), not just one — ruff flags any unbound loop var even if the closure is called synchronously.
- **Format drift:** `ruff format --check` fails on any file not matching `ruff format`'s style. Before committing changed Python files, run `ruff format services/<svc>/src/<changed>.py` (a one-shot *generate*) so the PR pipeline's `--check` passes. Do **not** run `ruff format` on the whole tree from an old branch — reformat drift on untouched files means the branch sat too long without a PR.

All four are deterministic-*generate* fixes (allowed by the Coding workflow exception); then push and let the pipeline verify `mypy --strict` / `pytest`.

**mypy --strict findings (avoid during implementation):**

- **`list[str]` into `dict[str, JsonValue]` (invariance):** `list` is invariant, so `list[str]` is NOT `list[JsonValue]` even though `str` is a `JsonValue`. When a `dict[str, JsonValue]` literal carries a `list[str]` value (e.g. `customer_emails`), `cast(list[JsonValue], the_list)` at the literal — or type the local `list[JsonValue]` from the start. The same invariance bites `dict[str, object]` vs `dict[str, JsonValue]`.
- **Shared DB-row helper params:** helpers called by both the live path (SQLAlchemy `RowMapping`/`Row[Any]`) and the dump path (`DumpRow`) must NOT be typed bare `RowMapping` — the dump path can't satisfy it. Use a structural `_RowLike` Protocol with `def get(self, key: str, default: object = None) -> object` (return `object`, NOT `JsonValue` — `RowMapping.get` returns `Any`, and `Any` satisfies `object` but mypy's Protocol match rejects the narrower `JsonValue` return), OR a `Row = RowMapping | DumpRow` union. Note `dict[int, RowMapping]` return types are invariant — widening to `dict[int, Row]` requires the live builder to insert `RowMapping` into `dict[int, Row]` (works, `RowMapping` is a member).
- **`redundant-cast`:** do not `cast(T, x)` when `x` is already `T` — mypy flags it. Drop the cast.
- **`str | None` → `str` assignment:** a variable annotated `str` that receives `str | None` (from `.get()` or an optional field) is an error. Annotate `str | None`, guard with `str_or_none`, or narrow with an `isinstance(str)`/truthy check.
- **Plain `dict` vs `TypedDict` params:** passing `dict[str, object]` to a `TypedDict` param fails mypy (a TypedDict is structurally narrower than `dict[str, object]`). `cast(TheTypedDict | None, the_dict)` when the shape matches — or build it as the TypedDict.
- **TypedDict boundary > scattered `cast`:** prefer a `_coerce_customer_link(raw: object) -> _CustomerLink | None` validator at the parse boundary over `cast(_CustomerLink, customer_raw)` — the cast is documentation, not a contract, and forces every reader to re-guard with `str_or_none`. Validate once at the boundary; type the drain/propose path's `customer_link` as the TypedDict so string-ness is enforced by the type, not by per-field runtime guards.

### Inspecting CI
```bash
wpci home doctor --json
wpci home repo ls
wpci home pipeline last sparkfn/hyperP --branch <branch>
wpci home pipeline show sparkfn/hyperP <pipeline-number>
wpci home pipeline log show sparkfn/hyperP <pipeline-number> <step-name>
```
If CI needs a fresh run and no code change is needed, prefer the Woodpecker rerun path; only as a last resort create a clearly labelled empty commit (`ci: retrigger PR validation`) on the PR branch and validate the resulting pipeline SHA.

## Python Coding Standards

These rules apply to all Python code in the repository (`services/api/`, `services/ingestion/`, etc.):

- **Strict typing**: every variable, parameter, and attribute has an explicit, concrete type — no untyped bindings, no `Any`. Use `TypedDict`, `pydantic.BaseModel`, `dataclass`, `Literal`, `Protocol`, generics, or unions.
- **Return types required**: every function/method declares a return type annotation; functions returning nothing are `-> None`.
- **Type checker**: code must pass `mypy --strict` (or `pyright` strict). `# type: ignore` only with a narrow code + comment explaining why. **Known pre-existing failures**: `types_sales.py`/`types_requests.py` have pre-strict `Any` annotations — not regressions.
- **No `Any` escape hatches**: no `Any`, `cast(Any, …)`, `object` placeholders, or untyped `dict`/`list` — prefer `dict[str, SomeModel]`, `list[Person]`, `Mapping[str, str | int]`.
- **Module / function size**: modules under ~400 lines, functions under ~50 — extract cohesive helpers, split routers by resource, move Cypher/SQL into query modules.
- **Project standards**: PEP 8, PEP 257 (docstrings on public APIs), PEP 484/695 typing. Format with `ruff format`, lint with `ruff check`; use `from __future__ import annotations` only for forward refs.
- **FastAPI specifics**: request/response bodies are Pydantic models (not raw `dict`); path/query params are typed; `Depends(...)` has annotated return types; routers split per resource, registered in a single `app` factory.
- **Package manager — uv**: every Python service uses [uv](https://github.com/astral-sh/uv); each has its own `pyproject.toml` + committed `uv.lock`. Use `uv add`/`uv remove` (never `pip install`), `uv sync`, `uv run <cmd>` — no `requirements.txt`/`poetry.lock`/`Pipfile`. Dockerfiles install uv from `ghcr.io/astral-sh/uv` and run `uv sync --frozen --no-dev`.

## TypeScript / Next.js Coding Standards

These rules apply to all TypeScript code in the active app (`services/frontend2/`). The retired `services/frontend/` (v1) source is kept for reference only and is no longer built or routed; do not add new code there.

- **Strict TypeScript**: `tsconfig.json` enables `strict`, `noUncheckedIndexedAccess`, `noImplicitOverride`, `noFallthroughCasesInSwitch`; code compiles clean under `tsc --noEmit`.
- **No `any`, no unsafe casts**: never `any`, `as any`, or `as unknown as T`. Parse external data (fetch responses, `JSON.parse`, route params) via type guards or schema validators (e.g. zod) before narrowing. A bare `as` cast on `unknown` is OK only immediately after a type guard.
- **Explicit return types**: every exported function, React component, route handler, and Server Action declares its return type — `ReactElement` for components (not `React.JSX.Element`/implicit), `Promise<NextResponse>` for route handlers, `Promise<void>` for void handlers.
- **Discriminated unions over enums**: prefer `type X = "a" | "b"` + a type guard (`isX`) over TS `enum`; define option lists as `readonly` tuples and derive types from them.
- **No `Record<string, unknown>` escape hatches**: model payloads with `interface`s mirroring the API contract. Three type files — `src/lib/api-types.ts` (main: `Person`, `PersonConnection`, `SalesOrder`), `src/lib/api-types-person.ts` (person-detail: `PersonIdentifier`, `PersonSourceRecord`, merge/unmerge bodies), `src/lib/api-types-ops.ts` (admin/ops/ingestion/review: `OAuthClient`, `ReviewCaseDetail`, `IngestRunResponse`). Hand-mirroring is interim; long term, generate from `docs/profile-unifier-openapi-3.1.yaml` via `openapi-typescript`.
- **Server / client boundary discipline** (App Router):
  - Server-only modules (`src/lib/api-server.ts`, secret env vars, anything calling FastAPI directly) **must** import `"server-only"` at the top.
  - Client components declare `"use client"` first and **must not** import server-only modules; browser code talks to the BFF via `src/lib/api-client.ts`.
  - Secrets (internal URLs, tokens, DB credentials) are server-side env vars **without** `NEXT_PUBLIC_` — anything `NEXT_PUBLIC_*` ships to the browser and is public.
- **BFF pattern is mandatory**: the browser never calls FastAPI directly for UI data — all upstream traffic flows through Next.js Route Handlers under `src/app/bff/*` via `proxyToApi` (`src/lib/proxy.ts`). The public `/api/*` namespace is reserved for nginx to expose FastAPI to external services.
- **Route handler shape**: each handler exports typed `GET`/`POST`/etc. returning `Promise<NextResponse>`, declares `export const dynamic = "force-dynamic"` when not cacheable, and types Next 15 async params as `{ params: Promise<{ ... }> }`. Keep handlers thin — delegate to `proxyToApi` or a service module.
- **Data fetching in Server Components**: prefer Server Components for read-only pages, calling `apiFetch` directly (no client round-trip); parallelize with `Promise.all` and translate upstream 404s to `notFound()`.
- **Component / module size**: React components under ~150 lines, modules under ~300 — extract subcomponents and pull pure helpers out of components.
- **MUI usage**: import from per-component paths (`@mui/material/Button`), not the barrel, to keep bundles tight. Use `sx` for one-off styling, the theme for shared tokens. Wrap the App Router with `AppRouterCacheProvider` (`@mui/material-nextjs/v15-appRouter`) exactly once in `layout.tsx`.
- **Project standards**: format with Prettier, lint with `eslint src` (ESLint 9 flat config). Import order: node/external → `next/*`/`@mui/*` → `@/*` aliases → relative; use `@/` instead of long relative paths.
- **Package manager — npm**: `services/frontend2/` uses npm — always `npm install` (locally and in Docker), never `npm ci`, no `pnpm-lock.yaml`/`yarn.lock`.

### Interactive graph viewer

The person/relationship graph uses `react-force-graph-2d` (dynamically imported, SSR-disabled). Key patterns:

- **Module split**: types, colors, icon paths, and canvas callbacks live in `graph-utils.ts` (~300 lines); the viewer component and legend stay in `PersonGraphViewer.tsx`; the detail panel is in `GraphDetailPanel.tsx`.
- **Canvas icons**: Node icons use `Path2D(svgPathString)` constructed from MUI icon SVG path data (24×24 viewBox). Icons are drawn in world coordinates inside `paintNode()` — they scale with the graph zoom, not in screen pixels. The legend uses actual MUI icon React components in `Chip` elements.
- **Detail panel**: Person nodes show a rich profile card (name, status chips, key fields grid, "More" link to person page). Non-Person nodes show generic key-value properties. Both panels include an "Expand in graph" link.


### Identity-link machine synchronization (#256)

The OAuth-only `/oauth2/v1/identity-links/{events,snapshot}` routes are a separate privacy-safe ordered synchronization contract, not a replacement for `/v1/events`. They require an unscoped OAuth client with `identity-links:read` or `admin`; they are deliberately absent from MCP because MCP cannot provide the durable cursor and checkpoint semantics. Event pages freeze `through_revision`; snapshot pages freeze `snapshot_revision` and recovery is snapshot plus event tail. Snapshot reads remain unavailable until the leased, keyset-resumable baseline marks both migration and stream counter ready. Lifecycle writers must append through the existing Neo4j transaction; no second session, post-commit event, or exported raw evidence is allowed.
