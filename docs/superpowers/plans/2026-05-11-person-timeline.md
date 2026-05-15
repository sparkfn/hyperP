# Person Timeline Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Timeline tab to the person detail page that groups profile facts by source record and orders them by the source record's actual source timestamp, with lazy upward loading and deep links from Profile source records.

**Architecture:** Add a server-authoritative timeline endpoint on the existing person repository path. The backend maps each linked `SourceRecord` into a timeline group with displayable fact items derived from normalized payload, returns cursor pages newest-first, and supports target lookup by source record id. The frontend renders the data as a bottom-anchored rail, prepends older pages on upward scroll, and uses URL query parameters for tab/target jumps.

**Tech Stack:** FastAPI, Pydantic, Neo4j Cypher repository layer, Next.js App Router BFF, React 19, MUI, TypeScript strict mode.

---

## File structure

- Modify `services/api/src/types.py` — add `TimelineTimestampKind`, `TimelineFact`, `PersonTimelineGroup`, and `PersonTimelineWindow` response models.
- Modify `services/api/src/graph/mappers.py` — add source-record-to-timeline mapper helpers.
- Modify `services/api/src/graph/queries/persons.py` — add timeline page/count/target Cypher constants.
- Modify `services/api/src/graph/queries/__init__.py` — export new query constants.
- Modify `services/api/src/repositories/protocols/person.py` — add timeline methods to `PersonRepository`.
- Modify `services/api/src/repositories/neo4j/person.py` — implement timeline methods using the new queries and mapper.
- Modify `services/api/src/routes/persons.py` — expose `GET /v1/persons/{person_id}/timeline` and `GET /v1/persons/{person_id}/timeline/target`.
- Create `services/api/tests/test_person_timeline.py` — unit-test timeline mapping, timestamp fallback, and route pagination with a fake repository.
- Modify `services/frontend/src/lib/api-types-person.ts` — add timeline TypeScript interfaces.
- Create `services/frontend/src/app/bff/persons/[personId]/timeline/route.ts` — proxy timeline pages to FastAPI.
- Create `services/frontend/src/app/bff/persons/[personId]/timeline/target/route.ts` — proxy target lookups to FastAPI.
- Create `services/frontend/src/components/PersonTimelineTab.tsx` — render rail, lazy upward loading, jump controls, target highlight.
- Modify `services/frontend/src/components/SourceRecordsTab.tsx` — add `View in timeline` action to source-record detail dialog.
- Modify `services/frontend/src/components/PersonDetailTabs.tsx` — add Timeline tab, query-param tab/target handling, and pass timeline callbacks into source records.

Do not commit during implementation unless the user explicitly asks for a commit.

---

### Task 1: Backend timeline models and mapper tests

**Files:**
- Modify: `services/api/src/types.py`
- Modify: `services/api/src/graph/mappers.py`
- Test: `services/api/tests/test_person_timeline.py`

- [ ] **Step 1: Write failing mapper tests**

Create `services/api/tests/test_person_timeline.py` with these tests:

```python
from __future__ import annotations

from pydantic.types import JsonValue

from src.graph.mappers import map_timeline_group
from src.types import PersonTimelineGroup


def _record(
    *,
    observed_at: str | None,
    ingested_at: str,
    payload: dict[str, JsonValue] | None,
) -> dict[str, object]:
    return {
        "source_record": {
            "source_record_pk": "sr-1",
            "source_record_id": "external-1",
            "source_record_version": "v1",
            "record_type": "system",
            "extraction_confidence": None,
            "link_status": "linked",
            "observed_at": observed_at,
            "ingested_at": ingested_at,
            "normalized_payload": payload,
        },
        "source_system": "pos",
        "linked_person_id": "person-1",
    }


def test_map_timeline_group_uses_observed_at_as_source_timestamp() -> None:
    group = map_timeline_group(
        _record(
            observed_at="2026-04-28T14:32:00Z",
            ingested_at="2026-05-01T01:00:00Z",
            payload={
                "identifiers": [{"identifier_type": "phone", "normalized_value": "+6591234567"}],
                "address": {"normalized_full": "10 Orchard Road"},
                "attributes": [{"attribute_name": "full_name", "attribute_value": "Ana Tan"}],
                "summary": "Customer asked about renewal.",
            },
        )
    )

    assert isinstance(group, PersonTimelineGroup)
    assert group.source_record_pk == "sr-1"
    assert group.occurred_at == "2026-04-28T14:32:00Z"
    assert group.timestamp_kind == "source"
    assert [(fact.category, fact.label, fact.value) for fact in group.facts] == [
        ("source", "Summary", "Customer asked about renewal."),
        ("identity", "Full name", "Ana Tan"),
        ("contact", "Phone", "+6591234567"),
        ("address", "Address", "10 Orchard Road"),
    ]


def test_map_timeline_group_labels_ingested_at_as_fallback_timestamp() -> None:
    group = map_timeline_group(
        _record(
            observed_at=None,
            ingested_at="2026-05-01T01:00:00Z",
            payload={"identifiers": [{"identifier_type": "email", "normalized_value": "ana@example.com"}]},
        )
    )

    assert group.occurred_at == "2026-05-01T01:00:00Z"
    assert group.timestamp_kind == "fallback"
    assert group.facts[0].category == "contact"
    assert group.facts[0].label == "Email"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest services/api/tests/test_person_timeline.py -v
```

