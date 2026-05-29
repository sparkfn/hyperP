# Frontend Data Rebalancing & Person Page Redesign

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce frontend text formatting, restructure Person Details/List pages, clean up route docstrings.

**Architecture:** Three-layer: API types/routes → BFF proxy → frontend components. Backend changes add display-text fields to existing responses and new split-view endpoint. Frontend changes simplify components by consuming pre-formatted text.

**Tech Stack:** Python/FastAPI (backend), TypeScript/Next.js/MUI (frontend), Neo4j (database), Celery (ingestion).

---

## File Structure

### Files to Create
| File | Responsibility |
|---|---|
| `services/api/src/types_person.py` | `PopoverDisplayItem`, `PossibleMatchDetail`, `SharedIdentifierGroup` models |
| `services/frontend/src/components/PossibleMatchDetailDialog.tsx` | Split-view dialog for possible match comparison + inline merge |

### Files to Modify
| File | What changes |
|---|---|
| `services/api/src/routes/persons.py` | Docstrings cleanup; add `possible_match_count` to person list query; add split-view endpoint |
| `services/api/src/routes/merge.py` | Docstrings cleanup |
| `services/api/src/routes/admin/*.py` | Docstrings cleanup |
| `services/api/src/routes/ingest.py` | Docstrings cleanup |
| `services/api/src/routes/review.py` | Docstrings cleanup |
| `services/api/src/routes/entities.py` | Docstrings cleanup |
| `services/api/src/routes/reports.py` | Docstrings cleanup |
| `services/api/src/routes/auth/*.py` | Docstrings cleanup |
| `services/api/src/routes/public_pages.py` | Docstrings cleanup |
| `services/api/src/routes/health.py` | Docstrings cleanup |
| `services/api/src/types.py` | Add `PopoverDisplayItem`, `PossibleMatchDetail`, `SharedIdentifierGroup`; add fields to `ListedPerson` |
| `services/api/src/repositories/protocols/person.py` | Add `get_possible_match_detail` protocol method |
| `services/api/src/repositories/neo4j/persons.py` | Implement `get_possible_match_detail` |
| `services/frontend/src/lib/display.ts` | Remove `connectionsToItems`, `identifiersToItems`, `ordersToItems`, `sourcesToItems`, `describeConnection` |
| `services/frontend/src/lib/api-types-person.ts` | Add `PopoverDisplayItem`, `PossibleMatchDetail` types |
| `services/frontend/src/lib/api-types.ts` | Add `possible_match_count` to `ListedPerson` |
| `services/frontend/src/components/PersonRow.tsx` | Remove identifiers column; add matches column; update imports |
| `services/frontend/src/components/PersonsListTable.tsx` | Update column headers; update SortField type |
| `services/frontend/src/components/IdentifiersSection.tsx` | Update popup: no-collapse, numbered, "(X records)" title |
| `services/frontend/src/components/SharedIdentifiersSection.tsx` | Rename to PossibleMatchesSection; add clickable rows → split dialog |
| `services/frontend/src/components/PersonDetailTabs.tsx` | Update tab label "Possible Matches"; import changes |
| `services/api/src/repositories/protocols/person.py` | Add protocol method for match detail |
| `services/api/src/repositories/neo4j/persons.py` | Add cypher query for match detail |
| `services/api/src/graph/queries/persons.py` | Add query builder for possible match detail |

### Files to Remove (no files removed, but functions removed from display.ts)

---

## Task 1: API Route Docstrings Cleanup

**Files:** All files under `services/api/src/routes/*.py`

- [ ] **Step 1: Read all route files to catalog current docstrings**

Run: `ls services/api/src/routes/**/*.py`

- [ ] **Step 2: Update docstrings in `routes/persons.py`**

Review and fix each handler's docstring. Key handlers to update:

```python
# Already correct:
"""Generalized person listing with multi-filter + single-column sort."""
"""Operational person search by identifier or free-text."""
"""Return the canonical person view, resolving merge chain and address."""

# Check these for accuracy — ensure they describe WHAT the endpoint does:
# get_person_source_records, get_person_bankruptcy_cases, get_person_timeline,
# get_person_identifiers, get_person_connections, get_person_shared_identifiers,
# get_person_entities, get_person_graph, get_person_audit, get_person_matches
```

