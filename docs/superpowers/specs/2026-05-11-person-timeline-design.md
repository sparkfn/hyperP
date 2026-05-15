# Person timeline tab design

## Goal

Add a Timeline tab to the person detail page that shows all profile facts grouped by their source record and ordered by the record's real-world source timestamp. System timestamps such as person creation, update, ingestion, and golden-profile recomputation must not drive chronology except as clearly labeled fallback dates when no source timestamp exists.

## Scope

The Timeline tab covers facts currently represented in the Profile tab where those facts can be attributed to a source record. Timeline entries are grouped one card per source record. Each card lists the profile facts from that source record, such as identifiers, contact fields, addresses, sales facts, conversation facts, and relationship facts when those are present in source-record data.

The source timestamp should use the best available source-side timestamp, starting with `SourceRecord.observed_at`. If no source-side timestamp is available, the timeline may use a fallback timestamp such as `ingested_at`, but the UI must label that timestamp as a fallback so it is not confused with event chronology.

## API design

Add a server-authoritative read path for timeline data:

- FastAPI: `GET /v1/persons/{person_id}/timeline?limit=&cursor=`
- BFF: `GET /bff/persons/{personId}/timeline?limit=&cursor=`

The FastAPI response uses the existing `ApiResponse[T]` envelope and cursor pagination. Data is returned newest-first by event sort key so the backend owns ordering and fallback rules.

Each timeline group includes:

- source record id
- source system key and display label when available
- `occurred_at`
- `timestamp_kind`: `source` or `fallback`
- optional source record metadata such as external id and record type
- fact items extracted from the source record/profile data

Each fact item includes:

- stable id within the group
- category, such as `identity`, `contact`, `address`, `sale`, `relationship`, `conversation`, or `source`
- label
- display value
- optional confidence/provenance detail when already available from source data

## Jump and deep-link design

The Timeline tab includes a jump interface that supports:

- jumping to a date or datetime
- searching/selecting a specific source record/card

The person page supports URL state for opening the timeline and targeting a card, for example with query parameters equivalent to `tab=timeline` plus a source-record id or timestamp target. A targeted card must be scrolled into view and briefly highlighted.

The Profile tab source-record detail UI adds a `View in timeline` action. Clicking it switches to the Timeline tab with the source-record target. If the target card is not in the currently loaded timeline page, the frontend asks the timeline API for the necessary window around the target before scrolling and highlighting.

## Frontend design

The person page tabs become `Profile | Timeline | Matches`.

The Timeline tab uses a vertical rail with cards. It behaves like a chat view:

- initial load shows the newest source-record groups
- the newest loaded group is anchored near the bottom
- scrolling upward triggers lazy loading of older groups
- prepending older groups preserves the user's scroll position
- each card header shows source system, timestamp, and fallback/source label
- each card body lists the facts contributed by that source record

The frontend should use the existing BFF and client fetch patterns rather than calling FastAPI directly from browser code. The Timeline tab can be a client component because it needs scroll detection, lazy loading, jump controls, and highlight behavior.

## Backend data flow

The route depends on the person repository protocol rather than accessing Neo4j directly. The Neo4j repository implementation builds timeline groups from source records linked to the person and their available source payload/fact data. Query strings live in the graph query module, following existing repository-layer conventions.

Pagination should support normal newest-first pages and target lookup for source-record deep links. If target lookup requires a separate endpoint or query parameter, it should still return the same timeline group shape so the UI has one renderer.

## Error handling

- Missing person returns the existing person-not-found behavior.
- Invalid cursors return the existing cursor validation behavior.
- A missing source-record target should leave the Timeline tab open and show a concise not-found message near the jump control.
- Fallback timestamps must be labeled in the UI.
- Empty timelines show an empty state explaining that no source-record facts are available.

## Testing and verification

Backend tests should cover timeline ordering by source timestamp, fallback timestamp labeling, pagination, and target-source-record lookup. Frontend tests or type checks should cover the new types and route handler shape. Manual UI verification should cover initial bottom anchoring, lazy loading on upward scroll, date/source-record jump, and Profile tab `View in timeline` navigation.