Expected: FAIL because `PersonTimelineGroup` and `map_timeline_group` do not exist.

- [ ] **Step 3: Add API models**

In `services/api/src/types.py`, after `SourceRecord`, add:

```python
TimelineTimestampKind = Literal["source", "fallback"]
TimelineFactCategory = Literal[
    "identity",
    "contact",
    "address",
    "sale",
    "relationship",
    "conversation",
    "source",
]


class TimelineFact(BaseModel):
    fact_id: str
    category: TimelineFactCategory
    label: str
    value: str
    detail: str | None = None


class PersonTimelineGroup(BaseModel):
    source_record_pk: str
    source_system: str
    source_record_id: str
    source_record_version: str | None = None
    record_type: Literal["system", "conversation"] = "system"
    extraction_confidence: float | None = None
    link_status: str
    linked_person_id: str | None = None
    occurred_at: str
    timestamp_kind: TimelineTimestampKind
    ingested_at: str
    facts: list[TimelineFact] = Field(default_factory=list)
```

- [ ] **Step 4: Add mapper helpers**

In `services/api/src/graph/mappers.py`, import `TimelineFact` and `PersonTimelineGroup` from `src.types`, then add these helpers near `map_source_record`:

```python
def _labelize(value: str) -> str:
    return " ".join(part.capitalize() for part in value.split("_") if part)


def _append_summary_fact(facts: list[TimelineFact], payload: dict[str, JsonValue]) -> None:
    summary = payload.get("summary")
    if isinstance(summary, str) and summary.strip():
        facts.append(
            TimelineFact(
                fact_id="summary",
                category="source",
                label="Summary",
                value=summary.strip(),
            )
        )


def _append_attribute_facts(facts: list[TimelineFact], payload: dict[str, JsonValue]) -> None:
    raw_attributes = payload.get("attributes")
    if not isinstance(raw_attributes, list):
        return
    for index, raw in enumerate(raw_attributes):
        item = _json_dict(raw)
        name = _json_str(item.get("attribute_name"))
        value = _json_str(item.get("attribute_value"))
        if name is None or value is None or value == "":
            continue
        category: Literal["identity", "source"] = "identity" if name in {"full_name", "dob"} else "source"
        facts.append(
            TimelineFact(
                fact_id=f"attribute-{index}",
                category=category,
                label=_labelize(name),
                value=value,
                detail=_json_str(item.get("quality_flag")),
            )
        )


def _append_identifier_facts(facts: list[TimelineFact], payload: dict[str, JsonValue]) -> None:
    raw_identifiers = payload.get("identifiers")
    if not isinstance(raw_identifiers, list):
        return
    for index, raw in enumerate(raw_identifiers):
        item = _json_dict(raw)
        identifier_type = _json_str(item.get("identifier_type"))
        normalized_value = _json_str(item.get("normalized_value"))
        if identifier_type is None or normalized_value is None or normalized_value == "":
            continue
        category: Literal["contact", "identity"] = (
            "contact" if identifier_type in {"phone", "email"} else "identity"
        )
        facts.append(
            TimelineFact(
                fact_id=f"identifier-{index}",
                category=category,
                label=_labelize(identifier_type),
                value=normalized_value,
                detail=_json_str(item.get("quality_flag")),
            )
        )


def _append_address_fact(facts: list[TimelineFact], payload: dict[str, JsonValue]) -> None:
    address = _json_dict(payload.get("address"))
    normalized = _json_str(address.get("normalized_full"))
    if normalized is None or normalized == "":
        return
    facts.append(
        TimelineFact(
            fact_id="address",
            category="address",
            label="Address",
            value=normalized,
            detail=_json_str(address.get("quality_flag")),
        )
    )


def _timeline_facts(payload: dict[str, JsonValue] | None) -> list[TimelineFact]:
    if payload is None:
        return []
    facts: list[TimelineFact] = []
    _append_summary_fact(facts, payload)
    _append_attribute_facts(facts, payload)
    _append_identifier_facts(facts, payload)
    _append_address_fact(facts, payload)
    return facts


def map_timeline_group(record: GraphRecord) -> PersonTimelineGroup:
    sr = _as_dict(record.get("source_record"))
    observed_at = to_iso_or_none(sr.get("observed_at"))
    ingested_at = to_iso_or_empty(sr.get("ingested_at"))
    occurred_at = observed_at if observed_at is not None else ingested_at
    return PersonTimelineGroup(
        source_record_pk=to_str(sr.get("source_record_pk")),
        source_system=to_str(record.get("source_system")),
        source_record_id=to_str(sr.get("source_record_id")),
        source_record_version=to_optional_str(sr.get("source_record_version")),
        record_type="conversation" if to_str(sr.get("record_type")) == "conversation" else "system",
        extraction_confidence=(
            to_float(sr.get("extraction_confidence"))
            if sr.get("extraction_confidence") is not None
            else None
        ),
        link_status=to_str(sr.get("link_status")),
        linked_person_id=to_optional_str(record.get("linked_person_id")),
        occurred_at=occurred_at,
        timestamp_kind="source" if observed_at is not None else "fallback",
        ingested_at=ingested_at,
        facts=_timeline_facts(_parse_normalized_payload(sr.get("normalized_payload"))),
    )
```

