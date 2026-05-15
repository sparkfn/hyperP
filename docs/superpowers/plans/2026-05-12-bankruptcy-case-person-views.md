# BankruptcyCase Person Views Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose `BankruptcyCase` data on the person profile, person graph, person data timeline, and person listing with a presence filter.

**Architecture:** Add a first-class typed read model for `BankruptcyCase` in the API repository layer, then proxy it through the existing Next.js BFF pattern. Reuse existing pagination/count-card UI primitives so the new bankruptcy views behave like Sales, Source Records, Entities, and other person sections.

**Tech Stack:** FastAPI, Neo4j/Cypher, Pydantic, strict mypy, Next.js App Router, TypeScript strict mode, MUI, existing `usePaginatedFetch` and `CountCardsCell` components.

---

## Design reference

Spec: `docs/superpowers/specs/2026-05-12-bankruptcy-case-person-views-design.md`

Do not commit during implementation unless the user explicitly asks. The project memory says commits require explicit user instruction.

---

## File structure

### Backend
- Modify `services/api/src/types.py`
  - Add `BankruptcyCase` Pydantic model.
  - Add timeline category literal `bankruptcy`.
  - Add `bankruptcy_case_count` to `ListedPerson`.
- Modify `services/api/src/repositories/protocols/person.py`
  - Import `BankruptcyCase`.
  - Add `has_bankruptcy_case` filter.
  - Add `get_bankruptcy_cases()` protocol method.
- Modify `services/api/src/graph/queries/persons.py`
  - Add `GET_PERSON_BANKRUPTCY_CASES` and `COUNT_PERSON_BANKRUPTCY_CASES`.
  - Enrich timeline query with described bankruptcy case data.
- Modify `services/api/src/graph/queries/persons_list.py`
  - Add `has_bankruptcy_case` presence filter.
  - Add `bankruptcy_case_count` enrichment and sortable column.
- Modify `services/api/src/graph/queries/__init__.py`
  - Export new bankruptcy query constants.
- Modify `services/api/src/graph/mappers.py`
  - Add `map_bankruptcy_case()`.
  - Add bankruptcy timeline facts from source payload and/or case node.
- Modify `services/api/src/graph/mappers_entities.py`
  - Map `bankruptcy_case_count` into `ListedPerson`.
- Modify `services/api/src/repositories/neo4j/person.py`
  - Import query constants and mapper.
  - Pass `has_bankruptcy_case` into list query params.
  - Implement `get_bankruptcy_cases()`.
- Modify `services/api/src/routes/persons.py`
  - Import `BankruptcyCase`.
  - Parse `has_bankruptcy_case` query param on `GET /v1/persons`.
  - Add `GET /v1/persons/{person_id}/bankruptcy-cases`.
- Test `services/api/tests/test_person_bankruptcy_cases.py`
  - New focused tests for mapper and endpoint.
- Modify `services/api/tests/test_person_timeline.py`
  - Add mapper test for bankruptcy timeline facts.

### Frontend
- Modify `services/frontend/src/lib/api-types.ts`
  - Add `bankruptcy_case_count` to `ListedPerson`.
- Modify `services/frontend/src/lib/api-types-person.ts`
  - Add `PersonBankruptcyCase` interface.
  - Add `bankruptcy` to `TimelineFactCategory`.
- Create `services/frontend/src/app/bff/persons/[personId]/bankruptcy-cases/route.ts`
  - Thin proxy to API.
- Create `services/frontend/src/components/BankruptcyCasesCard.tsx`
  - Paginated profile section after sales.
- Modify `services/frontend/src/components/PersonDetailTabs.tsx`
  - Render `BankruptcyCasesCard` immediately after `SalesCard`.
- Modify `services/frontend/src/components/PersonsFilterPanel.tsx`
  - Add `has_bankruptcy_case` to filters and tri-state select.
- Modify `services/frontend/src/app/persons/query.ts`
  - URL parse/build support for `has_bankruptcy_case` and sort field `bankruptcy_case_count`.
- Modify `services/frontend/src/app/persons/page.tsx`
  - No structural change expected beyond types flowing through filters.
- Modify `services/frontend/src/components/PersonsListTable.tsx`
  - Add sortable Bankruptcy column and lazy fetch cache.
- Modify `services/frontend/src/components/PersonRow.tsx`
  - Render bankruptcy count-card popover.
- Modify `services/frontend/src/components/graph-utils.ts`
  - Add BankruptcyCase color/icon and display name handling.
- Modify `services/frontend/src/components/GraphDetailPanel.tsx`
  - Ensure case fields are readable if current generic rendering is insufficient.
- Modify `services/frontend/src/components/PersonTimelineTab.tsx`
  - Add visual styling for bankruptcy timeline facts if category-specific styling exists.

---

## Task 1: Backend type model and repository contract

**Files:**
- Modify: `services/api/src/types.py`
- Modify: `services/api/src/repositories/protocols/person.py`

- [ ] **Step 1: Add backend domain type**

In `services/api/src/types.py`, add this model near `SourceRecord`:

