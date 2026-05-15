# BankruptcyCase Person Views Design

## Goal
Expose already-materialized `BankruptcyCase` graph nodes in the person experience: profile tab, graph viewer, profile data timeline, and person listing column/filter.

## Scope
- Add a first-class typed API read model for bankruptcy cases linked by `(:Person)-[:HAS_BANKRUPTCY_CASE]->(:BankruptcyCase)`.
- Add `GET /v1/persons/{person_id}/bankruptcy-cases` with cursor pagination and total count.
- Add `bankruptcy_case_count` to `GET /v1/persons` rows, sorting support, and a presence filter `has_bankruptcy_case`.
- Add a paginated Bankruptcy Cases section after Sales History on the person profile tab.
- Add a Bankruptcy column to the person listing that mirrors count-card behavior used for sources/entities and lazily fetches case summaries.
- Add graph viewer color/icon/display-name support for `BankruptcyCase` nodes.
- Add bankruptcy facts to profile data timeline groups from the linked source record payload and/or described case node data.

## Data model
The ingestion pipeline already creates `BankruptcyCase` nodes with these properties: `bankruptcy_case_id`, `source_system_key`, `source_case_id`, `case_number`, `document_type`, `document_date`, `event_type`, `event_date`, `trustee_name`, `trustee_firm`, `source_url`, `first_seen_at`, `last_seen_at`, `raw_payload`, `created_at`, and `updated_at`. The person relationship is `HAS_BANKRUPTCY_CASE`; source records link via `DESCRIBES_CASE`.

The API exposes all relevant scalar fields except raw payload by default. Raw payload stays available through the existing Source Records section to avoid duplicating bulky source JSON in the bankruptcy case table.

## Person profile UI
The profile tab adds `BankruptcyCasesCard` immediately after `SalesCard`. It uses the existing `usePaginatedFetch` + `PaginationBar` pattern. The table shows case number, event type/date, document type/date, trustee, source system, first/last seen, and source link when present.

## Person listing UI
The listing gains a sortable `Bankruptcy` count column backed by `bankruptcy_case_count`. Clicking a non-zero count opens the existing `CountCardsCell` popover with lazily loaded case summaries. The filter panel gains a tri-state presence filter: All, Has bankruptcy, No bankruptcy.

## Timeline UI
Timeline facts gain category `bankruptcy`. For sgbankruptcy source records, the mapper emits facts for case number, event, document, trustee, and source URL when present. Timeline ordering remains source-record based.

## Graph UI
The graph traversal already includes `BankruptcyCase` because it is not in the excluded labels. The frontend adds explicit color, icon, and display-name handling so the node is recognizable and its detail panel shows useful fields.

## Testing
Backend tests cover mapping, list query filter/count/sort behavior, the paginated endpoint, and timeline bankruptcy facts. Frontend verification covers typecheck/lint/build; UI behavior should be checked in browser after rebuilding/restarting frontend/API containers.