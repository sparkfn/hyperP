# Spec 3 — Fix phppos sales→customer FK mapping (person_id vs customers.id)

**Date:** 2026-06-08
**Status:** Approved (design)
**Context:** Re-scoped from the original "sales phone+name fallback" after profiling uncovered a pre-existing mis-linking bug. The phone+name fallback is deferred (it would serve only ~0.1% truly-anonymous orders; see "Deferred").

## The bug (evidence-backed)

`phppos_sales.customer_id` is a **person_id** (schema FK: `FOREIGN KEY (customer_id) REFERENCES phppos_customers(person_id)`), but the eko/speedzone **identity** connectors key their source records on `phppos_customers.id` (`{source}-customer-{customers.id}`). The sales connector builds the link target as `{source}-customer-{sale.customer_id}` = `{source}-customer-{person_id}`, and `LINK_SALES_TO_IDENTITY_RECORD` matches by exact `source_record_id`. Because `customers.id != person_id` per row, the sale attaches `FOR_CUSTOMER_RECORD` to whichever identity record happens to have `customers.id == person_id` — **a different person**.

Profiled from `.dumps/eko_phppos_2026-05-06.sql` (full dump):

| Metric | Value |
|---|---|
| sales rows / customers rows | 13,561 / 13,279 |
| `sale.customer_id ∈ customers.person_id` | 4839/4839 distinct (100%) |
| `customers.id == person_id` (per row) | 0/13,279 |
| `sale.customer_id` also exists as some `customers.id` (→ mis-link) | 4373/4839 (90%) |
| `person_id` unique among non-deleted customers | 12,306/12,306 (yes) |
| `sale.customer_id` resolvable to a non-deleted customer | 4807/4839 (99.3%) |
| sales with NULL/0 customer_id (anonymous) | 8/13,561 (0.1%) |

**Second defect, same root cause:** `excluded_customer_ids` is built from `customers.id` (`eko/sales.py` + `speedzone/sales.py` `_fetch_employee_customer_ids`) but compared against `sale.customer_id` (a person_id) inside `fetch_phppos_sales` — so the employee-exclusion filters the wrong rows.

Fundbox is **not** affected: `orders.user_id` → `users.id` directly, and the fundbox identity connector keys on `users.id`. No id/person_id indirection.

## Fix — Option A: re-key customer identity records by `person_id`

> **Superseded approach (rejected):** a first attempt translated `sale.customer_id` (person_id) → `customers.id` inside the **live** sales connector (`phppos_sales_common.py`). A reset-and-ingest revealed it was incomplete — **dump-mode** ingestion (the actual local/dev + dump_path path) goes through `connectors/dumps/connectors.py`, whose sales connector loads only `PHPPOS_SALES_TABLES` (no `phppos_customers`), so it cannot translate locally. That path still mis-linked (verified: speedzone sale-2, person_id 35, linked to `customer-35` = the customer whose *id*=35, person_id 128 — wrong person).

The root cause is that customer **identity** records are keyed on `customers.id` while **sales** reference `person_id`. The clean, universal fix makes both agree on **`person_id`** — no translation map, works identically in live and dump mode. `person_id` is unique among non-deleted customers (verified in full dumps: eko 12,306/12,306, speedzone 4,158/4,158), so it is a safe key.

### 1. Identity record key → `person_id`

- `connectors/eko/connector.py:EkoConnector._build_one` and `connectors/speedzone/connector.py` (`_build_envelope_with_customer`): `source_record_id = f"{source}-customer-{row.person_id}"` (was `row.customer_id` = `customers.id`). `row.person_id` is available in both the live SQL query (`people.c.person_id`) and the dump join (`_join_eko_row`/`_join_speedzone_row` spread `**person.as_dict()`). The dump connectors reuse `_build_one`, so both modes are fixed by this one change.

### 2. Sales link uses `person_id` directly (no map)

- `connectors/phppos_sales_common.py` (live) and `connectors/dumps/connectors.py:_build_phppos_sales_envelope` (dump) both set `identity_source_record_id = f"{source}-customer-{sale.customer_id}"` (person_id) — now matching the identity key. The dump connector already did this (no change); the live connector's translation map is **reverted**.

### 3. Employee-exclusion defect (independent, kept)

- `connectors/eko/sales.py` + `connectors/speedzone/sales.py`: the duplicated employee-exclusion is centralized into `phppos_sales_common.fetch_employee_person_ids(conn, *, customers_t, employees_t, existing_tables)`, which selects `customers.c.person_id` (not `customers.c.id`) so the excluded set matches `sale.customer_id` (a person_id). Param `excluded_customer_ids` → `excluded_person_ids`.

## Testing

- **`test_dump_connectors.py`**: identity-record assertions re-pointed to the person_id key — `eko_phppos-customer-7` (person_id 7, was customers.id 11) and `speedzone_phppos-customer-8` (person_id 8, was customers.id 12). These exercise the shared `_build_one` so they cover both live and dump modes.
- **`test_phppos_sales_exclusions.py`**: updated for the `excluded_person_ids` rename; still asserts the sales `WHERE` clause.
- Removed the interim `test_phppos_sales_customer_mapping.py` (the translation map it tested no longer exists).
- **Reset-and-ingest confirmation**: after re-ingest, no phppos sale links to the wrong person; sales whose customer person_id is present resolve correctly; sales referencing an absent/deleted customer stay `pending_customer` (orphan).

## Verification

- `uv run --package profile-unifier-ingestion ruff check` + `mypy --strict services/ingestion/src`.
- `uv run pytest services/ingestion/tests` green.
- Dev reset-and-ingest of `eko_phppos` + `eko_phppos:sales` (limited-100): sales `FOR_CUSTOMER_RECORD` edges resolve to the **same** Person the customer identity record created (spot-check a known person_id↔customers.id pair).

## Deferred (separate follow-up)

- **Phone+name attach-only fallback** for the ~0.7% sales whose customer is deleted/missing and the 0.1% anonymous orders: carry the customer's mobile+name in the sales envelope (join `phppos_people` / fundbox `users`), and when the FK resolves no Person, run `MatchEngine` (record_type `sales`) to **attach** the order to an existing match only (never create a Person), registering `sales` in `heuristic._promote_by_record_type`. Decisions already taken: attach-only, phone+name only.