```python
class BankruptcyCase(BaseModel):
    bankruptcy_case_id: str
    source_system_key: str
    source_case_id: str
    case_number: str | None = None
    document_type: str | None = None
    document_date: str | None = None
    event_type: str | None = None
    event_date: str | None = None
    trustee_name: str | None = None
    trustee_firm: str | None = None
    source_url: str | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
```

Update `TimelineFactCategory`:

```python
TimelineFactCategory = Literal[
    "identity",
    "contact",
    "address",
    "sale",
    "relationship",
    "conversation",
    "source",
    "bankruptcy",
]
```

Update `ListedPerson`:

```python
class ListedPerson(EntityPerson):
    """Person row in the /v1/persons listing, with inline entity memberships."""

    entities: list[PersonEntitySummary] = Field(default_factory=list)
    entity_count: int = 0
    identifier_count: int = 0
    order_count: int = 0
    bankruptcy_case_count: int = 0
```

- [ ] **Step 2: Update repository protocol**

In `services/api/src/repositories/protocols/person.py`, add `BankruptcyCase` to imports from `src.types`.

Add the filter field:

```python
class PersonListFilters(TypedDict, total=False):
    q: str | None
    entity_keys: list[str] | None
    source_keys: list[str] | None
    is_high_value: bool | None
    is_high_risk: bool | None
    has_phone: bool | None
    has_email: bool | None
    has_address: bool | None
    has_bankruptcy_case: bool | None
    addr_street: str | None
    addr_unit: str | None
    addr_city: str | None
    addr_postal: str | None
    addr_country: str | None
    updated_after: str | None
    updated_before: str | None
    has_dob: bool | None
    dob_from: str | None
    dob_to: str | None
    sort_by: str | None
    sort_order: str | None
```

Add protocol method after `get_source_records()`:

```python
    async def get_bankruptcy_cases(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[BankruptcyCase], int]: ...
```

- [ ] **Step 3: Run type import smoke check**

Run:

```bash
uv run --package profile-unifier-api ruff check services/api/src/types.py services/api/src/repositories/protocols/person.py
```

Expected: exit 0 or only pre-existing unrelated warnings. Fix any import-order or formatting issues in these touched files.

---

## Task 2: Backend Cypher queries and mappers

**Files:**
- Modify: `services/api/src/graph/queries/persons.py`
- Modify: `services/api/src/graph/queries/persons_list.py`
- Modify: `services/api/src/graph/queries/__init__.py`
- Modify: `services/api/src/graph/mappers.py`
- Modify: `services/api/src/graph/mappers_entities.py`
- Test: `services/api/tests/test_person_bankruptcy_cases.py`
- Test: `services/api/tests/test_person_timeline.py`

- [ ] **Step 1: Write mapper tests first**

Create `services/api/tests/test_person_bankruptcy_cases.py` with:

```python
from __future__ import annotations

from src.graph.mappers import map_bankruptcy_case
from src.graph.mappers_entities import map_listed_person
from src.types import BankruptcyCase, ListedPerson


def test_map_bankruptcy_case_maps_relevant_fields() -> None:
    case = map_bankruptcy_case(
        {
            "bankruptcy_case": {
                "bankruptcy_case_id": "bc-1",
                "source_system_key": "sgbankruptcy",
                "source_case_id": "case-123",
                "case_number": "B 123/2026",
                "document_type": "Bankruptcy Order",
                "document_date": "2026-04-01",
                "event_type": "order_made",
                "event_date": "2026-04-02",
                "trustee_name": "Jane Trustee",
                "trustee_firm": "Trustee LLP",
                "source_url": "https://example.test/case",
                "first_seen_at": "2026-04-03T00:00:00Z",
                "last_seen_at": "2026-04-04T00:00:00Z",
                "created_at": "2026-04-03T00:00:00Z",
                "updated_at": "2026-04-04T00:00:00Z",
            }
        }
    )

    assert isinstance(case, BankruptcyCase)
    assert case.bankruptcy_case_id == "bc-1"
    assert case.source_system_key == "sgbankruptcy"
    assert case.case_number == "B 123/2026"
    assert case.event_type == "order_made"
    assert case.trustee_name == "Jane Trustee"


def test_map_listed_person_includes_bankruptcy_case_count() -> None:
    person = map_listed_person(
        {
            "person": {
                "person_id": "person-1",
                "status": "active",
                "is_high_value": False,
                "is_high_risk": True,
                "preferred_full_name": "Ana Tan",
                "preferred_phone": None,
                "preferred_email": None,
                "preferred_dob": None,
                "preferred_nric": None,
                "profile_completeness_score": 0.2,
                "golden_profile_computed_at": None,
                "golden_profile_version": 1,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-02T00:00:00Z",
            },
            "preferred_address": None,
            "source_record_count": 1,
            "connection_count": 0,
            "phone_confidence": None,
            "entities": [],
            "entity_count": 0,
            "identifier_count": 0,
            "order_count": 0,
            "bankruptcy_case_count": 2,
        }
    )

    assert isinstance(person, ListedPerson)
    assert person.bankruptcy_case_count == 2
```

- [ ] **Step 2: Run mapper tests to verify failure**

Run:

```bash
uv run pytest services/api/tests/test_person_bankruptcy_cases.py -q
```

Expected: FAIL because `BankruptcyCase` / `map_bankruptcy_case` / `bankruptcy_case_count` mapping is not implemented yet.

- [ ] **Step 3: Add person bankruptcy case queries**

In `services/api/src/graph/queries/persons.py`, add after source record queries:

```python
GET_PERSON_BANKRUPTCY_CASES = """
MATCH (:Person {person_id: $person_id})-[:HAS_BANKRUPTCY_CASE]->(bc:BankruptcyCase)
RETURN bc {
  .bankruptcy_case_id, .source_system_key, .source_case_id,
  .case_number, .document_type, .document_date,
  .event_type, .event_date, .trustee_name, .trustee_firm,
  .source_url, .first_seen_at, .last_seen_at, .created_at, .updated_at
} AS bankruptcy_case
ORDER BY coalesce(bc.event_date, bc.document_date, toString(bc.last_seen_at), toString(bc.updated_at), bc.source_case_id) DESC
SKIP $skip LIMIT $limit
"""

COUNT_PERSON_BANKRUPTCY_CASES = """
MATCH (:Person {person_id: $person_id})-[:HAS_BANKRUPTCY_CASE]->(bc:BankruptcyCase)
RETURN count(bc) AS total
"""
```

Update `GET_PERSON_TIMELINE` and `GET_PERSON_TIMELINE_TARGET` to optionally return case data:

```cypher
OPTIONAL MATCH (sr)-[:DESCRIBES_CASE]->(bc:BankruptcyCase)
RETURN sr { ... } AS source_record,
ss.source_key AS source_system,
p.person_id AS linked_person_id,
bc {
  .bankruptcy_case_id, .source_system_key, .source_case_id,
  .case_number, .document_type, .document_date,
  .event_type, .event_date, .trustee_name, .trustee_firm,
  .source_url, .first_seen_at, .last_seen_at, .created_at, .updated_at
} AS bankruptcy_case
```

Keep the existing `ORDER BY`, `SKIP`, and `LIMIT` lines.

- [ ] **Step 4: Export queries**

In `services/api/src/graph/queries/__init__.py`, add `COUNT_PERSON_BANKRUPTCY_CASES` and `GET_PERSON_BANKRUPTCY_CASES` to the import/export list from `persons.py`.

- [ ] **Step 5: Add list query count/filter/sort**

In `services/api/src/graph/queries/persons_list.py`, update `_COMMON_FILTER_CLAUSE`:

```cypher
  AND ($has_bankruptcy_case IS NULL
       OR ($has_bankruptcy_case = true AND EXISTS { (p)-[:HAS_BANKRUPTCY_CASE]->(:BankruptcyCase) })
       OR ($has_bankruptcy_case = false AND NOT EXISTS { (p)-[:HAS_BANKRUPTCY_CASE]->(:BankruptcyCase) }))
```

Add to `_ENRICH_AND_RETURN` before the final `RETURN`:

```cypher
CALL {
  WITH p
  RETURN count{ (p)-[:HAS_BANKRUPTCY_CASE]->(:BankruptcyCase) } AS bankruptcy_case_count
}
```

Add `bankruptcy_case_count` to the returned scalar list:

```cypher
source_record_count, connection_count, phone_confidence, entities,
size(entities) AS entity_count, identifier_count, order_count, bankruptcy_case_count, score
```

Add to `_SORT_COLUMNS`:

```python
"bankruptcy_case_count": "bankruptcy_case_count",
```

- [ ] **Step 6: Implement backend mappers**

In `services/api/src/graph/mappers.py`, import `BankruptcyCase` from `src.types`.

Add:

```python
def map_bankruptcy_case(record: GraphRecord) -> BankruptcyCase:
    bc = _as_dict(record.get("bankruptcy_case"))
    return BankruptcyCase(
        bankruptcy_case_id=to_str(bc.get("bankruptcy_case_id")),
        source_system_key=to_str(bc.get("source_system_key")),
        source_case_id=to_str(bc.get("source_case_id")),
        case_number=to_optional_str(bc.get("case_number")),
        document_type=to_optional_str(bc.get("document_type")),
        document_date=to_iso_or_none(bc.get("document_date")) or to_optional_str(bc.get("document_date")),
        event_type=to_optional_str(bc.get("event_type")),
        event_date=to_iso_or_none(bc.get("event_date")) or to_optional_str(bc.get("event_date")),
        trustee_name=to_optional_str(bc.get("trustee_name")),
        trustee_firm=to_optional_str(bc.get("trustee_firm")),
        source_url=to_optional_str(bc.get("source_url")),
        first_seen_at=to_iso_or_none(bc.get("first_seen_at")),
        last_seen_at=to_iso_or_none(bc.get("last_seen_at")),
        created_at=to_iso_or_none(bc.get("created_at")),
        updated_at=to_iso_or_none(bc.get("updated_at")),
    )
```

Add helper functions near timeline fact helpers:

