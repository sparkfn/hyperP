# Source Records Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only "Source Records" tab to the person detail page (frontend2) that lists a person's source records, filters them by entity (server-side), and expands each record in place to show metadata, normalized payload, and a collapsible raw payload — with dates/percentages formatted by the API.

**Architecture:** Backend adds two things to the existing `persons` router (already mounted under FastAPI `/app/v2`): optional `entity_key`/`record_type` filters on the source-records list endpoint, and a new facets endpoint for the entity chips. A new v2-only `SourceRecordView` presentation model carries API-formatted display strings, leaving the shared `SourceRecord` (public contract) untouched. Frontend adds the tab to `page.tsx`, fetches facets up front for the count badge, and renders a paginated, filterable, expand-in-place list.

**Tech Stack:** FastAPI + Neo4j (Cypher) + Pydantic (mypy --strict) on the API; Next.js App Router + TypeScript (strict) + CSS modules on frontend2.

---

## File Structure

**Backend (`services/api`):**
- Create: `src/display_format.py` — presentation helpers (date/datetime/percent → display strings).
- Create: `tests/test_display_format.py` — unit tests for the helpers.
- Modify: `src/types.py` — add `SourceRecordView` and `SourceRecordEntityFacet` models.
- Modify: `src/graph/queries/persons.py` — add filters to source-records queries; add facets query.
- Modify: `src/graph/queries/__init__.py` — export the new facets query constant.
- Modify: `src/graph/mappers.py` — add `map_source_record_entity_facet`.
- Modify: `src/repositories/protocols/person.py` — add filter params + facets method.
- Modify: `src/repositories/neo4j/person.py` — implement filters + facets.
- Modify: `src/routes/persons.py` — map `SourceRecord`→`SourceRecordView`; add filters + facets route.
- Create: `tests/test_source_records_tab.py` — endpoint tests (filters, facets, display fields).

**Frontend (`services/frontend2`):**
- Modify: `src/lib/api-types-person.ts` — extend `PersonSourceRecord`; add `SourceRecordEntityFacet`.
- Create: `src/app/bff/persons/[personId]/source-record-entities/route.ts` — BFF facets route.
- Modify: `src/app/persons/[personId]/page.tsx` — `Tab` union, facets fetch, tab config, render branch, `SourceRecordsTab` component.
- Modify: `src/app/persons/[personId]/person.module.css` — new styles for chips, metadata grid, pills, raw-payload block.

---

## Task 1: API display-formatting helper

**Files:**
- Create: `services/api/src/display_format.py`
- Test: `services/api/tests/test_display_format.py`

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/test_display_format.py
"""Tests for presentation formatting helpers."""

from src.display_format import (
    format_confidence_pct,
    format_display_date,
    format_display_datetime,
)


def test_format_display_date_basic() -> None:
    assert format_display_date("2026-04-02T05:30:00Z") == "02 Apr 2026"


def test_format_display_date_converts_to_utc() -> None:
    # 00:30 at +08:00 is 16:30 the previous day in UTC.
    assert format_display_date("2026-04-02T00:30:00+08:00") == "01 Apr 2026"


def test_format_display_date_empty_returns_empty() -> None:
    assert format_display_date("") == ""
    assert format_display_date("not-a-date") == ""


def test_format_display_datetime_basic() -> None:
    assert format_display_datetime("2026-04-02T03:14:00Z") == "02 Apr 2026, 03:14 AM"


def test_format_display_datetime_pm() -> None:
    assert format_display_datetime("2026-04-02T15:14:00Z") == "02 Apr 2026, 03:14 PM"


def test_format_display_datetime_midnight() -> None:
    assert format_display_datetime("2026-04-02T00:05:00Z") == "02 Apr 2026, 12:05 AM"


def test_format_confidence_pct() -> None:
    assert format_confidence_pct(0.82) == "82%"
    assert format_confidence_pct(1.0) == "100%"
    assert format_confidence_pct(0.005) == "1%"


def test_format_confidence_pct_none() -> None:
    assert format_confidence_pct(None) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/api/tests/test_display_format.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.display_format'`

- [ ] **Step 3: Write minimal implementation**

