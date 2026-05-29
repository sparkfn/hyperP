# Frontend Data Rebalancing & Person Page Redesign

Date: 2026-05-25
Status: Approved

## Overview

Five interrelated tasks to reduce frontend data processing, restructure the Person Detail and Person List pages, and clean up API route documentation.

## Task 1: Reduce Frontend Workload — Text Formatting to API

**Goal**: Move text string formatting from frontend (`display.ts`) to the API layer. Visual display properties (font, size, color, layout) remain on the frontend.

### Changes — API types (`services/api/src/types.py`)

**`ListedPerson`** — add display-text fields:
- `created_at_display: str` — formatted "28 Apr 2026"
- `updated_at_display: str` — same format
- `preferred_dob_display: str` — formatted "28 Apr 1990"
- `preferred_address_display: str` — e.g. "123 Main St #01-02"
- `profile_completeness_display: str` — e.g. "75%"

Keep raw ISO fields (`created_at`, `updated_at`, `preferred_dob`) for sorting/filtering.

**Popover endpoints** (connections, orders, identifiers, shared-identifiers, source-records):

Return a `display_items: list[PopoverDisplayItem]` alongside the existing data. `PopoverDisplayItem`:
```python
class PopoverDisplayItem(BaseModel):
    primary: str          # Main display text (e.g. person name, order number)
    secondary: str        # Subtitle text (e.g. "phone:123 · address:456")
```

Each popover endpoint adds:
- `items: list[RawModel]` — existing raw data
- `display_items: list[PopoverDisplayItem]` — pre-formatted display strings

### Changes — Frontend (`services/frontend/src/lib/display.ts`)

Remove functions:
- `connectionsToItems`
- `identifiersToItems`
- `ordersToItems`
- `sourcesToItems`
- `bankruptcyToItems` (if exists)

Keep functions:
- `formatDate`, `formatDateTime`, `formatDob` — can be removed later once all callers switch to API display fields
- `statusColor`, `confidenceColor` — color mapping stays on frontend
- `describeConnection` — deleted with `connectionsToItems`

### Changes — List page and detail page components

- `CountCardsCell`-based columns read `display_items` from API response instead of calling `*ToItems` functions
- `PersonDetailTabs` Overview section uses `preferred_dob_display`, `created_at_display`, etc.

## Task 2: Identifiers as Its Own Tab

**Goal**: Move identifiers from being a section within the Profile/Overview tab to their own dedicated tab.

### Changes — `PersonDetailTabs.tsx`

- Current state: Tab 0 "Overview" shows golden profile info. Tab 1 "Identifiers" shows identifiers table.
- **Confirmed**: Identifiers stays as its own dedicated tab. The "Overview/Profile" tab shows golden profile only.
- Tab order remains: Overview → Identifiers → Source Records → Sales → Connections → Audit → Possible Matches → Bankruptcy → Timeline

### Changes — Identifier popup (`IdentifierSourceRecordsDialog`)

- **Title**: Shows `"{type}: {value} ({N} records)"` where N is source_records.length
- **No collapsible sections**: Remove Accordion wrapper. Each source record section is always expanded.
- **Numbered sections**: Each section prefixed with "1.", "2.", "3." etc.
- Use plain `<Box>` or `<Paper>` instead of `<Accordion>`

## Task 3: Possible Matches — Split View + Inline Merge

**Goal**: Rename "Shared Identifiers" tab to "Possible Matches". Clickable rows open a split-view popup for comparing source records across shared identifiers, with an inline merge button.

### Changes — Tab and table

- Tab label: "Possible Matches" (was "Shared Identifiers")
- Table rows: Entire row is clickable (not just the Merge button)
- Click → opens `PossibleMatchReviewDialog`

### Changes — `PossibleMatchReviewDialog` (new component)

Full-width dialog (`maxWidth="xl"` or `fullWidth`) with:

**Header**: `"{candidate_name} vs {current_person_name}"`
**Body — Split view**:
- Left column: source records belonging to the candidate person (filtered to shared identifiers)
- Right column: source records belonging to the current person (filtered to shared identifiers)
- Grouped by shared identifier type+value. Each group shows:
  - `identifier_type: normalized_value` as a section heading
  - Source records arranged left/right
- If a side has no source records for a given shared identifier, show "No records" placeholder