```python
def _append_bankruptcy_case_facts(facts: list[TimelineFact], value: GraphValue) -> None:
    case = _as_dict(value)
    case_number = to_optional_str(case.get("case_number")) or to_optional_str(case.get("source_case_id"))
    if case_number is not None:
        facts.append(TimelineFact(fact_id="bankruptcy_case", category="bankruptcy", label="Bankruptcy case", value=case_number))
    event_type = to_optional_str(case.get("event_type"))
    event_date = to_iso_or_none(case.get("event_date")) or to_optional_str(case.get("event_date"))
    if event_type is not None:
        facts.append(TimelineFact(fact_id="bankruptcy_event", category="bankruptcy", label="Bankruptcy event", value=event_type, detail=event_date))
    document_type = to_optional_str(case.get("document_type"))
    document_date = to_iso_or_none(case.get("document_date")) or to_optional_str(case.get("document_date"))
    if document_type is not None:
        facts.append(TimelineFact(fact_id="bankruptcy_document", category="bankruptcy", label="Bankruptcy document", value=document_type, detail=document_date))
    trustee_name = to_optional_str(case.get("trustee_name"))
    trustee_firm = to_optional_str(case.get("trustee_firm"))
    if trustee_name is not None:
        facts.append(TimelineFact(fact_id="bankruptcy_trustee", category="bankruptcy", label="Trustee", value=trustee_name, detail=trustee_firm))
```

Update `map_timeline_group()`:

```python
    facts = _timeline_facts(_parse_normalized_payload(sr.get("normalized_payload")))
    _append_bankruptcy_case_facts(facts, record.get("bankruptcy_case"))
    return PersonTimelineGroup(
        ...,
        facts=facts,
    )
```

- [ ] **Step 7: Map list count**

In `services/api/src/graph/mappers_entities.py`, update `map_listed_person()`:

```python
    return ListedPerson(
        **ep.model_dump(),
        entities=entities,
        entity_count=to_int(record.get("entity_count", len(entities))),
        identifier_count=to_int(record.get("identifier_count")),
        order_count=to_int(record.get("order_count")),
        bankruptcy_case_count=to_int(record.get("bankruptcy_case_count")),
    )
```

- [ ] **Step 8: Add timeline mapper test**

In `services/api/tests/test_person_timeline.py`, add:

```python
def test_map_timeline_group_includes_bankruptcy_case_facts() -> None:
    group = map_timeline_group(
        {
            "source_record": {
                "source_record_pk": "sr-bc",
                "source_record_id": "external-bc",
                "source_record_version": "v1",
                "record_type": "system",
                "extraction_confidence": None,
                "link_status": "linked",
                "observed_at": "2026-04-28T14:32:00Z",
                "ingested_at": "2026-05-01T01:00:00Z",
                "normalized_payload": {},
            },
            "source_system": "sgbankruptcy",
            "linked_person_id": "person-1",
            "bankruptcy_case": {
                "bankruptcy_case_id": "bc-1",
                "source_system_key": "sgbankruptcy",
                "source_case_id": "case-123",
                "case_number": "B 123/2026",
                "event_type": "order_made",
                "event_date": "2026-04-02",
                "document_type": "Bankruptcy Order",
                "document_date": "2026-04-01",
                "trustee_name": "Jane Trustee",
                "trustee_firm": "Trustee LLP",
            },
        }
    )

    assert [(fact.category, fact.label, fact.value, fact.detail) for fact in group.facts] == [
        ("bankruptcy", "Bankruptcy case", "B 123/2026", None),
        ("bankruptcy", "Bankruptcy event", "order_made", "2026-04-02"),
        ("bankruptcy", "Bankruptcy document", "Bankruptcy Order", "2026-04-01"),
        ("bankruptcy", "Trustee", "Jane Trustee", "Trustee LLP"),
    ]
```

- [ ] **Step 9: Run focused mapper tests**

Run:

```bash
uv run pytest services/api/tests/test_person_bankruptcy_cases.py services/api/tests/test_person_timeline.py -q
```

Expected: PASS.

---

## Task 3: Backend repository and routes

**Files:**
- Modify: `services/api/src/repositories/neo4j/person.py`
- Modify: `services/api/src/routes/persons.py`
- Test: `services/api/tests/test_person_bankruptcy_cases.py`

- [ ] **Step 1: Add route test before implementation**

Append to `services/api/tests/test_person_bankruptcy_cases.py`:

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from src.auth.deps import get_current_user_or_oauth_client, require_active_user
from src.auth.models import AuthUser
from src.repositories.deps import get_person_repo
from src.routes.persons import router
from src.types import AuditEvent, ConnectionType, MatchDecision, Person, PersonConnection, PersonEntitySummary, PersonGraph, PersonIdentifier, PersonTimelineGroup, SourceRecord