- [ ] **Step 5: Run mapper tests**

Run:

```bash
uv run pytest services/api/tests/test_person_timeline.py -v
```

Expected: PASS.

---

### Task 2: Backend timeline repository and routes

**Files:**
- Modify: `services/api/src/graph/queries/persons.py`
- Modify: `services/api/src/graph/queries/__init__.py`
- Modify: `services/api/src/repositories/protocols/person.py`
- Modify: `services/api/src/repositories/neo4j/person.py`
- Modify: `services/api/src/routes/persons.py`
- Test: `services/api/tests/test_person_timeline.py`

- [ ] **Step 1: Add failing route tests with fake repository**

Append to `services/api/tests/test_person_timeline.py`:

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.routes.persons import router
from src.repositories.deps import get_person_repo
from src.types import (
    AuditEvent,
    ConnectionType,
    ListedPerson,
    MatchDecision,
    Person,
    PersonConnection,
    PersonEntitySummary,
    PersonGraph,
    PersonIdentifier,
    PersonTimelineGroup,
    SourceRecord,
)


class FakeTimelineRepo:
    async def get_page(self, filters: dict[str, object], skip: int, limit: int) -> tuple[list[ListedPerson], int]:
        return [], 0

    async def search_by_identifier(self, identifier_type: str, value: str) -> list[Person]:
        return []

    async def search_by_query(self, q: str, status: str | None, skip: int, limit: int) -> tuple[list[Person], bool]:
        return [], False

    async def get_by_id(self, person_id: str) -> Person | None:
        return None

    async def get_source_records(self, person_id: str, skip: int, limit: int) -> tuple[list[SourceRecord], int]:
        return [], 0

    async def get_identifiers(self, person_id: str, skip: int, limit: int) -> tuple[list[PersonIdentifier], int]:
        return [], 0

    async def get_connections(
        self,
        person_id: str,
        connection_type: ConnectionType,
        identifier_type: str | None,
        skip: int,
        limit: int,
    ) -> tuple[list[PersonConnection], int]:
        return [], 0

    async def get_entities(self, person_id: str) -> list[PersonEntitySummary]:
        return []

    async def get_graph(self, person_id: str, max_hops: int) -> PersonGraph | None:
        return None

    async def get_node_graph(self, element_id: str, max_hops: int) -> PersonGraph | None:
        return None

    async def get_audit(self, person_id: str, skip: int, limit: int) -> tuple[list[AuditEvent], int]:
        return [], 0

    async def get_matches(self, person_id: str, skip: int, limit: int) -> tuple[list[MatchDecision], bool]:
        return [], False

    async def get_timeline(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[PersonTimelineGroup], int]:
        return [
            PersonTimelineGroup(
                source_record_pk="sr-new",
                source_system="pos",
                source_record_id="external-new",
                record_type="system",
                link_status="linked",
                linked_person_id=person_id,
                occurred_at="2026-04-30T09:10:00Z",
                timestamp_kind="source",
                ingested_at="2026-05-01T01:00:00Z",
                facts=[],
            )
        ], 2

    async def get_timeline_target(
        self, person_id: str, source_record_pk: str
    ) -> PersonTimelineGroup | None:
        if source_record_pk != "sr-new":
            return None
        return PersonTimelineGroup(
            source_record_pk="sr-new",
            source_system="pos",
            source_record_id="external-new",
            record_type="system",
            link_status="linked",
            linked_person_id=person_id,
            occurred_at="2026-04-30T09:10:00Z",
            timestamp_kind="source",
            ingested_at="2026-05-01T01:00:00Z",
            facts=[],
        )


@pytest.fixture
def timeline_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_person_repo] = lambda: FakeTimelineRepo()
    return app


@asynccontextmanager
async def timeline_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.mark.anyio
async def test_get_person_timeline_returns_envelope(timeline_app: FastAPI) -> None:
    async with timeline_client(timeline_app) as client:
        response = await client.get("/v1/persons/person-1/timeline?limit=1")

    assert response.status_code == 200
    body = response.json()
    assert body["data"][0]["source_record_pk"] == "sr-new"
    assert body["meta"]["total_count"] == 2
    assert body["meta"]["next_cursor"] is not None


@pytest.mark.anyio
async def test_get_person_timeline_target_returns_one_group(timeline_app: FastAPI) -> None:
    async with timeline_client(timeline_app) as client:
        response = await client.get(
            "/v1/persons/person-1/timeline/target?source_record_pk=sr-new"
        )

    assert response.status_code == 200
    assert response.json()["data"]["source_record_pk"] == "sr-new"