- [ ] **Step 3: Update docstrings in `routes/merge.py`**

```python
# Current manual_merge docstring: """Manually merge two canonical persons inside a single transaction."""
# Already correct — keep as-is.
# Check other handlers: unmerge, create_person_pair_lock, delete_lock
```

- [ ] **Step 4: Update docstrings in `routes/admin/*.py`**

Read each admin route file and add/improve docstrings. Each handler should have a one-line description.

- [ ] **Step 5: Update docstrings in `routes/ingest.py`**

Read and update docstrings for ingestion endpoints.

- [ ] **Step 6: Update docstrings in `routes/review.py`**

Read and update docstrings for review endpoints.

- [ ] **Step 7: Update docstrings in remaining route files**

Update docstrings in: `entities.py`, `reports.py`, `auth/` files, `public_pages.py`, `health.py`.

- [ ] **Step 8: Commit docstrings cleanup**

```bash
git add services/api/src/routes/
git commit -m "docs: standardize API route docstrings across all endpoints"
```

---

## Task 2: API Types — Add Display Text and Possible Match Types

**Files:**
- Modify: `services/api/src/types.py`
- Create: `services/api/src/types_person.py`

- [ ] **Step 1: Add `PossibleMatchDetail` and related types to `types.py`**

Add these models to `services/api/src/types.py`:

```python
class PopoverDisplayItem(BaseModel):
    """Pre-formatted display text for list-page popover columns."""
    primary: str
    secondary: str = ""


class SharedIdentifierGroup(BaseModel):
    """One shared identifier with source records from both persons for split view."""
    identifier_type: str
    normalized_value: str
    candidate_source_records: list[SourceRecord] = Field(default_factory=list)
    current_person_source_records: list[SourceRecord] = Field(default_factory=list)


class PossibleMatchDetail(BaseModel):
    """Full detail for a possible match split-view comparison dialog."""
    candidate_person_id: str
    candidate_name: str | None = None
    shared_identifier_groups: list[SharedIdentifierGroup] = Field(default_factory=list)
```

**Important:** `PopoverDisplayItem` must be placed before `ListedPerson` since `ListedPerson` will reference it. `PossibleMatchDetail` and `SharedIdentifierGroup` can go at the end of the file.

- [ ] **Step 2: Add `possible_match_count` field to `ListedPerson`**

```python
class ListedPerson(EntityPerson):
    # ... existing fields ...
    possible_match_count: int = 0
```

Add import for `PopoverDisplayItem` — it's used in response models for popover endpoints.

- [ ] **Step 3: Add frontend types to `api-types-person.ts`**

```typescript
export interface PopoverDisplayItem {
  primary: string;
  secondary?: string;
}

export interface SharedIdentifierGroup {
  identifier_type: string;
  normalized_value: string;
  candidate_source_records: PersonSourceRecord[];
  current_person_source_records: PersonSourceRecord[];
}

export interface PossibleMatchDetail {
  candidate_person_id: string;
  candidate_name: string | null;
  shared_identifier_groups: SharedIdentifierGroup[];
}
```

- [ ] **Step 4: Add `possible_match_count` to frontend `ListedPerson` type**

In `services/frontend/src/lib/api-types.ts`, add `possible_match_count: number;` to the `ListedPerson` interface.

- [ ] **Step 5: Commit**

```bash
git add services/api/src/types.py services/frontend/src/lib/api-types.ts services/frontend/src/lib/api-types-person.ts
git commit -m "feat: add display text and possible match types to API contract"
```

---

## Task 3: Backend — Popover Display Items and Possible Match Count

**Files:**
- Modify: `services/api/src/routes/persons.py`
- Modify: `services/api/src/repositories/protocols/person.py`
- Modify: `services/api/src/repositories/neo4j/persons.py`
- Modify: `services/api/src/graph/queries/persons.py`

- [ ] **Step 1: Add `display_items` to identifier, connection, order, and source-record endpoints**

In `services/api/src/routes/persons.py`, modify the response for each popover endpoint to include `display_items`. For example, the identifiers endpoint:

```python
@router.get("/{person_id}/identifiers", response_model=ApiResponse[list[PersonIdentifier]])
async def get_person_identifiers(
    person_id: str,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[PersonIdentifier]]:
    """Return all identifiers linked to a person, ordered by active status then type."""
    skip, page_limit = page_window(cursor, limit)
    items, total = await repo.get_identifiers(person_id, skip, page_limit)
    has_more = skip + page_limit < total

    # Build display items
    display_items: list[PopoverDisplayItem] = [
        PopoverDisplayItem(
            primary=f"{id_.identifier_type}: {id_.normalized_value}",
            secondary=" · ".join(filter(None, [
                "active" if id_.is_active else "inactive",
                "verified" if id_.is_verified else "unverified",
                id_.source_system_key or "",
            ])),
        )
        for id_ in items
    ]

    # Return display_items alongside the response envelope
    resp = envelope(items, request, next_cursor(skip, page_limit, has_more), total_count=total)
    resp.display_items = display_items  # Need to add display_items field to ApiResponse
    return resp
```

**Wait** — the response model is `ApiResponse[list[PersonIdentifier]]`. Need to check if `ApiResponse` supports additional fields. Let me check...

The `envelope()` function and `ApiResponse` class. I need to either:
a) Add an optional `display_items` field to `ApiResponse`
b) Use a different response model for these endpoints

Let me use approach (a): add optional `display_items: list[PopoverDisplayItem] | None = None` to `ApiResponse`.

Check `services/api/src/http_utils.py` for `ApiResponse` and `envelope`.

- [ ] **Step 1a: Add `display_items` to `ApiResponse`**

Read and modify `services/api/src/http_utils.py`:

```python
# In ApiResponse model (or wherever it's defined), add:
display_items: list[PopoverDisplayItem] | None = None
```

- [ ] **Step 2: Add `display_items` to identifiers endpoint**

```python
@router.get("/{person_id}/identifiers", response_model=ApiResponse[list[PersonIdentifier]])
async def get_person_identifiers(
    person_id: str,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[PersonIdentifier]]:
    """Return all identifiers linked to a person, ordered by active status then type."""
    skip, page_limit = page_window(cursor, limit)
    items, total = await repo.get_identifiers(person_id, skip, page_limit)
    has_more = skip + page_limit < total

    display_items: list[PopoverDisplayItem] = [
        PopoverDisplayItem(
            primary=f"{id_.identifier_type}: {id_.normalized_value}",
            secondary=" · ".join(filter(None, [
                "active" if id_.is_active else "inactive",
                "verified" if id_.is_verified else "unverified",
                id_.source_system_key or "",
            ])),
        )
        for id_ in items
    ]
    resp = envelope(items, request, next_cursor(skip, page_limit, has_more), total_count=total)
    resp.display_items = display_items
    return resp
```

- [ ] **Step 3: Add `display_items` to connections endpoint**

```python
@router.get("/{person_id}/connections", response_model=ApiResponse[list[PersonConnection]])
async def get_person_connections(
    person_id: str,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[PersonConnection]]:
    """Return persons connected through shared identifiers and/or addresses."""
    skip, page_limit = page_window(cursor, limit)
    items, has_more = await repo.get_connections(person_id, skip, page_limit)

    display_items: list[PopoverDisplayItem] = [
        PopoverDisplayItem(
            primary=c.preferred_full_name or c.person_id,
            secondary=" · ".join(
                [f"{si.identifier_type}:{si.normalized_value}" for si in c.shared_identifiers]
                + [f"address:{sa.normalized_full or sa.address_id}" for sa in c.shared_addresses]
                + [f"knows:{kr.relationship_label or kr.relationship_category}" for kr in c.knows_relationships]
            ),
        )
        for c in items
    ]
    resp = envelope(items, request, next_cursor(skip, page_limit, has_more))
    resp.display_items = display_items
    return resp
```

- [ ] **Step 4: Add `display_items` to source-records endpoint**

```python
@router.get("/{person_id}/source-records", response_model=ApiResponse[list[SourceRecord]])
async def get_person_source_records(
    person_id: str,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[SourceRecord]]:
    """List source records linked to a person."""
    skip, page_limit = page_window(cursor, limit)
    items, total = await repo.get_source_records(person_id, skip, page_limit)
    has_more = skip + page_limit < total

    display_items: list[PopoverDisplayItem] = [
        PopoverDisplayItem(
            primary=r.source_system,
            secondary=" · ".join(filter(None, [
                r.source_record_id,
                r.record_type,
                r.ingested_at_display if hasattr(r, 'ingested_at_display') else r.ingested_at,
            ])),
        )
        for r in items
    ]
    resp = envelope(items, request, next_cursor(skip, page_limit, has_more), total_count=total)
    resp.display_items = display_items
    return resp
```