class FakeBankruptcyRepo:
    async def get_page(self, filters, skip: int, limit: int):
        return [], 0

    async def search_by_identifier(self, identifier_type: str, value: str) -> list[Person]:
        return []

    async def search_by_query(self, q: str, status: str | None, skip: int, limit: int):
        return [], False

    async def get_by_id(self, person_id: str) -> Person | None:
        return None

    async def get_source_records(self, person_id: str, skip: int, limit: int) -> tuple[list[SourceRecord], int]:
        return [], 0

    async def get_bankruptcy_cases(self, person_id: str, skip: int, limit: int):
        return [
            BankruptcyCase(
                bankruptcy_case_id="bc-1",
                source_system_key="sgbankruptcy",
                source_case_id="case-123",
                case_number="B 123/2026",
                event_type="order_made",
            )
        ], 1

    async def get_identifiers(self, person_id: str, skip: int, limit: int) -> tuple[list[PersonIdentifier], int]:
        return [], 0

    async def get_connections(self, person_id: str, connection_type: ConnectionType, identifier_type: str | None, skip: int, limit: int) -> tuple[list[PersonConnection], int]:
        return [], 0

    async def get_entities(self, person_id: str) -> list[PersonEntitySummary]:
        return []

    async def get_graph(self, person_id: str, max_hops: int) -> PersonGraph | None:
        return PersonGraph()

    async def get_node_graph(self, element_id: str, max_hops: int) -> PersonGraph | None:
        return PersonGraph()

    async def get_audit(self, person_id: str, skip: int, limit: int) -> tuple[list[AuditEvent], int]:
        return [], 0

    async def get_matches(self, person_id: str, skip: int, limit: int) -> tuple[list[MatchDecision], bool]:
        return [], False

    async def get_timeline(self, person_id: str, skip: int, limit: int) -> tuple[list[PersonTimelineGroup], int]:
        return [], 0

    async def get_timeline_target(self, person_id: str, source_record_pk: str) -> PersonTimelineGroup | None:
        return None


async def _bankruptcy_user() -> AuthUser:
    return AuthUser(email="person@example.com", google_sub="employee-sub", role="employee", entity_key="fundbox", display_name="Person")


@pytest.fixture
def bankruptcy_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_person_repo] = lambda: FakeBankruptcyRepo()
    app.dependency_overrides[require_active_user] = _bankruptcy_user
    app.dependency_overrides[get_current_user_or_oauth_client] = _bankruptcy_user
    return app


@asynccontextmanager
async def bankruptcy_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.mark.anyio
async def test_get_person_bankruptcy_cases_returns_envelope(bankruptcy_app: FastAPI) -> None:
    async with bankruptcy_client(bankruptcy_app) as client:
        response = await client.get("/v1/persons/person-1/bankruptcy-cases?limit=1")

    assert response.status_code == 200
    body = response.json()
    assert body["data"][0]["bankruptcy_case_id"] == "bc-1"
    assert body["data"][0]["case_number"] == "B 123/2026"
    assert body["meta"]["total_count"] == 1
```

- [ ] **Step 2: Run route test to verify failure**

Run:

```bash
uv run pytest services/api/tests/test_person_bankruptcy_cases.py::test_get_person_bankruptcy_cases_returns_envelope -q
```

Expected: FAIL with 404 because route is not implemented.

- [ ] **Step 3: Implement Neo4j repository method**

In `services/api/src/repositories/neo4j/person.py`, import `map_bankruptcy_case`, `COUNT_PERSON_BANKRUPTCY_CASES`, `GET_PERSON_BANKRUPTCY_CASES`, and `BankruptcyCase`.

Update `get_page()` params to include:

```python
has_bankruptcy_case=filters.get("has_bankruptcy_case"),
```

Add after `get_source_records()`:

```python
    async def get_bankruptcy_cases(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[BankruptcyCase], int]:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_BANKRUPTCY_CASES,
                person_id=person_id,
                skip=skip,
                limit=limit + 1,
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
            count_result = await session.run(COUNT_PERSON_BANKRUPTCY_CASES, person_id=person_id)
            count_record = await count_result.single()
        return [map_bankruptcy_case(rec) for rec in records[:limit]], to_total(count_record)
```

- [ ] **Step 4: Implement FastAPI route and list filter parsing**

In `services/api/src/routes/persons.py`, import `BankruptcyCase`.

In the list endpoint filter dict, add:

```python
"has_bankruptcy_case": has_bankruptcy_case,
```

Add a query parameter to the list endpoint signature:

```python
has_bankruptcy_case: bool | None = Query(default=None),
```

Add route after source records:

```python
@router.get("/{person_id}/bankruptcy-cases", response_model=ApiResponse[list[BankruptcyCase]])
async def get_person_bankruptcy_cases(
    person_id: str,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[BankruptcyCase]]:
    """List bankruptcy cases linked to a person."""
    skip, page_limit = page_window(cursor, limit)
    items, total = await repo.get_bankruptcy_cases(person_id, skip, page_limit)
    has_more = skip + page_limit < total
    return envelope(items, request, next_cursor(skip, page_limit, has_more), total_count=total)