@pytest.mark.anyio
async def test_get_person_timeline_target_404s_for_missing_record(timeline_app: FastAPI) -> None:
    async with timeline_client(timeline_app) as client:
        response = await client.get(
            "/v1/persons/person-1/timeline/target?source_record_pk=missing"
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "source_record_not_found"
```

- [ ] **Step 2: Run tests to verify route failures**

Run:

```bash
uv run pytest services/api/tests/test_person_timeline.py -v
```

Expected: FAIL because the route and protocol methods do not exist.

- [ ] **Step 3: Add query constants**

In `services/api/src/graph/queries/persons.py`, after `GET_PERSON_SOURCE_RECORDS`, add:

```python
GET_PERSON_TIMELINE = """
MATCH (sr:SourceRecord)-[:LINKED_TO]->(p:Person {person_id: $person_id})
MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
RETURN sr {
  .source_record_pk, .source_record_id, .source_record_version,
  .record_type, .extraction_confidence,
  .link_status, .observed_at, .ingested_at, .normalized_payload
} AS source_record,
ss.source_key AS source_system,
p.person_id AS linked_person_id
ORDER BY coalesce(sr.observed_at, sr.ingested_at) DESC, sr.source_record_pk DESC
SKIP $skip LIMIT $limit
"""

COUNT_PERSON_TIMELINE = """
MATCH (sr:SourceRecord)-[:LINKED_TO]->(:Person {person_id: $person_id})
RETURN count(sr) AS total
"""

GET_PERSON_TIMELINE_TARGET = """
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})-[:LINKED_TO]->(p:Person {person_id: $person_id})
MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
RETURN sr {
  .source_record_pk, .source_record_id, .source_record_version,
  .record_type, .extraction_confidence,
  .link_status, .observed_at, .ingested_at, .normalized_payload
} AS source_record,
ss.source_key AS source_system,
p.person_id AS linked_person_id
"""
```

Export those three names in `services/api/src/graph/queries/__init__.py` imports and `__all__`.

- [ ] **Step 4: Extend repository protocol**

In `services/api/src/repositories/protocols/person.py`, import `PersonTimelineGroup` and add these methods to `PersonRepository`:

```python
    async def get_timeline(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[PersonTimelineGroup], int]: ...

    async def get_timeline_target(
        self, person_id: str, source_record_pk: str
    ) -> PersonTimelineGroup | None: ...
```

- [ ] **Step 5: Implement Neo4j repository methods**

In `services/api/src/repositories/neo4j/person.py`, import `map_timeline_group`, `PersonTimelineGroup`, and the new query constants. Add methods to `Neo4jPersonRepository`:

```python
    async def get_timeline(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[PersonTimelineGroup], int]:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_TIMELINE, person_id=person_id, skip=skip, limit=limit + 1
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
            count_result = await session.run(COUNT_PERSON_TIMELINE, person_id=person_id)
            count_record = await count_result.single()
        return [map_timeline_group(rec) for rec in records[:limit]], to_total(count_record)

    async def get_timeline_target(
        self, person_id: str, source_record_pk: str
    ) -> PersonTimelineGroup | None:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_TIMELINE_TARGET,
                person_id=person_id,
                source_record_pk=source_record_pk,
            )
            record = await result.single()
        if record is None:
            return None
        return map_timeline_group(record_to_dict(record.keys(), list(record.values())))
```

- [ ] **Step 6: Add route handlers**

In `services/api/src/routes/persons.py`, import `PersonTimelineGroup` and add these handlers after `get_person_source_records`:

```python
@router.get("/{person_id}/timeline", response_model=ApiResponse[list[PersonTimelineGroup]])
async def get_person_timeline(
    person_id: str,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[PersonTimelineGroup]]:
    """List source-record timeline groups for a person, newest source timestamp first."""
    skip, page_limit = page_window(cursor, limit)
    items, total = await repo.get_timeline(person_id, skip, page_limit)
    has_more = skip + page_limit < total
    return envelope(items, request, next_cursor(skip, page_limit, has_more), total_count=total)


@router.get("/{person_id}/timeline/target", response_model=ApiResponse[PersonTimelineGroup])
async def get_person_timeline_target(
    person_id: str,
    request: Request,
    source_record_pk: str = Query(),
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[PersonTimelineGroup]:
    """Return one source-record timeline group for deep-link jumps."""
    item = await repo.get_timeline_target(person_id, source_record_pk)
    if item is None:
        raise http_error(404, "source_record_not_found", "Source record not found.", request)
    return envelope(item, request)
```

- [ ] **Step 7: Run backend timeline tests**

Run:

```bash
uv run pytest services/api/tests/test_person_timeline.py -v
```

Expected: PASS.

---

### Task 3: Frontend API types and BFF routes

**Files:**
- Modify: `services/frontend/src/lib/api-types-person.ts`
- Create: `services/frontend/src/app/bff/persons/[personId]/timeline/route.ts`
- Create: `services/frontend/src/app/bff/persons/[personId]/timeline/target/route.ts`

- [ ] **Step 1: Add frontend timeline types**

In `services/frontend/src/lib/api-types-person.ts`, after `PersonSourceRecord`, add:

```typescript
export type TimelineTimestampKind = "source" | "fallback";
export type TimelineFactCategory =
  | "identity"
  | "contact"
  | "address"
  | "sale"
  | "relationship"
  | "conversation"
  | "source";

export interface PersonTimelineFact {
  fact_id: string;
  category: TimelineFactCategory;
  label: string;
  value: string;
  detail: string | null;
}

export interface PersonTimelineGroup {
  source_record_pk: string;
  source_system: string;
  source_record_id: string;
  source_record_version: string | null;
  record_type: SourceRecordType;
  extraction_confidence: number | null;
  link_status: string;
  linked_person_id: string | null;
  occurred_at: string;
  timestamp_kind: TimelineTimestampKind;
  ingested_at: string;
  facts: PersonTimelineFact[];
}
```

- [ ] **Step 2: Add BFF timeline page route**

Create `services/frontend/src/app/bff/persons/[personId]/timeline/route.ts`:

```typescript
import type { NextResponse } from "next/server";

import type { PersonTimelineGroup } from "@/lib/api-types-person";
import { proxyToApi, searchParamsToQuery } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ personId: string }>;
}