```python
# services/api/src/display_format.py
"""Presentation helpers that format raw values into display strings.

Centralized so the API can hand the frontend ready-to-render text (dates,
percentages) and the frontend does no locale/number formatting. Output matches
the existing frontend2 en-SG / UTC style: "02 Apr 2026" and
"02 Apr 2026, 03:14 AM".
"""

from __future__ import annotations

from datetime import datetime, timezone

_MONTHS: tuple[str, ...] = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _parse_utc(value: str) -> datetime | None:
    """Parse an ISO-8601 string to a naive UTC datetime, or None if unparseable."""
    if not value:
        return None
    text = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def format_display_date(value: str) -> str:
    """Format an ISO string as 'DD Mon YYYY' in UTC; '' if unparseable/empty."""
    dt = _parse_utc(value)
    if dt is None:
        return ""
    return f"{dt.day:02d} {_MONTHS[dt.month - 1]} {dt.year}"


def format_display_datetime(value: str) -> str:
    """Format an ISO string as 'DD Mon YYYY, hh:mm AM/PM' in UTC; '' if invalid."""
    dt = _parse_utc(value)
    if dt is None:
        return ""
    hour12 = dt.hour % 12 or 12
    meridiem = "AM" if dt.hour < 12 else "PM"
    return f"{dt.day:02d} {_MONTHS[dt.month - 1]} {dt.year}, {hour12:02d}:{dt.minute:02d} {meridiem}"


def format_confidence_pct(value: float | None) -> str | None:
    """Format a 0..1 confidence as an integer percent string, or None."""
    if value is None:
        return None
    return f"{round(value * 100)}%"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest services/api/tests/test_display_format.py -v`
Expected: PASS (all 9 tests)

- [ ] **Step 5: Lint/type-check**

Run: `uv run --package profile-unifier-api ruff format services/api/src/display_format.py && uv run --package profile-unifier-api ruff check services/api/src/display_format.py && uv run --package profile-unifier-api mypy --strict services/api/src/display_format.py`
Expected: no errors.

---

## Task 2: API models — `SourceRecordView` and `SourceRecordEntityFacet`

**Files:**
- Modify: `services/api/src/types.py` (after the `SourceRecord` class, ~line 200)

- [ ] **Step 1: Add the models**

Insert immediately after the `SourceRecord` class definition:

```python
class SourceRecordView(SourceRecord):
    """v2 presentation model: SourceRecord plus API-formatted display strings.

    Kept separate from SourceRecord so the public person-page contract (which
    returns SourceRecord) is unaffected.
    """

    observed_at_display: str
    ingested_at_display: str
    extraction_confidence_display: str | None = None


class SourceRecordEntityFacet(BaseModel):
    """Per-entity source-record count for a person, for filter chips."""

    source_system: str
    entity_key: str | None = None
    entity_display_name: str | None = None
    count: int
```

- [ ] **Step 2: Type-check**

Run: `uv run --package profile-unifier-api mypy --strict services/api/src/types.py`
Expected: no new errors (pre-existing `types_sales.py`/`types_requests.py` Any warnings are unrelated and not in this file).

- [ ] **Step 3: Commit**

```bash
git add services/api/src/display_format.py services/api/tests/test_display_format.py services/api/src/types.py
git commit -m "feat(api): add display-format helpers and source-record view/facet models"
```

---

## Task 3: Cypher — filters on source-records queries + facets query

**Files:**
- Modify: `services/api/src/graph/queries/persons.py` (`GET_PERSON_SOURCE_RECORDS` ~line 78, `COUNT_PERSON_SOURCE_RECORDS` ~line 408)
- Modify: `services/api/src/graph/queries/__init__.py`

- [ ] **Step 1: Add `entity_key`/`record_type` filters to the list query**

Replace `GET_PERSON_SOURCE_RECORDS` with (adds an OPTIONAL-MATCH-aware `WHERE`; values are Cypher params so the query structure stays static):

```python
GET_PERSON_SOURCE_RECORDS = """
MATCH (sr:SourceRecord)-[:LINKED_TO]->(p:Person {person_id: $person_id})
MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
OPTIONAL MATCH (ss)-[:OPERATED_BY]->(entity:Entity)
WHERE ($entity_key IS NULL OR entity.entity_key = $entity_key)
  AND ($record_type IS NULL OR sr.record_type = $record_type)
RETURN sr {
  .source_record_pk, .source_record_id, .source_record_version,
  .record_type, .extraction_confidence, .extraction_method,
  .link_status, .observed_at, .ingested_at,
  .conversation_ref, .raw_payload, .normalized_payload
} AS source_record,
ss.source_key AS source_system,
p.person_id AS linked_person_id,
entity.entity_key AS entity_key,
entity.display_name AS entity_display_name
ORDER BY sr.observed_at DESC
SKIP $skip LIMIT $limit
"""
```

- [ ] **Step 2: Add the same filters to the count query**

Replace `COUNT_PERSON_SOURCE_RECORDS` with:

```python
COUNT_PERSON_SOURCE_RECORDS = """
MATCH (sr:SourceRecord)-[:LINKED_TO]->(p:Person {person_id: $person_id})
MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
OPTIONAL MATCH (ss)-[:OPERATED_BY]->(entity:Entity)
WHERE ($entity_key IS NULL OR entity.entity_key = $entity_key)
  AND ($record_type IS NULL OR sr.record_type = $record_type)
RETURN count(sr) AS total
"""
```

- [ ] **Step 3: Add the facets query**

Add a new constant near the other source-record queries in `persons.py`:

```python
GET_PERSON_SOURCE_RECORD_ENTITY_FACETS = """
MATCH (sr:SourceRecord)-[:LINKED_TO]->(:Person {person_id: $person_id})
MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
OPTIONAL MATCH (ss)-[:OPERATED_BY]->(entity:Entity)
RETURN ss.source_key AS source_system,
       entity.entity_key AS entity_key,
       entity.display_name AS entity_display_name,
       count(sr) AS count
ORDER BY source_system, entity_display_name
"""
```

- [ ] **Step 4: Export the new constant**

In `services/api/src/graph/queries/__init__.py`, add `GET_PERSON_SOURCE_RECORD_ENTITY_FACETS` to both the `from .persons import (...)` block (alphabetically near `GET_PERSON_SOURCE_RECORDS`) and the `__all__` list (near `"GET_PERSON_SOURCE_RECORDS"`).

- [ ] **Step 5: Verify import resolves**

Run: `uv run --package profile-unifier-api python -c "from src.graph.queries import GET_PERSON_SOURCE_RECORD_ENTITY_FACETS; print('ok')"`
Expected: prints `ok`.

---

## Task 4: Repository — filters + facets method

**Files:**
- Modify: `services/api/src/repositories/protocols/person.py`
- Modify: `services/api/src/repositories/neo4j/person.py`
- Modify: `services/api/src/graph/mappers.py`

- [ ] **Step 1: Add the facet mapper**

In `services/api/src/graph/mappers.py`, add (near `map_source_record`, and ensure `SourceRecordEntityFacet` is imported from `src.types` and `to_int`/`to_optional_str`/`to_str` are already imported):

```python
def map_source_record_entity_facet(record: GraphRecord) -> SourceRecordEntityFacet:
    return SourceRecordEntityFacet(
        source_system=to_str(record.get("source_system")),
        entity_key=to_optional_str(record.get("entity_key")),
        entity_display_name=to_optional_str(record.get("entity_display_name")),
        count=to_int(record.get("count")),
    )
```

- [ ] **Step 2: Update the protocol**

In `services/api/src/repositories/protocols/person.py`:
- Add `SourceRecordEntityFacet` to the `from src.types import (...)` block.
- Replace the `get_source_records` signature and add the facets method:

```python
    async def get_source_records(
        self,
        person_id: str,
        skip: int,
        limit: int,
        entity_key: str | None = None,
        record_type: str | None = None,
    ) -> tuple[list[SourceRecord], int]: ...

    async def get_source_record_entity_facets(
        self, person_id: str
    ) -> list[SourceRecordEntityFacet]: ...
```

- [ ] **Step 3: Update the Neo4j implementation**

In `services/api/src/repositories/neo4j/person.py`:
- Add `GET_PERSON_SOURCE_RECORD_ENTITY_FACETS` to the queries import and `map_source_record_entity_facet` to the mappers import; add `SourceRecordEntityFacet` to the `src.types` import.
- Replace the `get_source_records` method with:

```python
    async def get_source_records(
        self,
        person_id: str,
        skip: int,
        limit: int,
        entity_key: str | None = None,
        record_type: str | None = None,
    ) -> tuple[list[SourceRecord], int]:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_SOURCE_RECORDS,
                person_id=person_id,
                skip=skip,
                limit=limit + 1,
                entity_key=entity_key,
                record_type=record_type,
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
            count_result = await session.run(
                COUNT_PERSON_SOURCE_RECORDS,
                person_id=person_id,
                entity_key=entity_key,
                record_type=record_type,
            )
            count_record = await count_result.single()
        return [map_source_record(rec) for rec in records[:limit]], to_total(count_record)

    async def get_source_record_entity_facets(
        self, person_id: str
    ) -> list[SourceRecordEntityFacet]:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_SOURCE_RECORD_ENTITY_FACETS, person_id=person_id
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        return [map_source_record_entity_facet(rec) for rec in records]
```

- [ ] **Step 4: Type-check the changed backend modules**

Run: `uv run --package profile-unifier-api mypy --strict services/api/src`
Expected: no new errors.

- [ ] **Step 5: Commit**

```bash
git add services/api/src/graph services/api/src/repositories
git commit -m "feat(api): filter source records by entity/type and add entity facets repo"
```

---

## Task 5: Routes — `SourceRecordView` mapping, filters, facets endpoint (+ tests)

**Files:**
- Modify: `services/api/src/routes/persons.py`
- Test: `services/api/tests/test_source_records_tab.py`

- [ ] **Step 1: Write failing endpoint tests**

Mirror the existing person-route test style in `services/api/tests/`. Use the repo-override fixture pattern already used by sibling tests (inspect an existing `services/api/tests/test_*person*.py` for the exact `app.dependency_overrides[get_person_repo]` helper; replicate it here).

