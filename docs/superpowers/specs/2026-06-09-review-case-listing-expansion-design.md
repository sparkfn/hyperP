# Review Case Listing Expansion — Design

**Date:** 2026-06-09
**Scope:** Expand `GET /v1/review-cases` and both frontends (v1 legacy, v2 active) with
server-side search, richer filtering, single-column sort, and total-count pagination.

## Motivation

Today `GET /v1/review-cases` supports only `queue_state`, `assigned_to`, `priority_lte`,
a hardcoded sort (`priority, sla_due_at, created_at`), and `has_more`-only cursor pagination
(no total). The v2 page does search **client-side over the current page only** (broken across
pages) and fakes "Showing X–Y". v1 has no search/sort/pagination at all. We bring the review
queue up to the capability of `GET /v1/persons` (`persons_list.py`), reusing its patterns.

## Backend contract — `GET /v1/review-cases`

All new params optional; existing callers unaffected. The `ReviewCaseSummary` response shape
is **unchanged**.

| Param | Type | Meaning |
|---|---|---|
| `q` | string (≥3 chars) | CONTAINS, case-insensitive over `review_case_id`, `md.decision`, `md.engine_type`, `assigned_to`, and linked left/right **Person** `preferred_full_name` / `preferred_phone` / `preferred_email` (via `ABOUT_LEFT`/`ABOUT_RIGHT`) |
| `priority_gte` | int | Lower bound (pairs with existing `priority_lte`) |
| `decision` | string | `md.decision` exact |
| `engine_type` | string | `md.engine_type` exact |
| `confidence_gte` / `confidence_lte` | float | `md.confidence` range |
| `created_after` / `created_before` | ISO datetime | `rc.created_at` range |
| `sla_due_after` / `sla_due_before` | ISO datetime | `rc.sla_due_at` range |
| `overdue_sla` | bool | `rc.sla_due_at < datetime()` and `queue_state NOT IN ['resolved','cancelled']` |
| `sort_by` | enum | `priority` (default) · `confidence` · `sla_due_at` · `created_at` · `updated_at` · `queue_state` |
| `sort_order` | `asc`/`desc` | default `asc` for priority/sla_due_at; `desc` for confidence/created_at/updated_at |

Unchanged: `queue_state`, `assigned_to`, `priority_lte`, `cursor`, `limit`.
Unknown `sort_by` → HTTP 400 `invalid_request` (mirrors `list_persons`).
`q` shorter than 3 chars → HTTP 400 (mirrors `list_persons`).

**Sort tiebreakers:** every sort appends `, rc.sla_due_at, rc.created_at` so ordering is stable
and the prior default is preserved when `sort_by` is omitted.

**Pagination:** `ReviewRepository.get_page` returns `tuple[list[ReviewCaseSummary], int]`
(items, total). Route computes `has_more = skip + limit < total` and passes `total_count=total`
to `envelope()`. A parallel `build_count_review_cases_query()` shares the same filter clause.

## Implementation map

### Backend
- `graph/queries/review.py` — replace static `LIST_REVIEW_CASES` with:
  - `_REVIEW_FILTER_BASE` (shared non-search WHERE) + `_REVIEW_SEARCH_FILTER` (the `q` predicate) + `_REVIEW_SEARCH_JOINS` (person joins). `_review_body(has_q)` assembles them so the person joins/search predicate are only included when searching — the no-search list and count queries never touch the linked persons.
  - `build_list_review_cases_query(sort_by, sort_order, *, has_q)` — body + RETURN + ORDER/SKIP/LIMIT
  - `build_count_review_cases_query(*, has_q)` — body + `RETURN count(rc)`
  - `_SORT_COLUMNS` whitelist + `_resolve_sort()`; `has_q = filters.get("q") is not None` is computed in the repo.
- `graph/queries/__init__.py` — drop `LIST_REVIEW_CASES`, export the two builders.
- `repositories/protocols/review.py` — expand `ReviewListFilters`; `get_page → tuple[list, int]`.
- `repositories/neo4j/review.py` — run list + count queries, bind all params.
- `routes/review.py` — new `Query` params, `q`/`sort_by` validation, filters dict, `total_count`.

### Frontend v2 (`services/frontend2`)
- `app/review/page.tsx` — search wired to server `q` (debounced, reset to page 1); new filter
  controls (decision, engine, confidence range, priority range, date ranges, overdue toggle);
  clickable sortable column headers (`sort_by`/`sort_order`); real `total` from `meta.total_count`.
- BFF route unchanged (already forwards all `searchParams`).

### Frontend v1 (`services/frontend`)
- `components/ReviewQueueFilters.tsx` — add search `q`, sort controls, new filter inputs.
- `app/review/page.tsx` — forward new params, render Prev/Next pagination + total.
- BFF route unchanged.

### Tests
- `services/api/tests/test_review_listing.py` — query-builder unit tests (sort whitelist, filter
  clause membership, count query shape) + route-level filter/sort/pagination via the app harness.

## Out of scope (YAGNI)
- No fulltext index on `ReviewCase` (CONTAINS is sufficient at current volume).
- No new fields on `ReviewCaseSummary` (e.g. candidate person names in the row) — search traverses
  persons but the row shape stays as-is.
- No multi-column sort beyond the fixed tiebreakers.
