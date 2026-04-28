# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

HyperP is a customer profile unification and relationship intelligence platform. It resolves the same real-world person across systems (POS, Bitrix CRM, third-party apps) and supports complex relationship use cases such as contact tracing. The initial use case is sales. The repository contains both design documentation (`docs/`) and implementation services (`services/api/`, `services/ingestion/`).

## Development Commands

### Docker (primary workflow)
```bash
docker compose up -d                                        # start all services
docker compose build --no-cache api frontend               # rebuild images (always use --no-cache for code changes)
docker compose up -d api frontend                          # restart after rebuild
docker compose logs -f api                                 # stream logs from a service
docker compose down                                        # stop all services
```
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
```bash
uv run pytest                                        # all tests
uv run pytest services/api/tests                    # API tests only
uv run pytest services/ingestion/tests             # ingestion tests only
uv run pytest services/api/tests/test_foo.py        # single file
```
Test paths are configured in the root `pyproject.toml`.

### Frontend
```bash
cd services/frontend
npm install          # already done in Docker; run locally for typecheck/lint only
npm run dev         # dev server on http://localhost:3001
npm run typecheck   # tsc --noEmit
npm run lint       # eslint src (ESLint 9 flat config, max-warnings 9)
npm run build      # production build (runs in Docker for deployment)
```
**Note:** `next lint` was removed in Next.js 15 and replaced with direct ESLint. If `npm run lint` fails, check that `eslint` and `eslint-config-next` are in `devDependencies` and that `eslint.config.mjs` exists.
The frontend Dockerfile uses `npm install --legacy-peer-deps` because `@mui/x-date-pickers@7` has a peer dependency range that conflicts with `@mui/material@6`. Do not remove this flag.
**ESLint warning budget**: `--max-warnings 9` is enforced. The budget is currently fully consumed by pre-existing `react-hooks/set-state-in-effect` warnings in existing pages. Any new `"use client"` page that follows the `useEffect(() => { void loadX(); }, [loadX])` data-fetching pattern must add `// eslint-disable-next-line react-hooks/set-state-in-effect` on the `void loadX()` line to avoid exceeding the limit.

---

## Service Topology

Seven Docker containers defined in `docker-compose.yml`:

| Service | Image / Build | Internal address | Notes |
|---|---|---|---|
| `neo4j` | `neo4j:5-community` | `bolt://neo4j:7687` | HTTP browser at `:7474` |
| `redis` | `redis:7-alpine` | `redis://redis:6379` | Celery broker (db 0) + results (db 1) + token revocation store + public share-link tokens (TTL auto-cleanup) |
| `api` | `services/api/Dockerfile` | `http://api:3000` | FastAPI/uvicorn; not exposed directly |
| `frontend` | `services/frontend/Dockerfile` | `http://frontend:3001` | Next.js; not exposed directly |
| `web` | `nginx:1.27-alpine` | exposed on `:80` | Reverse proxy; routes `/api/*` → FastAPI (strips `/api` prefix, FastAPI root_path is `/api`), rest → frontend |
| `worker` | `services/ingestion/Dockerfile` | — | Celery worker; `celery -A src.celery_app worker` |
| `beat` | `services/ingestion/Dockerfile` | — | Celery beat scheduler; cron schedules from env vars |

**Startup:** `logging.basicConfig(level=...)` in `src/app.py` also silences the `neo4j.notifications` logger (Cypher deprecation warnings) so they don't flood the API container logs. Real Neo4j errors at ERROR level are unaffected.

Auth flow: browser → next-auth (Google OAuth) → `googleIdToken` stored in JWT session → Next.js BFF attaches as `Authorization: Bearer` → FastAPI verifies via `require_active_user` dependency. Token revocation is backed by Redis: `POST /v1/auth/logout` adds the token's `jti` to a Redis SET (TTL auto-cleanup), and the in-process user cache is also evicted immediately. Google refresh tokens are revoked via Google's revocation endpoint. If the refresh token expires, NextAuth sets `session.error = "RefreshTokenError"` and auto-redirects to `/login`.