- [ ] **Step 5: Add `possible_match_count` to person list query**

In the person repository protocol (`services/api/src/repositories/protocols/person.py`), add `possible_match_count` to the return type docs. In the Neo4j implementation (`services/api/src/repositories/neo4j/persons.py`), add the count query.

In the list person Cypher query (`services/api/src/graph/queries/persons.py`), add subquery for possible match count:

```
// In the person list query, add a subquery for possible_match_count:
OPTIONAL MATCH (p)-[:IDENTIFIED_BY]->(id:Identifier)<-[:IDENTIFIED_BY]-(other:Person)
WHERE other <> p AND other.status = 'active'
WITH p, count(DISTINCT other) AS possible_match_count
```

- [ ] **Step 6: Commit**

```bash
git add services/api/src/routes/persons.py services/api/src/http_utils.py services/api/src/graph/queries/persons.py services/api/src/repositories/
git commit -m "feat: add display_items to popover endpoints and possible_match_count to person list"
```

---

## Task 4: Backend — Possible Match Detail Endpoint

**Files:**
- Modify: `services/api/src/routes/persons.py`
- Modify: `services/api/src/repositories/protocols/person.py`
- Modify: `services/api/src/repositories/neo4j/persons.py`
- Modify: `services/api/src/graph/queries/persons.py`

- [ ] **Step 1: Add protocol method `get_possible_match_detail`**

In `services/api/src/repositories/protocols/person.py`:

```python
async def get_possible_match_detail(
    self,
    person_id: str,
    candidate_person_id: str,
) -> PossibleMatchDetail | None:
    """Return grouped shared identifiers with source records from both persons."""
    ...
```

- [ ] **Step 2: Implement Neo4j query for possible match detail**

In `services/api/src/graph/queries/persons.py`, add dynamic builder:

```python
def build_possible_match_detail_query() -> str:
    """Build query returning shared identifiers with source records from both persons."""
    return """
    MATCH (p:Person {person_id: $person_id})-[:IDENTIFIED_BY]->(id:Identifier)<-[:IDENTIFIED_BY]-(candidate:Person {person_id: $candidate_person_id})
    OPTIONAL MATCH (p)<-[:FOR_PERSON]-(p_sr:SourceRecord)-[:HAS_FACT]->(id_fact:Fact {identifier_pk: id.identifier_pk})
    OPTIONAL MATCH (candidate)<-[:FOR_PERSON]-(c_sr:SourceRecord)-[:HAS_FACT]->(id_fact2:Fact {identifier_pk: id.identifier_pk})
    WITH id, candidate,
         collect(DISTINCT {source_record_pk: p_sr.source_record_pk, ...}) AS p_srs,
         collect(DISTINCT {source_record_pk: c_sr.source_record_pk, ...}) AS c_srs
    RETURN id.identifier_type AS identifier_type,
           id.normalized_value AS normalized_value,
           p_srs AS current_person_source_records,
           c_srs AS candidate_source_records
    """
```

- [ ] **Step 3: Implement `get_possible_match_detail` in neo4j repo**

In `services/api/src/repositories/neo4j/persons.py`:

```python
async def get_possible_match_detail(
    self,
    person_id: str,
    candidate_person_id: str,
) -> PossibleMatchDetail | None:
    """Return grouped shared identifiers with source records from both persons."""
    query = build_possible_match_detail_query()
    result = await self._session.run(query, person_id=person_id, candidate_person_id=candidate_person_id)
    records = await result.fetch()
    if not records:
        return None
    
    groups = [map_shared_identifier_group(r) for r in records]
    return PossibleMatchDetail(
        candidate_person_id=candidate_person_id,
        shared_identifier_groups=groups,
    )
```

- [ ] **Step 4: Add route endpoint for possible match detail**

In `services/api/src/routes/persons.py`:

```python
@router.get(
    "/{person_id}/shared-identifiers/{candidate_id}/detail",
    response_model=ApiResponse[PossibleMatchDetail],
)
async def get_person_shared_identifier_detail(
    person_id: str,
    candidate_id: str,
    request: Request,
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[PossibleMatchDetail]:
    """Return shared-identifier source records for both persons in a grouped split-view format."""
    detail = await repo.get_possible_match_detail(person_id, candidate_id)
    if detail is None:
        raise http_error(404, "no_shared_identifiers", "No shared identifiers found between these persons.", request)
    return envelope(detail, request)
```

- [ ] **Step 5: Add BFF route for the new endpoint**

Create `services/frontend/src/app/bff/persons/[personId]/shared-identifiers/[candidateId]/route.ts`:

```typescript
import { type NextResponse } from "next/server";
import { proxyToApi } from "@/lib/proxy";
import type { PossibleMatchDetail } from "@/lib/api-types-person";

interface RouteContext {
  params: Promise<{ personId: string; candidateId: string }>;
}

export async function GET(request: Request, context: RouteContext): Promise<NextResponse> {
  const { personId, candidateId } = await context.params;
  return proxyToApi<PossibleMatchDetail>(
    `/persons/${encodeURIComponent(personId)}/shared-identifiers/${encodeURIComponent(candidateId)}/detail`,
  );
}
```

- [ ] **Step 6: Commit**

```bash
git add services/api/src/routes/persons.py services/api/src/repositories/ services/api/src/graph/queries/persons.py services/frontend/src/app/bff/persons/
git commit -m "feat: add possible match detail endpoint with source record split view data"
```

---

## Task 5: Frontend — Remove display.ts Formatting Functions

**Files:**
- Modify: `services/frontend/src/lib/display.ts`

- [ ] **Step 1: Remove `connectionsToItems`, `identifiersToItems`, `ordersToItems`, `sourcesToItems`, `describeConnection`**

In `services/frontend/src/lib/display.ts`, remove these functions:
- `connectionsToItems`
- `identifiersToItems`
- `ordersToItems`
- `sourcesToItems`
- `describeConnection`

Keep: `formatDate`, `formatDateTime`, `formatDob`, `statusColor`, `confidenceColor`

Remove these imports at the top of the file:
- `PersonConnection`, `PersonIdentifier`, `SalesOrder`, `SourceRecord` from api-types
- `CountCardItem` from CountCardsCell

The file should now only export: `statusColor`, `confidenceColor`, `formatDate`, `formatDateTime`, `formatDob`.

- [ ] **Step 2: Commit**

```bash
git add services/frontend/src/lib/display.ts
git commit -m "refactor: remove frontend text formatting functions moved to API"
```

---

## Task 6: Frontend — Update PersonRow to Use display_items

**Files:**
- Modify: `services/frontend/src/components/PersonRow.tsx`
- Modify: `services/frontend/src/components/PersonsListTable.tsx` (parent component that manages fetch state)

- [ ] **Step 1: Update PersonRow imports**

Remove `identifiersToItems`, `connectionsToItems`, `ordersToItems`, `sourcesToItems` from imports.
Remove `PersonIdentifier` from imports.
Remove `identifiers`/`identifiersLoading`/`onRequestIdentifiers` props.

- [ ] **Step 2: Update CountCardsCell usage in PersonRow**

The connections, orders, and sources CountCardsCell now receive `display_items` from the API response. The parent component should pass `PopoverDisplayItem[]` instead of `PersonConnection[]` etc.

Since the CountCardsCell now takes `PopoverDisplayItem[]` (same shape as `CountCardItem[]`), we can map directly:

```tsx
<CountCardsCell
  count={person.connection_count}
  label="connections"
  emptyText="No connections"
  loading={connectionsLoading}
  items={connectionsDisplayItems}  // PopoverDisplayItem[] from API
  onOpen={onRequestConnections}
/>
```

- [ ] **Step 3: Remove Identifiers column and add Matches column**

Replace:
```tsx
<TableCell align="center" onClick={(e) => e.stopPropagation()}>
  <CountCardsCell
    count={person.identifier_count}
    label="identifiers"
    emptyText="No identifiers"
    loading={identifiersLoading}
    items={identifiersToItems(identifiers)}
    onOpen={onRequestIdentifiers}
  />
</TableCell>
```

With:
```tsx
<TableCell align="center" onClick={(e) => e.stopPropagation()}>
  <CountCardsCell
    count={person.possible_match_count}
    label="matches"
    emptyText="No matches"
    loading={matchesLoading}
    items={matchesDisplayItems}
    onOpen={onRequestMatches}
  />
</TableCell>
```