```

- [ ] **Step 5: Run backend focused tests**

Run:

```bash
uv run pytest services/api/tests/test_person_bankruptcy_cases.py services/api/tests/test_person_timeline.py -q
```

Expected: PASS.

---

## Task 4: Frontend types and BFF proxy

**Files:**
- Modify: `services/frontend/src/lib/api-types.ts`
- Modify: `services/frontend/src/lib/api-types-person.ts`
- Create: `services/frontend/src/app/bff/persons/[personId]/bankruptcy-cases/route.ts`

- [ ] **Step 1: Add frontend types**

In `services/frontend/src/lib/api-types.ts`, update `ListedPerson`:

```typescript
export interface ListedPerson extends EntityPerson {
  entities: PersonEntitySummary[];
  entity_count: number;
  identifier_count: number;
  order_count: number;
  bankruptcy_case_count: number;
}
```

In `services/frontend/src/lib/api-types-person.ts`, update `TimelineFactCategory`:

```typescript
export type TimelineFactCategory =
  | "identity"
  | "contact"
  | "address"
  | "sale"
  | "relationship"
  | "conversation"
  | "source"
  | "bankruptcy";
```

Add:

```typescript
export interface PersonBankruptcyCase {
  bankruptcy_case_id: string;
  source_system_key: string;
  source_case_id: string;
  case_number: string | null;
  document_type: string | null;
  document_date: string | null;
  event_type: string | null;
  event_date: string | null;
  trustee_name: string | null;
  trustee_firm: string | null;
  source_url: string | null;
  first_seen_at: string | null;
  last_seen_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}
```

- [ ] **Step 2: Add BFF route**

Create `services/frontend/src/app/bff/persons/[personId]/bankruptcy-cases/route.ts`:

```typescript
import type { NextResponse } from "next/server";

import type { PersonBankruptcyCase } from "@/lib/api-types-person";
import { proxyToApi, searchParamsToQuery } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ personId: string }>;
}

export async function GET(request: Request, context: RouteContext): Promise<NextResponse> {
  const { personId } = await context.params;
  const { searchParams } = new URL(request.url);
  return proxyToApi<PersonBankruptcyCase[]>(
    `/persons/${encodeURIComponent(personId)}/bankruptcy-cases`,
    { query: searchParamsToQuery(searchParams) },
  );
}
```

- [ ] **Step 3: Run frontend typecheck**

Run:

```bash
cd services/frontend && npm run typecheck
```

Expected: PASS at this point, because new types are additive.

---

## Task 5: Person profile bankruptcy section

**Files:**
- Create: `services/frontend/src/components/BankruptcyCasesCard.tsx`
- Modify: `services/frontend/src/components/PersonDetailTabs.tsx`

- [ ] **Step 1: Create paginated card**

Create `services/frontend/src/components/BankruptcyCasesCard.tsx`:

```typescript
"use client";

import type { ReactElement } from "react";

import Alert from "@mui/material/Alert";
import CircularProgress from "@mui/material/CircularProgress";
import Link from "@mui/material/Link";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";

import PaginationBar from "@/components/PaginationBar";
import type { PersonBankruptcyCase } from "@/lib/api-types-person";
import { usePaginatedFetch } from "@/lib/usePaginatedFetch";

function displayValue(value: string | null): string {
  return value && value.trim() ? value : "—";
}

function displayDate(value: string | null): string {
  if (!value) return "—";
  try {
    return new Date(value).toLocaleDateString();
  } catch {
    return value;
  }
}

interface Props {
  personId: string;
}