**Auth bypass**: routes under `/public/**` and `/login`, `/api/health`, and `/bff/auth/**` are explicitly allowed through the NextAuth middleware without a session. The public person pages (`/public/persons/[token]`) are served entirely unauthenticated — they call `apiFetch` with `authToken: null` so no Bearer header is sent.

---

## Recurring Code Patterns

### Response envelope
All FastAPI endpoints return `ApiResponse[T]`. Use `envelope()` from `src/http_utils.py`:
```python
return envelope(data, request, cursor=next_cur, total_count=count)
```
`ResponseMeta` carries `request_id`, `next_cursor`, and `total_count`. Frontend reads these via `bffFetchEnvelope` in `src/lib/api-client.ts`.

**Exception — bare responses**: Admin management endpoints (e.g. `GET /v1/admin/api-keys`) return bare `list[T]` or bare objects without `envelope()`. This is intentional for machine-to-machine callers. `apiFetch` in `api-server.ts` handles all three non-envelope shapes automatically:
- `null` body (HTTP 204 No Content) → `{ data: null, meta: ... }`
- bare array → `{ data: [...], meta: ... }`
- bare object with no `"data"` key → `{ data: {...}, meta: ... }`

### Cursor-based pagination (backend)
`page_window(cursor, raw_limit)` in `src/http_utils.py` decodes the base64 cursor to a skip offset. Pattern for every list endpoint:
```python
skip, limit = page_window(cursor, raw_limit)
rows = fetch_rows(skip, limit + 1)          # fetch one extra to detect has_more
has_more = len(rows) > limit
return envelope(rows[:limit], request,
                cursor=next_cursor(skip, limit, has_more),
                total_count=_to_total(count_record))
```

### Cursor-based pagination (frontend)
Use `usePaginatedFetch<T>(basePath)` from `src/lib/usePaginatedFetch.ts`. It manages `cursor`/`prevStack`/`nextCursor` state and exposes `{ rows, loading, error, from, to, total, hasPrev, hasNext, goNext, goPrev }`. BFF route handlers must forward `limit`/`cursor` from `searchParams` using `searchParamsToQuery(searchParams)` from `src/lib/proxy.ts`.

### BFF proxy
Every browser→API call goes through a Next.js route handler. Standard thin handler:
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

### Graph query modules
All Cypher strings live as module-level string constants in `services/api/src/graph/queries/` and `services/ingestion/src/graph/queries/`. The `E501` line-length rule is disabled for these files so queries aren't artificially wrapped. Routes import query constants by name; they never embed Cypher inline.

### Mappers vs converters
- `graph/converters.py`: primitive type coercions (`to_str`, `to_int`, `to_float`, `to_optional_*`, `to_iso_or_none`, `to_datetime`, `to_str_list`, `encode_cursor`/`decode_cursor`). Also exports type aliases `GraphScalar`, `GraphValue`, `GraphRecord` — use these instead of `Any` when typing raw Neo4j records. Used by mappers.
- `graph/mappers*.py`: Neo4j `Record` → Pydantic model. One mapper file per domain (persons, entities, sales, reports).