```python
# services/api/tests/test_source_records_tab.py
"""Tests for the source-records tab endpoints: filters, facets, display fields."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from src.app import app
from src.repositories.deps import get_person_repo
from src.types import SourceRecord, SourceRecordEntityFacet


class _FakeRepo:
    def __init__(self) -> None:
        self.last_entity_key: str | None = None
        self.last_record_type: str | None = None

    async def get_source_records(
        self,
        person_id: str,
        skip: int,
        limit: int,
        entity_key: str | None = None,
        record_type: str | None = None,
    ) -> tuple[list[SourceRecord], int]:
        self.last_entity_key = entity_key
        self.last_record_type = record_type
        rec = SourceRecord(
            source_record_pk="pk1",
            source_system="eko_phppos",
            source_record_id="8841",
            record_type="conversation",
            extraction_confidence=0.82,
            link_status="linked",
            observed_at="2026-04-02T03:14:00Z",
            ingested_at="2026-04-02T03:14:00Z",
        )
        return [rec], 1

    async def get_source_record_entity_facets(
        self, person_id: str
    ) -> list[SourceRecordEntityFacet]:
        return [
            SourceRecordEntityFacet(
                source_system="eko_phppos",
                entity_key="eko",
                entity_display_name="EKO Sports",
                count=2,
            )
        ]


@pytest.fixture()
def fake_repo() -> _FakeRepo:
    repo = _FakeRepo()
    app.dependency_overrides[get_person_repo] = lambda: repo
    yield repo
    app.dependency_overrides.pop(get_person_repo, None)


@pytest.mark.asyncio
async def test_source_records_returns_display_fields(fake_repo: _FakeRepo) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/app/v2/persons/p1/source-records")
    assert resp.status_code == 200
    item = resp.json()["data"][0]
    assert item["observed_at_display"] == "02 Apr 2026, 03:14 AM"
    assert item["ingested_at_display"] == "02 Apr 2026, 03:14 AM"
    assert item["extraction_confidence_display"] == "82%"


@pytest.mark.asyncio
async def test_source_records_forwards_filters(fake_repo: _FakeRepo) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/app/v2/persons/p1/source-records?entity_key=eko&record_type=system")
    assert fake_repo.last_entity_key == "eko"
    assert fake_repo.last_record_type == "system"


@pytest.mark.asyncio
async def test_source_record_entities_facets(fake_repo: _FakeRepo) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/app/v2/persons/p1/source-record-entities")
    assert resp.status_code == 200
    facets = resp.json()["data"]
    assert facets[0]["entity_display_name"] == "EKO Sports"
    assert facets[0]["count"] == 2
```

> Note: confirm the mounted path prefix used by sibling tests. If existing tests hit routes via `/persons/...` (app, not the `/app/v2` mount), use that prefix instead — match whatever the existing person-route tests use. Adjust the auth override the same way sibling tests do.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest services/api/tests/test_source_records_tab.py -v`
Expected: FAIL — `observed_at_display` missing (KeyError) and the facets route 404s.

- [ ] **Step 3: Add the view mapper + filters + facets route**

In `services/api/src/routes/persons.py`:
- Add a new import: `from src.display_format import format_confidence_pct, format_display_datetime` (only these two are used — both timestamps format with `format_display_datetime`; do not import `format_display_date` here or it will trip the unused-import lint).
- Add `SourceRecordView` and `SourceRecordEntityFacet` to the existing `src.types` import.

Add a mapping helper (near `_source_record_display_items`):

```python
def _to_source_record_view(item: SourceRecord) -> SourceRecordView:
    return SourceRecordView(
        **item.model_dump(),
        observed_at_display=format_display_datetime(item.observed_at),
        ingested_at_display=format_display_datetime(item.ingested_at),
        extraction_confidence_display=format_confidence_pct(item.extraction_confidence),
    )
```

Replace the `get_person_source_records` endpoint with the filtered + mapped version:

```python
@router.get("/{person_id}/source-records", response_model=ApiResponse[list[SourceRecordView]])
async def get_person_source_records(
    person_id: str,
    request: Request,
    entity_key: str | None = Query(default=None),
    record_type: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[SourceRecordView]]:
    """List source records linked to a person (optionally filtered by entity/type)."""
    skip, page_limit = page_window(cursor, limit)
    items, total = await repo.get_source_records(
        person_id, skip, page_limit, entity_key=entity_key, record_type=record_type
    )
    views = [_to_source_record_view(item) for item in items]
    has_more = skip + page_limit < total
    resp = envelope(views, request, next_cursor(skip, page_limit, has_more), total_count=total)
    resp.display_items = _source_record_display_items(items)
    return resp