export async function GET(request: Request, context: RouteContext): Promise<NextResponse> {
  const { personId } = await context.params;
  const { searchParams } = new URL(request.url);
  return proxyToApi<PersonTimelineGroup[]>(
    `/persons/${encodeURIComponent(personId)}/timeline`,
    { query: searchParamsToQuery(searchParams) },
  );
}
```

- [ ] **Step 3: Add BFF target route**

Create `services/frontend/src/app/bff/persons/[personId]/timeline/target/route.ts`:

```typescript
import type { NextResponse } from "next/server";

import type { PersonTimelineGroup } from "@/lib/api-types-person";
import { proxyToApi, searchParamsToQuery } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ personId: string }>;
}

export async function GET(request: Request, context: RouteContext): Promise<NextResponse> {
  const { personId } = await context.params;
  const { searchParams } = new URL(request.url);
  return proxyToApi<PersonTimelineGroup>(
    `/persons/${encodeURIComponent(personId)}/timeline/target`,
    { query: searchParamsToQuery(searchParams) },
  );
}
```

- [ ] **Step 4: Run frontend typecheck**

Run:

```bash
cd services/frontend && npm run typecheck
```

Expected: PASS.

---

### Task 4: Timeline tab component

**Files:**
- Create: `services/frontend/src/components/PersonTimelineTab.tsx`

- [ ] **Step 1: Create timeline component**

Create `services/frontend/src/components/PersonTimelineTab.tsx`:

```typescript
"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from "react";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Divider from "@mui/material/Divider";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import { BffError, bffFetchEnvelope } from "@/lib/api-client";
import type { ApiResponse } from "@/lib/api-types";
import type { PersonTimelineFact, PersonTimelineGroup } from "@/lib/api-types-person";

const PAGE_SIZE = 10;

interface Props {
  personId: string;
  targetSourceRecordPk: string | null;
  targetTimestamp: string | null;
  onTargetConsumed: () => void;
}