### JWT / Google ID token verification
`services/api/src/auth/verify.py` uses a **self-contained** RS256 verifier with a 300-second clock-skew tolerance (absorb drift between our server and Google's token-issuing servers). It does NOT use `google-auth`'s `verify_oauth2_token` directly — that library has a strict `nbf` check that causes spurious 401s. Signature is verified against Google's public cert endpoint.
Token revocation is handled in `auth/revoke.py`: the raw JWT is decoded (no verification) to extract `jti` and `exp`, then checked against Redis before hitting the signature verifier. The in-process user cache (`auth/deps.py:_USER_CACHE`) is keyed by `jti` and evicted immediately on `POST /v1/auth/logout` so revoked tokens cannot be served from a warm cache.

### Server-to-server API keys
API keys are enabled when `API_KEYS_ENABLED=true`. They are HMAC-SHA256 hashed (using `API_KEY_SECRET`) and stored on `(:ApiKey)` nodes in Neo4j. A Redis set (`revoked_api_keys`) provides a fast-path rejection before hitting Neo4j.

`auth/deps.py` defines `ApiKeyUser(AuthUser)` — a subclass that carries `key_scopes: list[str]`. All `require_*` dependency functions accept `AuthUser | ApiKeyUser`. The `X-Api-Key` header is checked first (when enabled); on a missing or invalid key it falls back to Bearer token auth. Scopes are checked with `check_scope(scopes, required)` — the `"admin"` scope is a superset of all others.

Admin routes that manage keys (`src/routes/api_keys.py`) are registered with the standard `active` dependency list (requires an authenticated user), not a separate public router.

### Public (unauthenticated) API endpoints
When an endpoint must be publicly accessible (no Bearer token), register it on a separate router that is included in `app.py` **without** the `active` dependency list. The auth-gated action that produces the public resource (e.g. generating a share link) uses the normal `person_links_router` registered with `require_active_user`. Example from `src/routes/public_pages.py`:
```python
public_router = APIRouter(prefix="/v1/public")      # no auth — included bare
person_links_router = APIRouter(prefix="/v1/persons")  # included with active deps

# In app.py:
app.include_router(public_router)                          # no auth
app.include_router(person_links_router, dependencies=active)
```
Public share-link tokens are UUID strings stored in Redis with a TTL (`public_link:{token}` → `person_id`). The expiry is controlled by `PUBLIC_PAGE_EXPIRY_MINUTES` (default 30, set in `config.py` and passed via `docker-compose.yml`).

On the frontend, a Server Component page under `/public/**` fetches directly from the API with `authToken: null`:
```typescript
const res = await apiFetch<Person>(`/public/persons/${token}`, { authToken: null });
```
`authToken: null` skips the `auth()` call in `apiFetch` and sends no Authorization header. The client component for interactive sections (e.g. expandable sales rows) must be extracted to a separate `"use client"` file since the page itself is a Server Component.

### Ingestion dispatch
Always dispatch via Celery — never call `run_ingestion()` directly:
```python
run_ingestion_task.delay(source_key, mode="batch")
```
The task enforces a Redis-backed cluster-wide concurrency cap (`MAX_CONCURRENT_INGESTIONS`, default 1) and retries automatically if a slot is busy.

### Date picker fields
Date range filters use `DatePickerField` (a wrapper around `@mui/x-date-pickers@7` `DatePicker` + `dayjs` adapter with `en-gb` locale). The display format is `DD MMM YYYY` (e.g. "28 Apr 2026"), matching `formatDob`/`formatDate` from `display.ts`. The component stores values internally as ISO `YYYY-MM-DD` strings for API compatibility. Use `DatePickerField` for any date input that should match the table row date format — do not fall back to `<TextField type="date">`.

---

## Repository Structure

All documents live in `docs/` and follow the naming convention `profile-unifier-*.md` (plus one `.yaml`).

**Recommended reading order**: PRD → Glossary → Architecture → Matching Spec → Policy Decisions → Graph Schema → Graph Model Diagram → API Spec → OpenAPI 3.1 → Reviewer Workflow → Sequence Diagrams → Roadmap → Scaffold Architecture

## Key Design Decisions

- **Database**: Neo4j (decided). The platform must support contact tracing and complex multi-hop relationship queries beyond simple identity resolution. Neo4j's native graph storage and Cypher query language are the right fit. ACID transactions are available in Neo4j 4+.
- **Precision over recall**: optimize for low false-merge rates; false merges have high operational cost.
- **Immutable source facts**: source records are never modified after ingestion — all changes create new records.
- **Explainable decisions**: every merge/no-match must have traceable reasons.
- **4-layer matching**: Deterministic rules → Heuristic scoring → LLM adjudication (shadow-only in MVP) → Human review.
- **Confidence bands**: ≥0.90 auto-merge, 0.60–0.89 human review, <0.60 no-match (thresholds to be calibrated).
- **Sensitive data**: NRIC and Singpass-linked data require special handling; govt IDs stored as salted hashes.
- **Controlled rollout**: LLM starts in shadow/assist mode only — no autonomous production merges in MVP.
- **Merge lineage**: merge history is stored as native graph relationships (`MERGED_INTO`) between Person nodes. Path compression via relationship rewiring guarantees max 1 hop for canonical person lookups.
- **Unmerge**: post-merge source records stay with the surviving person but are flagged for review.
- **Concurrency**: ingestion partitioned by blocking key to prevent race conditions.
- **Cardinality caps**: blocking keys with too many matches are skipped; configurable per identifier type.
- **Pair ordering**: lock relationships (`NO_MATCH_LOCK`) enforce `left.person_id < right.person_id` to prevent duplicates.
- **Golden profile recomputation**: synchronous within the merge transaction (Neo4j ACID transactions).
- **Downstream events**: polling endpoint (`GET /v1/events?since=`) for now, designed for future push migration.
- **Identifier aging**: time-based deactivation via `last_confirmed_at`, configurable per type.
- **Retention**: `retention_expires_at` property on relevant nodes (SourceRecord, MatchDecision, MergeEvent); NULL for legal holds.
- **Scoring model**: must use conditional weighting or capping, not simple additive weights.
- **Graph-native candidate generation**: candidate persons are found by traversing shared Identifier nodes in Neo4j, not by index-based blocking-key lookups. Composite blocking (DOB + name) falls back to index queries.
- **Source record types**: every `SourceRecord` carries `record_type` of either `system` (deterministic extract from another service's system of record) or `conversation` (heuristic extract from chat / voice transcripts, with `extraction_confidence`, `extraction_method`, `conversation_ref`). Conversation-sourced evidence is never eligible for deterministic auto-merge — it always flows through Layer 2 scoring and at most reaches the review band. Hard NO_MATCH rules (locks, conflicting govt IDs) still fire for conversation records as blockers.
- **Social Person-to-Person relationship** (`KNOWS`): a directed `(:Person)-[:KNOWS]->(:Person)` edge mirrors the Fundbox `contacts` table (emergency contact / next-of-kin / referrer). Properties include `relationship_label`, `relationship_category`, source provenance, and `status`/`approved_at`. `KNOWS` is sourced, never inferred by the matching engine, and does not affect identity resolution. It supersedes the previously post-MVP `REFERRED_BY` / `WORKS_WITH` / `FAMILY_OF` proposal — narrower types may be added later if a use case needs them.
- **Interaction model** (post-MVP): Interaction nodes for contact tracing will connect to Person nodes in the same Neo4j graph.
- **Data deletion**: graph deletion requires detaching all relationships before removing a Person node. Shared Identifier nodes survive individual person deletion.

## Architecture Summary

Ingestion → Normalization → Candidate Generation (graph traversal through shared Identifier and Address nodes) → Match Engine → Person Graph (Neo4j) → Golden Profile → Review Operations → APIs

Core graph nodes: `Person` (with golden profile properties inline), `Identifier` (shared across persons — the graph backbone for contact tracing), `Address` (shared across persons — enables "who else lives here?" traversal), `SourceRecord`, `MatchDecision`, `MergeEvent`, `ReviewCase`, `SourceSystem`, `IngestRun`. Many relational concepts are modeled as relationships: `IDENTIFIED_BY`, `LIVES_AT`, `LINKED_TO`, `MERGED_INTO`, `NO_MATCH_LOCK`, `HAS_FACT`, `FOR_DECISION`.

Person statuses: `active`, `merged`, `suppressed` (no `under_review` — review state is tracked on `review_case`).

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

When editing or adding documentation:
- Follow the existing `profile-unifier-*.md` naming convention.
- Use the glossary terms consistently (Person, Source Record, Identifier, Address, Match Decision, Golden Profile, Merge Lineage, etc.).
- Keep the README document map and reading order in sync with any new files.
- Sequence diagrams use Mermaid syntax.
- The API contract is defined in both prose (`api-spec.md`) and machine-readable (`openapi-3.1.yaml`) — keep them consistent.
- The `review_action_type` enum includes both API-submitted actions and system-recorded actions — the API layer exposes only the API-submitted subset.

## Python Coding Standards

These rules apply to all Python code in the repository (`services/api/`, `services/ingestion/`, etc.):

- **Strict typing**: every variable, parameter, and attribute must have an explicit, concrete type. No untyped bindings, no implicit `Any`, no `typing.Any`. Use `TypedDict`, `pydantic.BaseModel`, `dataclass`, `Literal`, `Protocol`, generics, or unions instead.
- **Return types required**: every function and method that returns a value must declare a return type annotation. Functions that return nothing must be annotated `-> None`.
- **Type checker**: code must pass `mypy --strict` (or `pyright` in strict mode). `# type: ignore` is only acceptable with a narrow code and a comment explaining why. **Known pre-existing failures**: `types_sales.py` and `types_requests.py` contain `Any` annotations that predate strict enforcement — mypy reports them but they are not regressions introduced by new code.
- **No `Any` escape hatches**: do not use `Any`, `cast(Any, …)`, `object` as a placeholder, or untyped `dict`/`list`. Prefer `dict[str, SomeModel]`, `list[Person]`, `Mapping[str, str | int]`, etc.
- **Module / function size**: keep modules under ~400 lines and functions under ~50 lines. Refactor longer ones by extracting cohesive helpers, splitting routers by resource, and moving Cypher/SQL into dedicated query modules.
- **Project standards**: follow PEP 8, PEP 257 (docstrings on public APIs), and PEP 484/695 typing. Format with `ruff format`, lint with `ruff check`, and prefer `from __future__ import annotations` only when needed for forward refs.
- **FastAPI specifics**: request and response bodies must be Pydantic models (not raw `dict`). Path/query parameters must be typed. Dependencies (`Depends(...)`) must have annotated return types. Routers should be split per resource and registered in a single `app` factory.
- **Package manager — uv**: every Python service uses [uv](https://github.com/astral-sh/uv) for dependency management. Each service has its own `pyproject.toml` + committed `uv.lock`. Use `uv add` / `uv remove` (never `pip install`), `uv sync` to install, and `uv run <cmd>` to execute. Do not introduce `requirements.txt`, `poetry.lock`, `Pipfile`, or any other manager's metadata. Dockerfiles install uv from `ghcr.io/astral-sh/uv` and use `uv sync --frozen --no-dev`.

## TypeScript / Next.js Coding Standards

These rules apply to all TypeScript code in the repository (`services/frontend/`, etc.):

- **Strict TypeScript**: `tsconfig.json` must enable `strict`, `noUncheckedIndexedAccess`, `noImplicitOverride`, and `noFallthroughCasesInSwitch`. Code must compile clean under `tsc --noEmit`.
- **No `any`, no unsafe casts**: never use `any`, `as any`, or `as unknown as T`. Parse external data (fetch responses, `JSON.parse`, route params) through type guards or schema validators (e.g. zod) before narrowing. A bare `as` cast on an `unknown` value is acceptable only when immediately preceded by a type guard.
- **Explicit return types**: every exported function, React component, route handler, and Server Action must declare its return type. Use `ReactElement` (not `React.JSX.Element` or implicit) for component returns; `Promise<NextResponse>` for route handlers; `Promise<void>` for handlers with no return value.
- **Discriminated unions over enums**: prefer `type X = "a" | "b"` plus a type guard (`isX`) over TS `enum`. Define option lists as `readonly` tuples and derive types from them.
- **No `Record<string, unknown>` escape hatches**: model payloads with `interface`s mirroring the API contract. Types live in three files — `src/lib/api-types.ts` (main contract: `Person`, `PersonConnection`, `SalesOrder`, etc.), `src/lib/api-types-person.ts` (person-detail sub-types: `PersonIdentifier`, `PersonSourceRecord`, merge/unmerge request/response bodies), and `src/lib/api-types-ops.ts` (admin/ops/ingestion/review payloads: `ApiKey`, `ReviewCaseDetail`, `IngestRunResponse`, etc.). Hand-mirroring is the interim approach; long term, generate types from `docs/profile-unifier-openapi-3.1.yaml` via `openapi-typescript`.
- **Server / client boundary discipline** (App Router):
  - Server-only modules (`src/lib/api-server.ts`, anything reading secret env vars, anything calling FastAPI directly) **must** import `"server-only"` at the top.
  - Client components must declare `"use client"` on the first line and **must not** import server-only modules. Browser code talks to the BFF via `src/lib/api-client.ts`.
  - Secrets (internal service URLs, tokens, DB credentials) are server-side env vars **without** the `NEXT_PUBLIC_` prefix. Anything `NEXT_PUBLIC_*` is shipped to the browser — treat it as public.
- **BFF pattern is mandatory**: the browser must never call FastAPI directly for app UI data. All UI upstream traffic flows through Next.js Route Handlers under `src/app/bff/*`, which use `proxyToApi` from `src/lib/proxy.ts`. The public `/api/*` namespace is reserved for nginx to expose FastAPI directly for external services.
- **Route handler shape**: each route handler exports typed `GET`/`POST`/etc. returning `Promise<NextResponse>`, declares `export const dynamic = "force-dynamic"` when it must not be cached, and types Next 15 async params as `{ params: Promise<{ ... }> }`. Keep handlers thin — delegate to `proxyToApi` or a service module. The logout BFF handler at `src/app/bff/auth/logout/route.ts` calls `apiFetch` to revoke the token server-side before returning.
- **Data fetching in Server Components**: prefer Server Components for read-only pages and call `apiFetch` directly (no client round-trip). Parallelize independent fetches with `Promise.all`. Translate upstream 404s to `notFound()`.
- **Component / module size**: keep React components under ~150 lines and modules under ~300 lines. Extract subcomponents (e.g. `PersonHeader`, `ConnectionsCard`) rather than letting a single page balloon. Extract pure helpers (`statusColor`, `buildSearchParams`) out of components.
- **MUI usage**: import from per-component paths (`@mui/material/Button`) not the barrel (`@mui/material`) to keep bundles tight. Use the `sx` prop for one-off styling, the theme for shared tokens. Wrap the App Router with `AppRouterCacheProvider` from `@mui/material-nextjs/v15-appRouter` exactly once in `layout.tsx`.
- **Project standards**: format with Prettier, lint with `eslint src` (ESLint 9 flat config). Imports ordered: node/external → `next/*` and `@mui/*` → `@/*` aliases → relative. Use the `@/` path alias instead of long relative paths.
- **Package manager — npm**: `services/frontend/` uses npm. Always use `npm install` (locally and in Docker) — do not use `npm ci`. Do not introduce `pnpm-lock.yaml` or `yarn.lock`.

### Interactive graph viewer

The person/relationship graph uses `react-force-graph-2d` (dynamically imported, SSR-disabled). Key patterns:

- **Module split**: types, colors, icon paths, and canvas callbacks live in `graph-utils.ts` (~300 lines); the viewer component and legend stay in `PersonGraphViewer.tsx`; the detail panel is in `GraphDetailPanel.tsx`.
- **Canvas icons**: Node icons use `Path2D(svgPathString)` constructed from MUI icon SVG path data (24×24 viewBox). Icons are drawn in world coordinates inside `paintNode()` — they scale with the graph zoom, not in screen pixels. The legend uses actual MUI icon React components in `Chip` elements.
- **Force configuration**: `nodeVal` is set to `NODE_SIZE * 3` so the simulation respects node area for collision. `d3Force("link").distance()` and `d3Force("charge").strength()` are configured via ref callback after mount to prevent overlap while keeping the graph compact.
- **Detail panel**: Person nodes show a rich profile card (name, status chips, key fields grid, "More" link to person page). Non-Person nodes show generic key-value properties. Both panels include an "Expand in graph" link.