```

Add the facets endpoint immediately after it:

```python
@router.get(
    "/{person_id}/source-record-entities",
    response_model=ApiResponse[list[SourceRecordEntityFacet]],
)
async def get_person_source_record_entities(
    person_id: str,
    request: Request,
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[SourceRecordEntityFacet]]:
    """Per-entity source-record counts for a person (for filter chips)."""
    facets = await repo.get_source_record_entity_facets(person_id)
    return envelope(facets, request)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest services/api/tests/test_source_records_tab.py -v`
Expected: PASS (3 tests). If the path prefix differs, fix the test URLs per the Step 1 note.

- [ ] **Step 5: Full API test + lint + type-check**

Run: `uv run pytest services/api/tests && uv run --package profile-unifier-api ruff check services/api/src && uv run --package profile-unifier-api mypy --strict services/api/src`
Expected: all pass; no new mypy errors.

- [ ] **Step 6: Commit**

```bash
git add services/api/src/routes/persons.py services/api/tests/test_source_records_tab.py
git commit -m "feat(api): source-records filters, display view, and entity facets endpoint"
```

---

## Task 6: Frontend types

**Files:**
- Modify: `services/frontend2/src/lib/api-types-person.ts`

- [ ] **Step 1: Extend `PersonSourceRecord` and add the facet type**

Add the three display fields, `raw_payload`, and `conversation_ref` to `PersonSourceRecord` (after `normalized_payload`):

```typescript
  normalized_payload: SourceRecordNormalizedPayload | null;
  raw_payload: Record<string, unknown> | null;
  conversation_ref: Record<string, unknown> | null;
  observed_at_display: string;
  ingested_at_display: string;
  extraction_confidence_display: string | null;
```

> `raw_payload`/`conversation_ref` are free-form source JSON with no fixed schema, so `Record<string, unknown>` is the honest type here; the UI only pretty-prints them via `JSON.stringify`. This is the one sanctioned place for `unknown`-valued records — values are never accessed by key, only serialized.

Add the facet interface at the end of the file:

```typescript
export interface SourceRecordEntityFacet {
  source_system: string;
  entity_key: string | null;
  entity_display_name: string | null;
  count: number;
}
```

- [ ] **Step 2: Type-check**

Run: `cd services/frontend2 && npm run typecheck`
Expected: PASS (no errors). New `unknown`-valued record types are allowed; we never index them.

---

## Task 7: BFF facets route

**Files:**
- Create: `services/frontend2/src/app/bff/persons/[personId]/source-record-entities/route.ts`

- [ ] **Step 1: Create the thin proxy route**

```typescript
import type { NextResponse } from "next/server";

import type { SourceRecordEntityFacet } from "@/lib/api-types-person";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ personId: string }>;
}

export async function GET(_request: Request, context: RouteContext): Promise<NextResponse> {
  const { personId } = await context.params;
  return proxyToApi<SourceRecordEntityFacet[]>(
    `/persons/${encodeURIComponent(personId)}/source-record-entities`,
  );
}
```

- [ ] **Step 2: Type-check**

Run: `cd services/frontend2 && npm run typecheck`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add services/frontend2/src/lib/api-types-person.ts services/frontend2/src/app/bff/persons/[personId]/source-record-entities/route.ts
git commit -m "feat(frontend2): source-record display types and entity-facets BFF route"
```

---

## Task 8: `SourceRecordsTab` component + page wiring

**Files:**
- Modify: `services/frontend2/src/app/persons/[personId]/page.tsx`

All edits are in `page.tsx`. The component is defined in-file alongside the other tabs (established pattern) and reuses the in-file helpers `titleCase`, `TabEmptyState`, `TabPagination`, `SkeletonRows`.

- [ ] **Step 1: Import the facet type**

In the `@/lib/api-types-person` import block at the top, add `SourceRecordEntityFacet` to the imported names.

- [ ] **Step 2: Extend the `Tab` union and `DetailData`**

Change the `Tab` type (line ~30) to include the new tab:

```typescript
type Tab = "sales" | "connections" | "identifiers" | "source-records" | "matches" | "timeline" | "graph";
```

Add facets to `DetailData` (line ~32):

```typescript
type DetailData = {
  identifiers: PersonIdentifier[];
  sourceRecords: PersonSourceRecord[];
  sales: SalesOrder[];
  audit: PersonAuditEvent[];
  bankruptcyCases: PersonBankruptcyCase[];
  sourceRecordFacets: SourceRecordEntityFacet[];
};
```

- [ ] **Step 3: Initialize facets in the empty-detail constant**

Find the `EMPTY_DETAIL`/initial detail object (the one with `sourceRecords: []`, ~line 2189) and add `sourceRecordFacets: [],` to it.

- [ ] **Step 4: Fetch facets up front in the initial `Promise.all`**

In the `load()` effect (~line 2397), add a facets fetch to the `Promise.all` array and destructuring. Append to the array:

```typescript
          bffFetchEnvelope<SourceRecordEntityFacet[]>(`/bff/persons/${encodeURIComponent(personId)}/source-record-entities`).catch(catchNotFound),
```

Add `facetsEnv` to the destructured tuple (append at the end, matching order):

```typescript
        const [idEnv, srcEnv, salesEnv, auditEnv, matchesEnv, connsEnv, bkEnv, sharedEnv, facetsEnv] = await Promise.all([
```

In `setDetailData({...})`, add:

```typescript
          sourceRecordFacets: facetsEnv?.data ?? [],
```

- [ ] **Step 5: Add the `SourceRecordsTab` component**

Add this component near the other tab components (e.g. just before `IdentifiersTab`, ~line 1747):