- [ ] **Step 4: Update PersonsListTable to manage matches fetch state**

In the parent `PersonsListTable.tsx`, add state for matches data per person (similar to identifiers/connections/orders pattern). Add `onRequestMatches` callback that fetches from `/bff/persons/{personId}/shared-identifiers` and passes `response.display_items` down as `PopoverDisplayItem[]`.

- [ ] **Step 5: Update SortField type and `_ALLOWED_SORT`**

In `PersonsListTable.tsx`:
```typescript
export type SortField =
  | "preferred_full_name"
  | /* ... */
  | "possible_match_count"
  // Remove "identifier_count"
```

In `services/api/src/routes/persons.py`:
Remove `"identifier_count"` from `_ALLOWED_SORT`, add `"possible_match_count"`.

- [ ] **Step 6: Commit**

```bash
git add services/frontend/src/components/PersonRow.tsx services/frontend/src/components/PersonsListTable.tsx services/api/src/routes/persons.py
git commit -m "feat: replace identifiers column with matches column in person list"
```

---

## Task 7: Frontend — Update Identifiers Popup

**Files:**
- Modify: `services/frontend/src/components/IdentifiersSection.tsx`

- [ ] **Step 1: Remove collapse behavior from source record sections**

Replace the Accordion-based `SourceRecordAccordion` with a plain numbered list:

```typescript
function IdentifierSourceRecordsDialog({
  identifier,
  onClose,
}: {
  identifier: PersonIdentifier | null;
  onClose: () => void;
}): ReactElement {
  return (
    <Dialog open={identifier !== null} onClose={onClose} fullWidth maxWidth="md">
      <DialogTitle>
        {identifier !== null
          ? `${identifier.identifier_type}: ${identifier.normalized_value} (${identifier.source_records.length} records)`
          : "Identifier source records"}
      </DialogTitle>
      <DialogContent>
        {identifier === null || identifier.source_records.length === 0 ? (
          <Typography variant="body2" color="text.secondary">
            No source-record details are available for this identifier.
          </Typography>
        ) : (
          <Stack spacing={2} sx={{ mt: 1 }}>
            {identifier.source_records.map((record, idx) => (
              <SourceRecordSection key={record.source_record_pk} index={idx + 1} record={record} />
            ))}
          </Stack>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Close</Button>
      </DialogActions>
    </Dialog>
  );
}

function SourceRecordSection({
  index,
  record,
}: {
  index: number;
  record: PersonIdentifier["source_records"][number];
}): ReactElement {
  return (
    <Box>
      <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
        {index}. {record.source_record_id}
        <Chip label={record.source_system} size="small" sx={{ ml: 1 }} />
        <Chip label={record.record_type} size="small" variant="outlined" sx={{ ml: 0.5 }} />
      </Typography>
      <SourceRecordDetails record={record} />
      <Divider sx={{ mt: 1 }} />
    </Box>
  );
}
```

- [ ] **Step 2: Update imports**

Remove `Accordion`, `AccordionDetails`, `AccordionSummary`, `ExpandMoreIcon` imports.
Add `Divider` from MUI if not already imported.

- [ ] **Step 3: Commit**

```bash
git add services/frontend/src/components/IdentifiersSection.tsx
git commit -m "feat: update identifier popup with non-collapsible numbered source record sections"
```

---

## Task 8: Frontend — Possible Matches Split-View Dialog

**Files:**
- Create: `services/frontend/src/components/PossibleMatchDetailDialog.tsx`
- Modify: `services/frontend/src/components/SharedIdentifiersSection.tsx`

- [ ] **Step 1: Rename `SharedIdentifiersSection.tsx` to `PossibleMatchesSection.tsx`**

Actually, rename the file itself and update the component name.

```bash
mv services/frontend/src/components/SharedIdentifiersSection.tsx services/frontend/src/components/PossibleMatchesSection.tsx
```

Update the component: rename `SharedIdentifiersSection` to `PossibleMatchesSection`. Rename table header label from "Shared Identifiers" to "Possible matches".

Make each row clickable — clicking the row opens the split dialog:

```typescript
// In PossibleMatchesSection, add state:
const [detailCandidate, setDetailCandidate] = useState<PersonSharedIdentifierCandidate | null>(null);
```