export default function BankruptcyCasesCard({ personId }: Props): ReactElement {
  const { rows, error, loading, from, to, total, hasPrev, hasNext, goNext, goPrev } =
    usePaginatedFetch<PersonBankruptcyCase>(
      `/bff/persons/${encodeURIComponent(personId)}/bankruptcy-cases`,
    );

  return (
    <Paper elevation={0} variant="outlined" sx={{ p: 3 }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 2 }}>
        <Typography variant="h6">Bankruptcy Cases</Typography>
        {loading ? <CircularProgress size={18} /> : null}
      </Stack>
      {error !== null ? (
        <Alert severity="error">{error}</Alert>
      ) : rows === null ? null : rows.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          No bankruptcy cases found.
        </Typography>
      ) : (
        <>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Case #</TableCell>
                <TableCell>Event</TableCell>
                <TableCell>Event date</TableCell>
                <TableCell>Document</TableCell>
                <TableCell>Document date</TableCell>
                <TableCell>Trustee</TableCell>
                <TableCell>Source</TableCell>
                <TableCell>Seen</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {rows.map((c) => (
                <TableRow key={c.bankruptcy_case_id} hover>
                  <TableCell>{displayValue(c.case_number ?? c.source_case_id)}</TableCell>
                  <TableCell>{displayValue(c.event_type)}</TableCell>
                  <TableCell>{displayDate(c.event_date)}</TableCell>
                  <TableCell>{displayValue(c.document_type)}</TableCell>
                  <TableCell>{displayDate(c.document_date)}</TableCell>
                  <TableCell>{displayValue(c.trustee_name ?? c.trustee_firm)}</TableCell>
                  <TableCell>
                    {c.source_url ? (
                      <Link href={c.source_url} target="_blank" rel="noreferrer">
                        {c.source_system_key}
                      </Link>
                    ) : (
                      c.source_system_key
                    )}
                  </TableCell>
                  <TableCell>{displayDate(c.first_seen_at)} / {displayDate(c.last_seen_at)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <PaginationBar
            from={from}
            to={to}
            total={total}
            hasPrev={hasPrev}
            hasNext={hasNext}
            loading={loading}
            onPrev={goPrev}
            onNext={goNext}
          />
        </>
      )}
    </Paper>
  );
}
```

- [ ] **Step 2: Render after sales**

In `services/frontend/src/components/PersonDetailTabs.tsx`, import:

```typescript
import BankruptcyCasesCard from "./BankruptcyCasesCard";
```

Render immediately after `SalesCard`:

```tsx
<SalesCard personId={person.person_id} />
<BankruptcyCasesCard personId={person.person_id} />
<PersonSection title="Audit">
```

- [ ] **Step 3: Run frontend typecheck**

Run:

```bash
cd services/frontend && npm run typecheck
```

Expected: PASS.

---

## Task 6: Person listing column, lazy popover, and presence filter

**Files:**
- Modify: `services/frontend/src/components/PersonsFilterPanel.tsx`
- Modify: `services/frontend/src/app/persons/query.ts`
- Modify: `services/frontend/src/components/PersonsListTable.tsx`
- Modify: `services/frontend/src/components/PersonRow.tsx`

- [ ] **Step 1: Extend filters**

In `PersonsFilterPanel.tsx`, add to `PersonsFilters`:

```typescript
has_bankruptcy_case: boolean | null;
```

Add to `DEFAULT_FILTERS`:

```typescript
has_bankruptcy_case: null,
```

Add a select in the filter controls near other presence filters:

```tsx
<FormControl size="small" sx={{ minWidth: 190 }}>
  <InputLabel>Bankruptcy</InputLabel>
  <Select
    label="Bankruptcy"
    value={draft.has_bankruptcy_case === null ? "all" : draft.has_bankruptcy_case ? "yes" : "no"}
    onChange={(event) => {
      const value = event.target.value;
      setDraft((prev) => ({
        ...prev,
        has_bankruptcy_case: value === "all" ? null : value === "yes",
      }));
    }}
  >
    <MenuItem value="all">All</MenuItem>
    <MenuItem value="yes">Has bankruptcy</MenuItem>
    <MenuItem value="no">No bankruptcy</MenuItem>
  </Select>
</FormControl>
```

- [ ] **Step 2: Add URL query support**

In `services/frontend/src/app/persons/query.ts`, update `isSortField()` to accept:

```typescript
case "bankruptcy_case_count":
```

When parsing filters, set:

```typescript
has_bankruptcy_case: parseBool(searchParams.get("has_bankruptcy_case")),
```

When building query params, add:

```typescript
if (f.has_bankruptcy_case !== null) params.set("has_bankruptcy_case", String(f.has_bankruptcy_case));
```

In `countActiveFilters()`, add:

```typescript
if (f.has_bankruptcy_case !== null) count++;
```

- [ ] **Step 3: Add table sort field and lazy fetch**

In `PersonsListTable.tsx`, import `PersonBankruptcyCase`:

```typescript
import type { PersonBankruptcyCase, PersonIdentifier } from "@/lib/api-types-person";
```

Add sort field:

```typescript
| "bankruptcy_case_count"
```

Add column after Orders:

```typescript
{ field: "bankruptcy_case_count", label: "Bankruptcy", align: "center", sortable: true },
```

Add lazy fetch cache:

```typescript
const bankruptcyFetch = useLazyPersonFetch<PersonBankruptcyCase>(
  (id) => `/bff/persons/${encodeURIComponent(id)}/bankruptcy-cases?limit=50`,
);
```

Pass props to `PersonRow`:

```tsx
bankruptcyCases={bankruptcyFetch.cache.data[p.person_id]}
bankruptcyLoading={bankruptcyFetch.cache.loading.has(p.person_id)}
onRequestBankruptcyCases={() => bankruptcyFetch.request(p.person_id)}
```

- [ ] **Step 4: Render row cell**

In `PersonRow.tsx`, import `CountCardsCell` and `PersonBankruptcyCase` if not already present:

```typescript
import CountCardsCell, { type CountCardItem } from "@/components/CountCardsCell";
import type { PersonBankruptcyCase, PersonIdentifier } from "@/lib/api-types-person";
```

Extend props:

```typescript
bankruptcyCases?: PersonBankruptcyCase[];
bankruptcyLoading: boolean;
onRequestBankruptcyCases: () => void;
```

Add mapper helper:

```typescript
function bankruptcyToItems(cases: PersonBankruptcyCase[] | undefined): CountCardItem[] | undefined {
  return cases?.map((c) => ({
    primary: c.case_number ?? c.source_case_id,
    secondary: [c.event_type, c.event_date].filter(Boolean).join(" · ") || c.document_type,
    color: "warning",
  }));
}
```

Add the cell near Orders/Sources:

```tsx
<TableCell align="center">
  <CountCardsCell
    count={person.bankruptcy_case_count}
    label="cases"
    items={bankruptcyToItems(bankruptcyCases)}
    loading={bankruptcyLoading}
    onOpen={onRequestBankruptcyCases}
    emptyText="No bankruptcy cases"
  />
</TableCell>
```

- [ ] **Step 5: Run frontend typecheck**

Run:

```bash
cd services/frontend && npm run typecheck
```

Expected: PASS.

---

## Task 7: Graph and timeline UI polish

**Files:**
- Modify: `services/frontend/src/components/graph-utils.ts`
- Modify: `services/frontend/src/components/GraphDetailPanel.tsx`
- Modify: `services/frontend/src/components/PersonTimelineTab.tsx`

- [ ] **Step 1: Add graph color and icon**

In `graph-utils.ts`, add to `LABEL_COLORS`:

```typescript
BankruptcyCase: "#b45309",
```

Add a node icon type, for example:

```typescript
| "gavel"
```

Add to `LABEL_ICONS`:

```typescript
BankruptcyCase: "gavel",
```

Add to `ICON_PATHS` using MUI Gavel path data:

```typescript
gavel: "M1 21h12v2H1zm5.24-8.07 2.83-2.83 14.14 14.14-2.83 2.83zM12.9 2.1l7.07 7.07-2.83 2.83-7.07-7.07zM4.83 10.17l7.07-7.07 2.83 2.83-7.07 7.07z",
```

If the path visually looks wrong in browser, replace with the exact `@mui/icons-material/Gavel` SVG path.

- [ ] **Step 2: Add display-name handling**

In `displayNameForNode()` in `graph-utils.ts`, add a BankruptcyCase branch:

```typescript
if (node.label === "BankruptcyCase") {
  return String(
    node.properties.case_number ??
      node.properties.source_case_id ??
      node.properties.bankruptcy_case_id ??
      "Bankruptcy case",
  );
}
```

- [ ] **Step 3: Confirm detail panel renders useful fields**

If `GraphDetailPanel.tsx` currently renders generic property key/value pairs for non-person nodes, no change is needed beyond ensuring it does not hide `BankruptcyCase`. If it has label-specific branches that omit unknown labels, add:

```tsx
{node.label === "BankruptcyCase" ? (
  <Stack spacing={0.75}>
    <Typography variant="subtitle2">{String(node.properties.case_number ?? "Bankruptcy case")}</Typography>
    <Typography variant="body2" color="text.secondary">
      {String(node.properties.event_type ?? "—")} · {String(node.properties.event_date ?? "—")}
    </Typography>
  </Stack>
) : null}
```

- [ ] **Step 4: Add timeline category styling if applicable**

If `PersonTimelineTab.tsx` maps categories to colors/icons, add:

```typescript
bankruptcy: "warning",
```

If it uses a switch for labels/icons, add:

```typescript
case "bankruptcy":
  return "Bankruptcy";
```

- [ ] **Step 5: Run frontend typecheck**

Run:

```bash
cd services/frontend && npm run typecheck
```

Expected: PASS.

---

## Task 8: Verification and browser check

**Files:**
- No code files unless verification reveals issues.

- [ ] **Step 1: Run API lint**

Run:

```bash
uv run --package profile-unifier-api ruff check services/api/src services/api/tests/test_person_bankruptcy_cases.py services/api/tests/test_person_timeline.py
```

Expected: PASS.

- [ ] **Step 2: Run API format check/fix**

Run:

```bash
uv run --package profile-unifier-api ruff format services/api/src services/api/tests/test_person_bankruptcy_cases.py services/api/tests/test_person_timeline.py
```

Expected: files formatted. If it changes files, rerun Task 8 Step 1.

- [ ] **Step 3: Run API focused tests**

Run:

```bash
uv run pytest services/api/tests/test_person_bankruptcy_cases.py services/api/tests/test_person_timeline.py -q
```

Expected: PASS.

- [ ] **Step 4: Run API strict type check**

Run:

```bash
uv run --package profile-unifier-api mypy --strict services/api/src
```

Expected: PASS except for known pre-existing failures in `types_sales.py` and `types_requests.py` if they still appear. Confirm no new errors reference touched files.

- [ ] **Step 5: Run frontend typecheck**

Run:

```bash
cd services/frontend && npm run typecheck
```

Expected: PASS.

- [ ] **Step 6: Run frontend lint**

Run:

```bash
cd services/frontend && npm run lint
```

Expected: PASS within existing warning budget.

- [ ] **Step 7: Rebuild and restart changed containers**

Because this changes Python and TypeScript, run with no cache per project instructions:

```bash
docker compose build --no-cache api frontend && docker compose up -d api frontend
```

Expected: build succeeds and containers start.

- [ ] **Step 8: Browser verification**

Open the app and verify:

1. Person listing shows Bankruptcy column.
2. Bankruptcy presence filter supports All / Has bankruptcy / No bankruptcy.
3. Clicking a non-zero bankruptcy count opens a popover with case summaries.
4. Person profile tab shows Bankruptcy Cases after Sales History with pagination.
5. Person graph shows BankruptcyCase nodes with distinct icon/color.
6. Timeline tab shows bankruptcy facts for sgbankruptcy source records.

If no local data contains bankruptcy cases, verify empty states and use API test fixtures as backend evidence; report that live-data positive-path UI could not be verified.

- [ ] **Step 9: Final status evidence**

Run:

```bash
git status --short
```

Expected: only intended modified/new files. Do not commit unless the user explicitly asks.