```typescript
function srEntityLabel(facet: SourceRecordEntityFacet): string {
  return facet.entity_display_name ?? facet.entity_key ?? titleCase(facet.source_system);
}

function SourceRecordRow({ record }: { record: PersonSourceRecord }): ReactElement {
  const [open, setOpen] = useState(false);
  const [rawOpen, setRawOpen] = useState(false);
  const entity = record.entity_display_name ?? record.entity_key ?? "Unknown entity";
  const payload = record.normalized_payload;
  const identifiers = payload?.identifiers ?? [];
  const attributes = payload?.attributes ?? [];
  const address = payload?.address?.normalized_full ?? null;

  const meta: Array<[string, string]> = [
    ["Source system", titleCase(record.source_system)],
    ["Entity", entity],
    ["Record ID", record.source_record_id],
    ["Version", record.source_record_version ?? "—"],
    ["Type", titleCase(record.record_type)],
    ["Link status", titleCase(record.link_status)],
    ["Observed", record.observed_at_display || "—"],
    ["Ingested", record.ingested_at_display || "—"],
    ["Record PK", record.source_record_pk],
  ];
  if (record.record_type === "conversation") {
    if (record.extraction_method) meta.push(["Extraction", titleCase(record.extraction_method)]);
    if (record.extraction_confidence_display) meta.push(["Confidence", record.extraction_confidence_display]);
  }

  return (
    <div className={`${styles.srcRow} ${open ? styles.srcRowOpen : ""}`}>
      <div
        className={styles.srcMain}
        onClick={() => setOpen((v) => !v)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => e.key === "Enter" && setOpen((v) => !v)}
      >
        <div className={styles.idBody}>
          <span className={styles.idValue}>{titleCase(record.source_system)}</span>
          <div className={styles.connMeta}>
            <span>{entity}</span>
            <span className={styles.connMetaSep}>·</span>
            <span>{record.source_record_id}</span>
            {record.source_record_version && (
              <><span className={styles.connMetaSep}>·</span><span>v{record.source_record_version}</span></>
            )}
            <span className={styles.connMetaSep}>·</span>
            <span>{record.observed_at_display || "—"}</span>
          </div>
        </div>
        <div className={styles.idBadges}>
          <span className={record.record_type === "conversation" ? styles.srBadgeConv : styles.srBadgeSys}>{record.record_type}</span>
          <span className={styles.srBadgeLink}>{record.link_status}</span>
          <svg className={`${styles.srcChevron} ${open ? styles.srcChevronOpen : ""}`} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M6 9l6 6 6-6" />
          </svg>
        </div>
      </div>
      {open && (
        <div className={styles.idDetailPanel}>
          <div className={styles.idDetailSection}>
            <div className={styles.idDetailSectionTitle}>Record</div>
            <div className={styles.srMetaGrid}>
              {meta.map(([label, value]) => (
                <div key={label} className={styles.srMetaRow}>
                  <span className={styles.srMetaLabel}>{label}</span>
                  <span className={styles.srMetaValue}>{value}</span>
                </div>
              ))}
            </div>
          </div>

          <div className={styles.idDetailSection}>
            <div className={styles.idDetailSectionTitle}>Normalized payload</div>
            {identifiers.length === 0 && attributes.length === 0 && address === null ? (
              <div className={styles.srMetaValue}>—</div>
            ) : (
              <>
                <div className={styles.srPills}>
                  {identifiers.map((id, i) => (
                    <span key={`id-${i}`} className={styles.srPill}>
                      {titleCase(id.identifier_type ?? "")} · {id.normalized_value ?? "—"}{id.is_verified ? " ✓" : ""}
                    </span>
                  ))}
                </div>
                {address !== null && (
                  <div className={styles.srMetaRow}><span className={styles.srMetaLabel}>Address</span><span className={styles.srMetaValue}>{address}</span></div>
                )}
                {attributes.length > 0 && (
                  <div className={styles.srPills}>
                    {attributes.map((attr, i) => (
                      <span key={`attr-${i}`} className={styles.srPill}>{titleCase(attr.attribute_name ?? "")} · {attr.attribute_value ?? "—"}</span>
                    ))}
                  </div>
                )}
              </>
            )}
          </div>

          {record.raw_payload !== null && (
            <div className={styles.idDetailSection}>
              <button type="button" className={styles.srRawToggle} onClick={() => setRawOpen((v) => !v)} aria-expanded={rawOpen}>
                <svg className={`${styles.srcChevron} ${rawOpen ? styles.srcChevronOpen : ""}`} width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M6 9l6 6 6-6" /></svg>
                Raw payload <span className={styles.srMetaLabel}>(original source JSON)</span>
              </button>
              {rawOpen && <pre className={styles.srJson}>{JSON.stringify(record.raw_payload, null, 2)}</pre>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SourceRecordsTab({ personId, facets, onTotalLoaded }: { personId: string; facets: SourceRecordEntityFacet[]; onTotalLoaded: (n: number) => void }): ReactElement {
  const [activeEntity, setActiveEntity] = useState<string | null>(null);
  const facetTotal = facets.reduce((sum, f) => sum + f.count, 0);
  const basePath = activeEntity === null
    ? `/bff/persons/${encodeURIComponent(personId)}/source-records`
    : `/bff/persons/${encodeURIComponent(personId)}/source-records?entity_key=${encodeURIComponent(activeEntity)}`;
  const { rows, loading, error, from, to, total, hasPrev, hasNext, goNext, goPrev } =
    usePaginatedFetch<PersonSourceRecord>(basePath);
  const records = rows ?? [];

  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { onTotalLoaded(facetTotal); }, [facetTotal, onTotalLoaded]);

  return (
    <section className={styles.contentCard}>
      <div className={styles.connHeader}>
        <span className={styles.connHeaderTitle}>Source records</span>
        <span className={styles.connHeaderDot}>·</span>
        <span className={styles.connHeaderCount}>{facetTotal} {facetTotal === 1 ? "record" : "records"}</span>
      </div>

      {facets.length > 0 && (
        <div className={styles.srFilter}>
          <button type="button" className={`${styles.srChip} ${activeEntity === null ? styles.srChipOn : ""}`} onClick={() => setActiveEntity(null)}>
            All · {facetTotal}
          </button>
          {facets.map((f) => {
            const key = f.entity_key ?? f.source_system;
            return (
              <button
                key={`${f.source_system}:${key}`}
                type="button"
                className={`${styles.srChip} ${activeEntity === f.entity_key ? styles.srChipOn : ""}`}
                onClick={() => setActiveEntity(f.entity_key)}
                disabled={f.entity_key === null}
                title={f.entity_key === null ? "No entity key — cannot filter" : undefined}
              >
                {srEntityLabel(f)} · {f.count}
              </button>
            );
          })}
        </div>
      )}

      {loading ? (
        <SkeletonRows />
      ) : error ? (
        <div className={styles.tabError}>{error}</div>
      ) : records.length === 0 ? (
        <TabEmptyState message="No source records on file." />
      ) : (
        <div className={styles.idList}>
          {records.map((record) => (
            <SourceRecordRow key={record.source_record_pk} record={record} />
          ))}
        </div>
      )}

      {(hasPrev || hasNext) && <TabPagination from={from} to={to} total={total} hasPrev={hasPrev} hasNext={hasNext} onPrev={goPrev} onNext={goNext} />}
    </section>
  );
}
```