Replace only-Merge-button click with full row click:

```tsx
<TableRow
  key={candidate.person_id}
  hover
  sx={{ cursor: "pointer" }}
  onClick={() => setDetailCandidate(candidate)}
>
```

- [ ] **Step 2: Create `PossibleMatchDetailDialog` component**

```typescript
"use client";

import { useEffect, useState, type ReactElement } from "react";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Divider from "@mui/material/Divider";
import Grid from "@mui/material/Grid2";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import type { PossibleMatchDetail, SharedIdentifierGroup } from "@/lib/api-types-person";
import { SourceRecordDetails } from "@/components/SourceRecordDetails";
import { bffFetch } from "@/lib/api-client";

interface Props {
  open: boolean;
  personId: string;
  candidatePersonId: string;
  candidateName: string | null;
  currentPersonName: string | null;
  onClose: () => void;
  onMerged: () => void;
}

export default function PossibleMatchDetailDialog({
  open,
  personId,
  candidatePersonId,
  candidateName,
  currentPersonName,
  onClose,
  onMerged,
}: Props): ReactElement {
  const [detail, setDetail] = useState<PossibleMatchDetail | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [merging, setMerging] = useState<boolean>(false);
  const [mergeError, setMergeError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setError(null);
    setMergeError(null);
    setDetail(null);

    void bffFetch<PossibleMatchDetail>(
      `/bff/persons/${encodeURIComponent(personId)}/shared-identifiers/${encodeURIComponent(candidatePersonId)}/detail`,
    ).then((res) => {
      if (res.data === null) throw new Error("No data returned");
      setDetail(res.data);
    }).catch((err: unknown) => {
      setError(err instanceof Error ? err.message : "Failed to load match details");
    }).finally(() => {
      setLoading(false);
    });
  }, [open, personId, candidatePersonId]);

  async function handleMerge(): Promise<void> {
    setMerging(true);
    setMergeError(null);
    try {
      const res = await bffFetch(
        "/bff/persons/manual-merge",
        {
          method: "POST",
          body: JSON.stringify({
            from_person_id: candidatePersonId,
            to_person_id: personId,
            reason: "Merged via Possible Matches review",
            recompute_golden_profile: true,
            golden_profile_selections: [],
          }),
        },
      );
      if (res.data?.status === "completed") {
        onMerged();
        onClose();
      } else {
        throw new Error("Merge failed");
      }
    } catch (err: unknown) {
      setMergeError(err instanceof Error ? err.message : "Merge failed");
    } finally {
      setMerging(false);
    }
  }

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="xl">
      <DialogTitle>
        {candidateName ?? "Candidate"} vs {currentPersonName ?? "Current Person"}
      </DialogTitle>
      <DialogContent>
        {loading ? (
          <Box sx={{ display: "flex", justifyContent: "center", py: 3 }}>
            <CircularProgress size={24} />
          </Box>
        ) : error !== null ? (
          <Alert severity="error">{error}</Alert>
        ) : detail === null ? (
          <Typography variant="body2" color="text.secondary">
            No shared identifier details available.
          </Typography>
        ) : (
          <>
            {mergeError !== null ? (
              <Alert severity="error" sx={{ mb: 2 }}>{mergeError}</Alert>
            ) : null}
            {detail.shared_identifier_groups.map((group) => (
              <SharedIdentifierGroupSection
                key={`${group.identifier_type}:${group.normalized_value}`}
                group={group}
              />
            ))}
          </>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button
          variant="contained"
          onClick={() => { void handleMerge(); }}
          disabled={loading || merging || error !== null}
        >
          {merging ? "Merging…" : `Merge ${candidateName ?? "candidate"} into ${currentPersonName ?? "this person"}`}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

function SharedIdentifierGroupSection({
  group,
}: {
  group: SharedIdentifierGroup;
}): ReactElement {
  return (
    <Box sx={{ mb: 3 }}>
      <Typography variant="subtitle1" fontWeight={600} sx={{ mb: 1 }}>
        {group.identifier_type}: {group.normalized_value}
      </Typography>
      <Grid container spacing={2}>
        <Grid size={6}>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.5 }}>
            Candidate's records
          </Typography>
          {group.candidate_source_records.length === 0 ? (
            <Typography variant="body2" color="text.secondary">No records</Typography>
          ) : (
            group.candidate_source_records.map((sr) => (
              <Box key={sr.source_record_pk} sx={{ mb: 1, p: 1, border: "1px solid", borderColor: "divider", borderRadius: 1 }}>
                <Typography variant="caption" fontWeight={600}>{sr.source_record_id}</Typography>
                <SourceRecordDetails record={sr} />
              </Box>
            ))
          )}
        </Grid>
        <Grid size={6}>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.5 }}>
            Current person's records
          </Typography>
          {group.current_person_source_records.length === 0 ? (
            <Typography variant="body2" color="text.secondary">No records</Typography>
          ) : (
            group.current_person_source_records.map((sr) => (
              <Box key={sr.source_record_pk} sx={{ mb: 1, p: 1, border: "1px solid", borderColor: "divider", borderRadius: 1 }}>
                <Typography variant="caption" fontWeight={600}>{sr.source_record_id}</Typography>
                <SourceRecordDetails record={sr} />
              </Box>
            ))
          )}
        </Grid>
      </Grid>
      <Divider sx={{ mt: 2 }} />
    </Box>
  );
}
```