**Footer**:
- "Merge {candidate_name} into {current_person_name}" button (inline — not ManualMergeDialog)
- "Cancel" button

### API changes — New endpoint or extended endpoint

Extend `GET /v1/persons/{person_id}/shared-identifiers` to include source records per shared identifier for both the candidate and the target person. Or add a new endpoint for the split-view data.

New response model for split view:
```python
class PossibleMatchDetail(BaseModel):
    candidate_person_id: str
    candidate_name: str | None
    shared_identifier_groups: list[SharedIdentifierGroup]

class SharedIdentifierGroup(BaseModel):
    identifier_type: str
    normalized_value: str
    candidate_source_records: list[SourceRecord]
    current_person_source_records: list[SourceRecord]
```

### Merge flow

The inline merge button calls an existing or new merge endpoint directly (not via `ManualMergeDialog`). The merge target is the current person (the one whose detail page is open).

## Task 4: Matches Column on Person List Page

**Goal**: Remove the Identifiers column from the person list table. Add a new "Matches" column showing possible-matches count, using the same `CountCardsCell` popover pattern as Connections/Orders.

### Changes — `ListedPerson` API type

Add `possible_match_count: int = 0` to `ListedPerson`.

### Changes — Person list table

- Replace `identifier_count` column with `possible_match_count` column
- Remove `identifier_count` from `SortField` type and `_ALLOWED_SORT` frozenset
- Add `possible_match_count` to `SortField` type and `_ALLOWED_SORT`
- The Matches column uses `CountCardsCell` that lazy-fetches from `/shared-identifiers`
- Popover content uses `display_items` showing candidate names and shared identifier summaries

### Changes — `PersonRow.tsx`

- Replace the Identifiers cell with a Matches cell
- Uses the same pattern as Connections/Orders cells

### Changes — Person list page BFF

- Add `/bff/persons/{personId}/matches-summary` endpoint (or reuse shared-identifiers BFF)

## Task 5: Route Docstrings Cleanup

**Goal**: Consistent, descriptive docstrings on all route handlers in `services/api/src/routes/`.

### Files to audit

- `routes/persons.py`
- `routes/merge.py`
- `routes/admin/` (all files)
- `routes/ingest.py`
- `routes/review.py`
- `routes/entities.py`
- `routes/reports.py`
- `routes/auth/` (all files)
- `routes/public_pages.py`
- `routes/misc.py` (if exists)

### Docstring format

Each route handler to have:
```python
"""Short single-line description of the endpoint's purpose."""
```

Consistency rules:
- Start with capital letter, end with period
- Describe what the endpoint does (not what it returns)
- Be specific about filtering/behavior (e.g. "Return paginated person identifiers with source record details.")
- Remove stale or copy-pasted docstrings

## Implementation Order

1. API docstrings (Task 5) — least risky, can be done first
2. API types and endpoints (Task 1, 3, 4 backend changes)
3. Frontend display changes (Task 1 frontend)
4. Person List page (Task 4)
5. Person Detail page — Identifiers (Task 2)
6. Person Detail page — Possible Matches (Task 3)
7. Rebuild and test

## Data Flow

### Popover display items (Tasks 1, 4)

```
Before:
  API returns raw PersonConnection[] → frontend connectionsToItems() → {primary, secondary} → CountCardsCell

After:
  API returns {items: PersonConnection[], display_items: [{primary, secondary}]} → frontend maps display_items directly → CountCardsCell
```

### Possible Matches split view (Task 3)

```
User clicks row in Possible Matches table
  → API call GET /v1/persons/{currentId}/shared-identifiers/{candidateId}/detail
  → Returns PossibleMatchDetail with grouped source records
  → PossibleMatchReviewDialog renders left/right split
  → User clicks Merge → API call POST /v1/merge (inline, no ManualMergeDialog)
```

## Self-Review

- **Placeholders**: None. All models and endpoints specified.
- **Internal consistency**: Display items pattern consistent across all popover types. Tab restructuring respects existing page architecture.
- **Scope check**: Five well-bounded tasks. No scope creep into unrelated areas (e.g. no graph viewer changes, no auth changes).
- **Ambiguity check**: "Text formatting only" explicitly clarified. Popover display item format (primary/secondary) matches existing `CountCardItem` type exactly.