- [ ] **Step 6: Add the tab config entry, total callback, and render branch**

In the `tabs: TabConfig[]` array (~line 2453), add the entry after `identifiers`:

```typescript
    { id: "source-records", label: "Source records", count: tabTotals.sourceRecords },
```

Add `sourceRecords` to the `tabTotals` state type/initialization (find the `setTabTotals` initial state and the `useState` type — add `sourceRecords?: number`). Add a callback near `onSalesTotal` (~line 2441):

```typescript
  const onSourceRecordsTotal = useCallback((n: number) => { setTabTotals((p) => ({ ...p, sourceRecords: n })); }, []);
```

Add the render branch in the tab-content switch (~line 2474, after `identifiers`):

```typescript
        {activeTab === "source-records" && shell(<SourceRecordsTab personId={personId} facets={detailData.sourceRecordFacets} onTotalLoaded={onSourceRecordsTotal} />)}
```

- [ ] **Step 7: Type-check**

Run: `cd services/frontend2 && npm run typecheck`
Expected: PASS. If `tabTotals` is a typed object, ensure `sourceRecords?: number` is part of its type.

---

## Task 9: CSS for chips, metadata grid, pills, raw payload

**Files:**
- Modify: `services/frontend2/src/app/persons/[personId]/person.module.css`

- [ ] **Step 1: Append the new style rules**

Add at the end of the file (tune colors to match existing tokens already used in this file — reuse `var(--border)`, `var(--accent)`, `var(--text-muted)`, etc., which appear elsewhere in this stylesheet):