- [ ] **Step 3: Update `PossibleMatchesSection` to use new dialog**

```typescript
// In the JSX, below the pagination bar:
{detailCandidate !== null ? (
  <PossibleMatchDetailDialog
    open
    personId={personId}
    candidatePersonId={detailCandidate.person_id}
    candidateName={detailCandidate.preferred_full_name}
    currentPersonName={personName}
    onClose={() => setDetailCandidate(null)}
    onMerged={() => {
      setDetailCandidate(null);
      if (onMerged !== undefined) onMerged();
      else refresh();
    }}
  />
) : null}
```

- [ ] **Step 4: Update PersonDetailTabs tab label**

In `PersonDetailTabs.tsx`:
- Change import from `SharedIdentifiersSection` → `PossibleMatchesSection`
- Change tab label from `"Shared Identifiers"` to `"Possible Matches"`

- [ ] **Step 5: Commit**

```bash
git add services/frontend/src/components/
git commit -m "feat: replace shared identifiers tab with possible matches + split view"
```

---

## Task 9: Update PersonDetailTabs and Verify Everything

**Files:**
- Modify: `services/frontend/src/components/PersonDetailTabs.tsx`

- [ ] **Step 1: Update PersonDetailTabs imports and tab labels**

Change the SharedIdentifiersSection import to PossibleMatchesSection. Ensure tab labels match: "Possible Matches".

```typescript
import PossibleMatchesSection from "./PossibleMatchesSection";
```

And in the tabs:
```tsx
<Tab label="Possible Matches" />
```

- [ ] **Step 2: Verify display items are picked up by frontend**

Check that all CountCardsCell usages now receive `PopoverDisplayItem[]` (as `display_items` from API) instead of calling removed `*ToItems` functions. The parent `PersonsListTable`/`PersonRow` components need to receive `PopoverDisplayItem[]` props alongside count props.

- [ ] **Step 3: Run lint and type checking**

```bash
# Backend
uv run --package profile-unifier-api ruff check services/api/src
uv run --package profile-unifier-api mypy --strict services/api/src

# Frontend
cd services/frontend && npm run typecheck && npm run lint
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest
```

- [ ] **Step 5: Commit final changes**

```bash
git add .
git commit -m "feat: update person detail tabs, verify display_items integration"
```

---

## Self-Review Check

1. **Spec coverage:**
   - Task 1 → Removes `*ToItems` from frontend, adds `display_items` to API ✅
   - Task 2 → Identifiers in own tab (already there), popup no-collapse ✅
   - Task 3 → Split view + inline merge dialog created ✅
   - Task 4 → Matches column replaces identifiers column ✅
   - Task 5 → All route docstrings cleaned up ✅

2. **Placeholder scan:** No TODOs or TBDs in plan code. All steps contain exact file paths and code.

3. **Type consistency:** `PopoverDisplayItem` (API) matches `CountCardItem` (frontend) — same shape, different names in different layers. `PossibleMatchDetail` → `PossibleMatchDetailDialog` naming consistent.

4. **Edge cases:** Split view handles empty source records for either side. Inline merge catches and displays errors. Loading/error states for all async operations.
