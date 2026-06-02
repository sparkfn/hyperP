# Source Records tab — design

**Date:** 2026-06-02
**Branch:** `feature/source-records-tab`
**Status:** Draft for review

## Summary

Add a new **read-only** "Source Records" tab to the person detail page in `frontend2`. It lists every source record linked to a person, lets the user filter by entity, and expands each record in place to reveal its full metadata, normalized payload, and (separately collapsible) raw source payload.

Purpose, per agreed scope:

- **Provenance / audit** — see which source system/entity contributed what, and when.
- **Raw payload inspection** — drill into each record's `normalized_payload` and original `raw_payload`.
- **Data-quality review** — surface `link_status`, `record_type` (system vs conversation), and extraction confidence across sources.

No mutations. Operational actions (re-link, flag, re-ingest) are explicitly out of scope.

## UX

Layout direction **A** (flat list, expand in place), chosen for consistency with the existing Identifiers tab.

- **Entity filter bar** — chips at top: `All · N`, then one chip per entity (`entity_display_name`, falling back to `source_system` when entity is null) with its record count. Selecting a chip filters the list **server-side** and resets pagination. Counts come from the facets endpoint, so they are complete regardless of the current page.
- **Collapsed row** — source system, entity, `source_record_id` (+ version), `record_type` badge (system/conversation), `link_status` badge, and the formatted observed date.
- **Expanded row** (click to toggle) shows three sections:
  1. **Record** — metadata grid: source system, entity, record ID, version, type, link status, observed (display), ingested (display), record PK. Conversation records also show extraction method + `extraction_confidence_display`.
  2. **Normalized payload** — identifiers as pills (`type · value`, verified tick), address line, attributes as pills.
  3. **Raw payload** — a **separately collapsible** block (collapsed by default) showing the original source JSON, pretty-printed.
- **Pagination** — server-driven via the existing `usePaginatedFetch` + `TabPagination` pattern (20/page), scoped to the active entity filter.

## Backend changes (`services/api`)

All new endpoints land on the existing `persons` router, which is already included under the FastAPI `/app/v2` mount — so they are served at `/api/app/v2/persons/...` automatically and reachable by the `frontend2` BFF with no extra wiring.

### 1. List endpoint filters

`GET /persons/{id}/source-records` gains optional query params:

- `entity_key: str | None`
- `record_type: Literal["system", "conversation"] | None`

Threaded into `PersonRepository.get_source_records(...)` and the Cypher query. Filter values are passed as **Cypher parameters**, never string-interpolated. Pagination (`skip`/`limit`, count-based `total`) continues to work against the filtered set.

### 2. Facets endpoint

`GET /persons/{id}/source-record-entities` → `ApiResponse[list[SourceRecordEntityFacet]]`

New Pydantic model:

```python
class SourceRecordEntityFacet(BaseModel):
    entity_key: str | None
    entity_display_name: str | None
    source_system: str
    count: int
```

Returns one facet per distinct (`source_system`, `entity_key`) the person has source records for, with counts over the **whole** person (not a page). New repo protocol method + Neo4j implementation, new query constant in `graph/queries/`, new BFF route.

### 3. API-side text formatting

The display fields go on a **new v2-only presentation model**, leaving the shared `SourceRecord` (and therefore the public person-page contract) untouched:

```python
class SourceRecordView(SourceRecord):
    observed_at_display: str               # e.g. "02 Apr 2026"
    ingested_at_display: str               # e.g. "02 Apr 2026, 03:14 AM"
    extraction_confidence_display: str | None  # e.g. "82%"; None for system records
```

The authenticated `GET /persons/{id}/source-records` endpoint changes its `response_model` to `ApiResponse[list[SourceRecordView]]` and maps each domain `SourceRecord` → `SourceRecordView` in the route (adding the formatted strings). The repo layer still returns domain `SourceRecord`. Formatting is centralized in a small API helper (UTC, matching current frontend output) so other endpoints can adopt it later; the helper is reusable for monetary amounts if amount fields appear (source records carry none, so the "format amounts" principle is honored here via the confidence percentage).

> The public endpoint `GET /persons/{token}/source-records` (`routes/public_pages.py`) keeps returning the unchanged `SourceRecord` — the public contract is **not** touched in this iteration.

## Frontend changes (`services/frontend2`)

- **Types** (`src/lib/api-types-person.ts`): extend `PersonSourceRecord` with `raw_payload`, `conversation_ref`, `observed_at_display`, `ingested_at_display`, `extraction_confidence_display`. Add `SourceRecordEntityFacet`.
- **BFF**: new route `bff/persons/[personId]/source-record-entities/route.ts` (thin `proxyToApi`). The existing `bff/persons/[personId]/source-records/route.ts` already forwards query params, so `entity_key`/`record_type` pass through unchanged.
- **Up-front facets fetch**: the page's existing initial `Promise.all` gains a fetch of `/bff/persons/[personId]/source-record-entities`, stored alongside `detailData`. This makes the **tab count badge visible on initial page load** (badge = sum of facet counts) and avoids a refetch when the tab opens.
- **Tab wiring** (`src/app/persons/[personId]/page.tsx`): add `"source-records"` to the `Tab` union, a `TabConfig` entry whose `count` = sum of the loaded facet counts (shown immediately, like Sales/Connections), and a render branch.
- **New component** `SourceRecordsTab` (defined **in `page.tsx`** alongside the other six tabs — the established pattern; all tabs live in this file and share in-file helpers like `TabPagination`/`TabEmptyState`/`titleCase`/`maskNric`, so extracting just this one would force a risky shared-helper extraction that fights the existing structure). It receives the already-loaded facets as a prop to render the chip bar, fetches the paginated list via `usePaginatedFetch` (passing `entity_key`), and renders expand-in-place rows with the collapsible raw-payload block. All dates/percentages render from the API `*_display` strings — **no** client-side date/number formatting. New CSS goes in the existing `person.module.css`.

### record_type filter

The `record_type` query param exists on the endpoint but is **not** surfaced as a UI control in this iteration (endpoint-only, for future use).

## Testing

- **API** (`services/api/tests`, pytest):
  - `entity_key` / `record_type` filters narrow the list correctly and pagination totals reflect the filtered set.
  - Facets endpoint returns correct per-entity counts over the full person.
  - Display fields formatted correctly, including `extraction_confidence_display is None` for system records.
- **Frontend**: `npm run typecheck` (`tsc --noEmit`) + `npm run lint`. The new `"use client"` data-fetching follows the `eslint-disable-next-line react-hooks/set-state-in-effect` convention (CLAUDE.md ESLint budget).
- Lint/format/type-check the Python per CLAUDE.md (`ruff`, `mypy --strict`).

## Out of scope

- Operational actions on records (re-link, flag, re-ingest).
- The sidebar "Sources / Entity" card — left as-is (it reads only the first page of records; not changed here).
- The legacy `frontend` (v1) service.
- A `record_type` UI filter control.