```css
/* ── Source Records tab ── */
.srFilter { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
.srChip {
  font-size: 12px; padding: 4px 11px; border-radius: 14px;
  border: 1px solid var(--border); background: transparent;
  color: var(--text-muted); cursor: pointer; transition: all 0.12s ease;
}
.srChip:hover:not(:disabled) { color: var(--text); border-color: var(--text-muted); }
.srChip:disabled { opacity: 0.5; cursor: default; }
.srChipOn { border-color: var(--accent); color: var(--accent); background: color-mix(in srgb, var(--accent) 8%, transparent); }

.srMetaGrid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 4px 20px; }
.srMetaRow { display: flex; gap: 10px; font-size: 12.5px; padding: 2px 0; }
.srMetaLabel { color: var(--text-muted); flex: none; min-width: 96px; }
.srMetaValue { color: var(--text); word-break: break-word; }

.srPills { display: flex; flex-wrap: wrap; gap: 6px; margin: 4px 0; }
.srPill {
  font-size: 11.5px; padding: 3px 9px; border-radius: 6px;
  background: color-mix(in srgb, var(--text-muted) 12%, transparent);
  color: var(--text);
}

.srBadgeSys, .srBadgeConv, .srBadgeLink {
  font-size: 10px; padding: 1px 7px; border-radius: 10px;
  border: 1px solid currentColor; white-space: nowrap; text-transform: capitalize;
}
.srBadgeSys { color: var(--accent); }
.srBadgeConv { color: #b070d8; }
.srBadgeLink { color: var(--text-muted); }

.srRawToggle {
  display: flex; align-items: center; gap: 7px; width: 100%;
  background: none; border: none; padding: 4px 0; cursor: pointer;
  color: var(--text); font-size: 12.5px; text-align: left;
}
.srJson {
  margin: 6px 0 0; padding: 10px 12px; border-radius: 8px;
  background: color-mix(in srgb, var(--text-muted) 10%, transparent);
  color: var(--text); font-size: 11px; line-height: 1.5;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  white-space: pre; overflow-x: auto; max-height: 320px;
}
```

> If `color-mix` is not already used in this stylesheet, replace those backgrounds with an existing token/rgba pattern already present in the file (grep the file for `rgba(` to copy the established approach). Verify `--accent`, `--text`, `--text-muted`, `--border` exist in the theme (grep `globals.css`/theme); if a token name differs, use the file's actual names.

- [ ] **Step 2: Verify chevron/skeleton/error classes exist**

Confirm `styles.srcRow`, `styles.srcRowOpen`, `styles.srcMain`, `styles.srcChevron`, `styles.srcChevronOpen`, `styles.idDetailPanel`, `styles.idDetailSection`, `styles.idDetailSectionTitle`, `styles.idList`, `styles.idBody`, `styles.idValue`, `styles.idBadges`, `styles.connMeta`, `styles.connMetaSep`, `styles.connHeader`, `styles.tabError` are all already defined in `person.module.css` (they are used by Sales/Identifiers tabs). Run: `cd services/frontend2 && npx grep -l srcChevron src/app/persons/[personId]/person.module.css` or simply search the file. If any is missing, reuse the closest existing class.

- [ ] **Step 3: Lint + build the frontend**

Run: `cd services/frontend2 && npm run typecheck && npm run lint`
Expected: typecheck clean; lint within the `--max-warnings 9` budget (the new `eslint-disable-next-line react-hooks/set-state-in-effect` keeps it in budget).

- [ ] **Step 4: Commit**

```bash
git add services/frontend2/src/app/persons/[personId]/page.tsx services/frontend2/src/app/persons/[personId]/person.module.css
git commit -m "feat(frontend2): Source Records tab with entity filter and raw-payload drill-down"
```

---

## Task 10: End-to-end verification

**Files:** none (verification only)

- [ ] **Step 1: Rebuild and run the affected containers**

Run: `docker compose build --no-cache api frontend2 && docker compose up -d api frontend2`
Expected: both build and start cleanly.

- [ ] **Step 2: Smoke-test the endpoints**

With a known `person_id` that has source records (find one via the persons list), exercise the API through the running stack:

Run: `curl -s "http://localhost/api/app/v2/persons/<PERSON_ID>/source-record-entities"` (note: this requires auth in the running stack; if a bearer token is inconvenient, instead verify via the browser BFF in Step 3).
Expected: a JSON envelope with `data` = array of facets.

- [ ] **Step 3: Manual UI check**

Open a person detail page in the browser (the running `web` service at `http://localhost/persons/<PERSON_ID>`), confirm:
- The "Source records" tab appears with a count badge on initial load (before clicking it).
- Clicking it shows the entity filter chips and the record list.
- Selecting an entity chip filters the list and pagination resets.
- Expanding a record shows the metadata grid + normalized payload; the "Raw payload" toggle reveals pretty-printed JSON.
- Dates read like "02 Apr 2026, 03:14 AM" and conversation records show a confidence percentage — with no client-side formatting code involved.

- [ ] **Step 4: Final full check**

Run: `uv run pytest services/api/tests && uv run --package profile-unifier-api mypy --strict services/api/src && cd services/frontend2 && npm run typecheck && npm run lint`
Expected: all green.

---

## Notes for the executor

- **Commit policy:** the repository owner requires explicit instruction before committing. The commit steps are written for completeness, but do **not** run them unless the owner has said to commit. Otherwise leave changes staged/working and report.
- **mypy override:** `**item.model_dump()` in the route mapper relies on `src.routes.*` being in the mypy strict override list (per CLAUDE.md). Keep `_to_source_record_view` in `routes/persons.py`, not in a non-route module.
- **No public-contract change:** do not add display fields to `SourceRecord` or touch `routes/public_pages.py`.
- **docker-compose:** this change touches no compose files, so the `.docker/staging` sync rule does not apply.