export default function PersonTimelineTab({
  personId,
  targetSourceRecordPk,
  targetTimestamp,
  onTargetConsumed,
}: Props): ReactElement {
  const [groups, setGroups] = useState<PersonTimelineGroup[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loadingInitial, setLoadingInitial] = useState<boolean>(true);
  const [loadingOlder, setLoadingOlder] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [jumpDate, setJumpDate] = useState<string>("");
  const [jumpRecordPk, setJumpRecordPk] = useState<string>("");
  const [highlightedPk, setHighlightedPk] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const cardRefs = useRef<Map<string, HTMLDivElement>>(new Map());

  const timelinePath = `/bff/persons/${encodeURIComponent(personId)}/timeline`;

  const loadPage = useCallback(
    async (cursor: string | null): Promise<ApiResponse<PersonTimelineGroup[]>> => {
      const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
      if (cursor !== null) params.set("cursor", cursor);
      return bffFetchEnvelope<PersonTimelineGroup[]>(`${timelinePath}?${params.toString()}`);
    },
    [timelinePath],
  );

  useEffect(() => {
    let cancelled = false;
    setLoadingInitial(true);
    setError(null);
    setGroups([]);
    const run = async (): Promise<void> => {
      try {
        const envelope = await loadPage(null);
        if (cancelled) return;
        setGroups(envelope.data);
        setNextCursor(envelope.meta.next_cursor);
        requestAnimationFrame(() => {
          const el = scrollRef.current;
          if (el !== null) el.scrollTop = el.scrollHeight;
        });
      } catch (err: unknown) {
        if (!cancelled) setError(err instanceof BffError ? err.message : "Failed to load timeline.");
      } finally {
        if (!cancelled) setLoadingInitial(false);
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [loadPage]);

  const loadOlder = useCallback(async (): Promise<void> => {
    if (nextCursor === null || loadingOlder) return;
    const el = scrollRef.current;
    const previousHeight = el?.scrollHeight ?? 0;
    setLoadingOlder(true);
    try {
      const envelope = await loadPage(nextCursor);
      setGroups((current) => [...current, ...envelope.data]);
      setNextCursor(envelope.meta.next_cursor);
      requestAnimationFrame(() => {
        const currentEl = scrollRef.current;
        if (currentEl !== null) {
          currentEl.scrollTop = currentEl.scrollHeight - previousHeight + currentEl.scrollTop;
        }
      });
    } catch (err: unknown) {
      setError(err instanceof BffError ? err.message : "Failed to load older timeline records.");
    } finally {
      setLoadingOlder(false);
    }
  }, [loadPage, loadingOlder, nextCursor]);

  const handleScroll = useCallback((): void => {
    const el = scrollRef.current;
    if (el === null || el.scrollTop > 80) return;
    void loadOlder();
  }, [loadOlder]);

  const scrollToSourceRecord = useCallback((sourceRecordPk: string): boolean => {
    const card = cardRefs.current.get(sourceRecordPk);
    if (card === undefined) return false;
    card.scrollIntoView({ block: "center", behavior: "smooth" });
    setHighlightedPk(sourceRecordPk);
    window.setTimeout(() => setHighlightedPk(null), 1800);
    return true;
  }, []);

  const fetchTarget = useCallback(
    async (sourceRecordPk: string): Promise<void> => {
      const params = new URLSearchParams({ source_record_pk: sourceRecordPk });
      const envelope = await bffFetchEnvelope<PersonTimelineGroup>(
        `${timelinePath}/target?${params.toString()}`,
      );
      setGroups((current) => {
        if (current.some((group) => group.source_record_pk === envelope.data.source_record_pk)) {
          return current;
        }
        return [...current, envelope.data].sort((left, right) =>
          right.occurred_at.localeCompare(left.occurred_at),
        );
      });
      requestAnimationFrame(() => scrollToSourceRecord(envelope.data.source_record_pk));
    },
    [scrollToSourceRecord, timelinePath],
  );

  useEffect(() => {
    if (targetSourceRecordPk === null || loadingInitial) return;
    if (!scrollToSourceRecord(targetSourceRecordPk)) {
      void fetchTarget(targetSourceRecordPk).catch((err: unknown) => {
        setError(err instanceof BffError ? err.message : "Could not find that source record.");
      });
    }
    onTargetConsumed();
  }, [fetchTarget, loadingInitial, onTargetConsumed, scrollToSourceRecord, targetSourceRecordPk]);

  useEffect(() => {
    if (targetTimestamp === null || loadingInitial) return;
    const targetTime = Date.parse(targetTimestamp);
    if (Number.isNaN(targetTime)) return;
    const closest = groups.reduce<PersonTimelineGroup | null>((best, group) => {
      const groupTime = Date.parse(group.occurred_at);
      if (Number.isNaN(groupTime)) return best;
      if (best === null) return group;
      return Math.abs(groupTime - targetTime) < Math.abs(Date.parse(best.occurred_at) - targetTime)
        ? group
        : best;
    }, null);
    if (closest !== null) scrollToSourceRecord(closest.source_record_pk);
    onTargetConsumed();
  }, [groups, loadingInitial, onTargetConsumed, scrollToSourceRecord, targetTimestamp]);

  const sourceRecordOptions = useMemo(
    () => groups.map((group) => ({ label: `${group.source_system} • ${group.source_record_id}`, value: group.source_record_pk })),
    [groups],
  );

  function handleJumpToDate(): void {
    if (jumpDate.trim() === "") return;
    const params = new URLSearchParams(window.location.search);
    params.set("tab", "timeline");
    params.set("timelineAt", new Date(jumpDate).toISOString());
    window.history.replaceState(null, "", `${window.location.pathname}?${params.toString()}`);
    const event = new CustomEvent("timeline-target-change", { detail: { timestamp: new Date(jumpDate).toISOString() } });
    window.dispatchEvent(event);
  }

  function handleJumpToRecord(): void {
    if (jumpRecordPk === "") return;
    void fetchTarget(jumpRecordPk).catch((err: unknown) => {
      setError(err instanceof BffError ? err.message : "Could not find that source record.");
    });
  }

  if (loadingInitial) {
    return (
      <Box sx={{ display: "flex", justifyContent: "center", py: 4 }}>
        <CircularProgress size={24} />
      </Box>
    );
  }

  return (
    <Stack spacing={2}>
      {error !== null ? <Alert severity="error">{error}</Alert> : null}
      <TimelineJumpControls
        jumpDate={jumpDate}
        jumpRecordPk={jumpRecordPk}
        sourceRecordOptions={sourceRecordOptions}
        onJumpDateChange={setJumpDate}
        onJumpRecordChange={setJumpRecordPk}
        onJumpToDate={handleJumpToDate}
        onJumpToRecord={handleJumpToRecord}
      />
      {groups.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          No source-record facts are available for this person.
        </Typography>
      ) : (
        <Box
          ref={scrollRef}
          onScroll={handleScroll}
          sx={{
            border: 1,
            borderColor: "divider",
            borderRadius: 2,
            height: 640,
            overflowY: "auto",
            p: 2,
            bgcolor: "grey.50",
          }}
        >
          {loadingOlder ? (
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", textAlign: "center", mb: 1 }}>
              Loading older records…
            </Typography>
          ) : null}
          <Stack spacing={1.5} sx={{ flexDirection: "column-reverse" }}>
            {groups.map((group) => (
              <TimelineCard
                key={group.source_record_pk}
                group={group}
                highlighted={highlightedPk === group.source_record_pk}
                setRef={(node) => {
                  if (node === null) cardRefs.current.delete(group.source_record_pk);
                  else cardRefs.current.set(group.source_record_pk, node);
                }}
              />
            ))}
          </Stack>
        </Box>
      )}
    </Stack>
  );
}

interface TimelineJumpControlsProps {
  jumpDate: string;
  jumpRecordPk: string;
  sourceRecordOptions: { label: string; value: string }[];
  onJumpDateChange: (value: string) => void;
  onJumpRecordChange: (value: string) => void;
  onJumpToDate: () => void;
  onJumpToRecord: () => void;
}

function TimelineJumpControls({
  jumpDate,
  jumpRecordPk,
  sourceRecordOptions,
  onJumpDateChange,
  onJumpRecordChange,
  onJumpToDate,
  onJumpToRecord,
}: TimelineJumpControlsProps): ReactElement {
  return (
    <Paper variant="outlined" sx={{ p: 2 }}>
      <Stack direction={{ xs: "column", md: "row" }} spacing={1.5} alignItems={{ md: "center" }}>
        <TextField
          label="Jump to date/time"
          type="datetime-local"
          value={jumpDate}
          onChange={(event) => onJumpDateChange(event.target.value)}
          size="small"
          slotProps={{ inputLabel: { shrink: true } }}
        />
        <Button variant="outlined" onClick={onJumpToDate}>Jump</Button>
        <Divider flexItem orientation="vertical" />
        <TextField
          select
          label="Jump to loaded source record"
          value={jumpRecordPk}
          onChange={(event) => onJumpRecordChange(event.target.value)}
          size="small"
          sx={{ minWidth: 260 }}
        >
          {sourceRecordOptions.map((option) => (
            <MenuItem key={option.value} value={option.value}>{option.label}</MenuItem>
          ))}
        </TextField>
        <Button variant="outlined" onClick={onJumpToRecord}>Open card</Button>
      </Stack>
    </Paper>
  );
}

function TimelineCard({
  group,
  highlighted,
  setRef,
}: {
  group: PersonTimelineGroup;
  highlighted: boolean;
  setRef: (node: HTMLDivElement | null) => void;
}): ReactElement {
  return (
    <Box ref={setRef} sx={{ display: "grid", gridTemplateColumns: "32px 1fr", columnGap: 1.5 }}>
      <Box sx={{ position: "relative", display: "flex", justifyContent: "center" }}>
        <Box sx={{ position: "absolute", top: 0, bottom: 0, width: 2, bgcolor: "divider" }} />
        <Box sx={{ mt: 2, width: 14, height: 14, borderRadius: "50%", bgcolor: "primary.main", zIndex: 1 }} />
      </Box>
      <Paper
        variant="outlined"
        sx={{
          p: 1.5,
          transition: "background-color 180ms ease, border-color 180ms ease",
          bgcolor: highlighted ? "primary.50" : "background.paper",
          borderColor: highlighted ? "primary.main" : "divider",
        }}
      >
        <Stack spacing={1}>
          <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap" alignItems="center">
            <Chip label={group.source_system} size="small" />
            <Chip label={group.record_type} size="small" color={group.record_type === "conversation" ? "warning" : "default"} />
            <Chip label={group.timestamp_kind === "source" ? "Source timestamp" : "Fallback timestamp"} size="small" variant="outlined" />
          </Stack>
          <Box>
            <Typography variant="subtitle2">{group.source_record_id}</Typography>
            <Typography variant="caption" color="text.secondary">{new Date(group.occurred_at).toLocaleString()}</Typography>
          </Box>
          {group.facts.length === 0 ? (
            <Typography variant="body2" color="text.secondary">No normalized facts stored for this source record.</Typography>
          ) : (
            <Stack spacing={0.75}>
              {group.facts.map((fact) => <TimelineFactRow key={fact.fact_id} fact={fact} />)}
            </Stack>
          )}
        </Stack>
      </Paper>
    </Box>
  );
}

function TimelineFactRow({ fact }: { fact: PersonTimelineFact }): ReactElement {
  return (
    <Stack direction="row" spacing={1} alignItems="baseline">
      <Chip label={fact.category} size="small" variant="outlined" sx={{ minWidth: 88 }} />
      <Box>
        <Typography variant="body2"><strong>{fact.label}:</strong> {fact.value}</Typography>
        {fact.detail !== null ? <Typography variant="caption" color="text.secondary">{fact.detail}</Typography> : null}
      </Box>
    </Stack>
  );
}
```

- [ ] **Step 2: Run frontend typecheck and fix narrow errors**

Run:

```bash
cd services/frontend && npm run typecheck
```

Expected: PASS. If the `primary.50` color key fails typing, replace it with `action.hover`.

---

### Task 5: Wire Timeline tab and Profile source-record jumps

**Files:**
- Modify: `services/frontend/src/components/PersonDetailTabs.tsx`
- Modify: `services/frontend/src/components/SourceRecordsTab.tsx`

- [ ] **Step 1: Modify `PersonDetailTabs.tsx` imports and state**

Add imports:

```typescript
import { useCallback, useEffect, useState, type ReactElement, type SyntheticEvent } from "react";
import PersonTimelineTab from "./PersonTimelineTab";
```

Replace tab state with:

```typescript
const [tab, setTab] = useState<number>(0);
const [timelineTargetPk, setTimelineTargetPk] = useState<string | null>(null);
const [timelineTargetAt, setTimelineTargetAt] = useState<string | null>(null);
```

Add helpers inside `PersonDetailTabs`:

```typescript
const openTimelineTarget = useCallback((sourceRecordPk: string): void => {
  const params = new URLSearchParams(window.location.search);
  params.set("tab", "timeline");
  params.set("sourceRecordPk", sourceRecordPk);
  window.history.replaceState(null, "", `${window.location.pathname}?${params.toString()}`);
  setTimelineTargetPk(sourceRecordPk);
  setTimelineTargetAt(null);
  setTab(1);
}, []);

useEffect(() => {
  const params = new URLSearchParams(window.location.search);
  if (params.get("tab") === "timeline") {
    setTab(1);
    setTimelineTargetPk(params.get("sourceRecordPk"));
    setTimelineTargetAt(params.get("timelineAt"));
  }
  const listener = (event: Event): void => {
    const custom = event as CustomEvent<{ timestamp?: string }>;
    if (typeof custom.detail.timestamp === "string") {
      setTimelineTargetAt(custom.detail.timestamp);
      setTimelineTargetPk(null);
      setTab(1);
    }
  };
  window.addEventListener("timeline-target-change", listener);
  return () => window.removeEventListener("timeline-target-change", listener);
}, []);
```

Update `handleChange`:

```typescript
const handleChange = (_e: SyntheticEvent, value: number): void => {
  setTab(value);
  const params = new URLSearchParams(window.location.search);
  if (value === 1) params.set("tab", "timeline");
  else params.delete("tab");
  window.history.replaceState(null, "", `${window.location.pathname}?${params.toString()}`);
};
```

- [ ] **Step 2: Modify tab labels and rendering**

Update tabs:

```typescript
<Tab label="Profile" />
<Tab label="Timeline" />
<Tab label="Matches" />
```

Update source records and tab panels:

```typescript
<SourceRecordsTab personId={person.person_id} onViewInTimeline={openTimelineTarget} />
```

```typescript
{tab === 1 ? (
  <PersonTimelineTab
    personId={person.person_id}
    targetSourceRecordPk={timelineTargetPk}
    targetTimestamp={timelineTargetAt}
    onTargetConsumed={() => {
      setTimelineTargetPk(null);
      setTimelineTargetAt(null);
    }}
  />
) : null}
{tab === 2 ? <MatchesTab personId={person.person_id} /> : null}
```

- [ ] **Step 3: Modify `SourceRecordsTab.tsx` props and dialog action**

Change props:

```typescript
interface Props {
  personId: string;
  onViewInTimeline: (sourceRecordPk: string) => void;
}

export default function SourceRecordsTab({ personId, onViewInTimeline }: Props): ReactElement {
```

Pass the callback into the dialog:

```typescript
<RecordPayloadDialog
  record={selectedRecord}
  onClose={() => setSelectedRecord(null)}
  onViewInTimeline={onViewInTimeline}
/>
```

Update dialog props:

```typescript
function RecordPayloadDialog({
  record,
  onClose,
  onViewInTimeline,
}: {
  record: PersonSourceRecord | null;
  onClose: () => void;
  onViewInTimeline: (sourceRecordPk: string) => void;
}): ReactElement {
```

Add the action before Close:

```typescript
{record !== null ? (
  <Button
    onClick={() => {
      onViewInTimeline(record.source_record_pk);
      onClose();
    }}
  >
    View in timeline
  </Button>
) : null}
<Button onClick={onClose}>Close</Button>
```

- [ ] **Step 4: Run frontend checks**

Run:

```bash
cd services/frontend && npm run typecheck
cd services/frontend && npm run lint
```

Expected: both PASS. `npm run lint` must not exceed the existing warning budget.

---

### Task 6: Full verification

**Files:**
- Verify changed backend and frontend files only.

- [ ] **Step 1: Run backend tests**

Run:

```bash
uv run pytest services/api/tests/test_person_timeline.py -v
```

Expected: PASS.

- [ ] **Step 2: Run API lint/type checks**

Run:

```bash
uv run --package profile-unifier-api ruff check services/api/src services/api/tests/test_person_timeline.py
uv run --package profile-unifier-api mypy --strict services/api/src
```

Expected: ruff PASS. Mypy should not introduce new failures; if existing project failures outside changed files appear, record them and verify changed timeline files are not the source.

- [ ] **Step 3: Run frontend checks**

Run:

```bash
cd services/frontend && npm run typecheck
cd services/frontend && npm run lint
```

Expected: both PASS.

- [ ] **Step 4: Manual UI verification**

Run:

```bash
docker compose build --no-cache api frontend
docker compose up -d api frontend web
```

Open the app through nginx and verify:

1. Person page has `Profile | Timeline | Matches` tabs.
2. Timeline tab initially shows newest source-record cards at the bottom.
3. Scrolling upward lazy-loads older source-record cards.
4. Cards show source/fallback timestamp labels.
5. Date/datetime jump scrolls to the closest loaded card.
6. Source-record selector opens and highlights a card.
7. Profile tab source-record detail dialog `View in timeline` switches to Timeline and highlights that source-record card.

- [ ] **Step 5: Report verification only after all checks complete**

Report changed files, verification commands, and any failures. Do not commit unless the user explicitly asks.
