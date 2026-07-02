# phppos loyalty points + machine-unit display — design

**Date:** 2026-07-01
**Branch:** `feat/phppos-loyalty-machine-units`
**Scope:** Capture loyalty-points data from the phppos sources (Eko, SpeedZone) into the graph and surface loyalty points + machine units on the person profile in frontend2.

## Context & findings

The phppos schema (shared by Eko and SpeedZone via `PHPPOS_TABLES`) already carries a full loyalty-points subsystem in the source dumps:

| Table | Loyalty columns |
|---|---|
| `phppos_customers` | `points`, `disable_loyalty`, `current_spend_for_points`, `current_sales_for_discount` |
| `phppos_sales` | `points_used`, `points_gained`, `did_redeem_discount`, `is_purchase_points` |
| `phppos_sales_items` | `loyalty_multiplier` |
| `phppos_items` | `disable_loyalty`, `loyalty_multiplier` |

**The limited-100 dumps already contain these columns.** `generate_limited_dumps.py` round-trips entire rows without column filtering, and the dump reader (`dumps/reader.py`) parses them from the embedded `CREATE TABLE`/`INSERT` headers (confirmed by grep of `eko_customers_100.sql` / `eko_sales_100.sql` and the speedzone equivalents). **No dump changes are required** — the gap is entirely on the ingestion-mapping and graph/display side.

**Machine units are already written for phppos sales.** `pipeline_sales.py:_write_machine_unit_observations` calls `observations_from_sales_lines` (extracts `lta_tag`/`serialnumber` from line-item metadata + product name) and writes `Order-[:INVOLVES_UNIT]->MachineUnit` and `Person-[:BOUGHT_UNIT]->MachineUnit` (plus `OWNS_UNIT` from other sources). `:MachineUnit` carries `machine_unit_id`, `machine_product`, `lta_tag`, `serial_number`, `conflict_flag`, `conflict_reason`. So the graph data for a person-profile display already exists — this design only exposes and renders it.

The ingestion-side machine-unit **matching heuristic** described in `docs/superpowers/plans/2026-06-16-sales-machine-unit-matching.md` (pending_customer sales → ReviewCase) is **out of scope** for this work; we only display owned/bought units on the profile.

## Decisions (confirmed with user)

1. **Loyalty scope:** capture **both** the per-customer balance (`phppos_customers.points` etc.) **and** per-sale activity (`phppos_sales.points_used`/`points_gained` etc.).
2. **Machine-unit display scope:** person-profile owned/bought units only — no review-comparison wiring.
3. **Balance modeling:** **source-record read-through display** — loyalty balance is stored inside the identity `SourceRecord`'s `raw_payload` (which is persisted verbatim as JSON on the node — unlike `attributes`, which is flattened into `NormalizedAttribute` triples and would mangle a dict). The API reads the latest per-source-system `raw_payload.loyalty` block and surfaces it on `Person`. It does **not** enter the golden-profile survivorship / merge-picker / field-options-override machinery, and is **not** returned by the public share page (new exclusion code, see §4 — note `preferred_race_ethnicity` is currently *not* excluded from public either; this design adds a real strip for loyalty/machine_units/sales-points).

**Ingestion trace facts (verified):** `build_envelope` (`connectors/fundbox/builders.py:283`) stores `attributes` and `raw_payload` as sibling JSON keys on the envelope. The pipeline (`pipeline_normalization.py:173`) flattens `attributes` via `normalize_envelope_attributes` (dropping `address`, passthrough-normalizing everything else into `{attribute_name, attribute_value, quality_flag}` triples) and embeds that list inside `normalized_payload`. `CREATE_SOURCE_RECORD` (`graph/queries/source_records.py:49-69`) sets only `raw_payload` (verbatim JSON string) and `normalized_payload` on the node — **no dedicated `attributes` property**. Therefore the loyalty balance must live in `raw_payload`, not `attributes`.

## Design

### 1. Dumps (steps 1 & 2)

No code changes. Deliverable is verification only: re-grep the four phppos limited-100 dumps to confirm loyalty columns are present, and record the finding in the commit message. The generator's row-round-trip behavior means any future column additions to the source dump flow through automatically.

### 2. Ingestion mapping (step 3)

#### 2a. Identity — customer balance (live connectors)

`services/ingestion/src/connectors/eko/connector.py` and `services/ingestion/src/connectors/speedzone/connector.py`:

- Add to the SQLAlchemy select list (after `customers.c.company_name`):
  - `customers.c.points`
  - `customers.c.disable_loyalty`
  - `customers.c.current_spend_for_points`
  - `customers.c.current_sales_for_discount`
- In `_build_one` / `_build_envelope_with_customer`, add a `loyalty` block to the `raw_payload` dict (sibling to `person`), **not** to `attributes`:
  ```python
  raw_payload={
      "person": _person_raw_payload(row),
      "loyalty": {
          "points": to_int_or_none(row.points),
          "disable_loyalty": bool(row.disable_loyalty) if row.disable_loyalty is not None else None,
          "current_spend_for_points": to_float_or_none(row.current_spend_for_points),
          "current_sales_for_discount": to_float_or_none(row.current_sales_for_discount),
      },
  }
  ```
  (`to_int_or_none`/`to_float_or_none` from `graph/converters.py`.) `raw_payload` is persisted verbatim as JSON on the `SourceRecord` node, so the block survives intact and is queryable. `attributes` is left as `{full_name, dob, address}` — unchanged.

The person-only fallback path (`_build_records_from_people_only`, `connector.py:135`) does **not** carry loyalty (no customer row) — its envelope omits the `loyalty` attribute, which the API treats as "no loyalty data for this source."

#### 2b. Identity — dump-path join parity

`services/ingestion/src/connectors/dumps/connectors.py`:

- `_join_eko_row` (line ~797) and `_join_speedzone_row` (line ~818) currently copy `customer_id`, `account_number`, `company_name`, `custom_field_1..10` from the `phppos_customers` dict. Add `points`, `disable_loyalty`, `current_spend_for_points`, `current_sales_for_discount` to the copied fields so the joined row reaches the same `_build_one` / `_build_envelope_with_customer` builder, which then produces the same `loyalty` attribute block. The dump-side `phppos_customers` rows are parsed with these columns present (verified), so no reader change is needed.

#### 2c. Sales — per-sale activity (shared mapper)

`services/ingestion/src/connectors/phppos_sales_common.py:_build_order_payload` (line 258):

Add a `loyalty` block to the returned order dict, using the existing defensive `_col_or_none(sale, attr, sales_cols)` accessor so dumps with varying column sets still work:
```python
"loyalty": {
    "points_used": _col_or_none(sale, "points_used", sales_cols),
    "points_gained": _col_or_none(sale, "points_gained", sales_cols),
    "did_redeem_discount": _col_or_none(sale, "did_redeem_discount", sales_cols),
    "is_purchase_points": _col_or_none(sale, "is_purchase_points", sales_cols),
},
```
(`raw` already round-trips the full sale row; this block surfaces the four fields structurally.)

Mirror identically in the dump-side `_build_phppos_sales_envelope` (`dumps/connectors.py:~720`) so live + dump produce identical envelope shapes.

Line-item `loyalty_multiplier` and item-level loyalty fields are **not** captured structurally in this phase — they remain in the line-item/product `raw` payloads only. (YAGNI: no display need identified for per-line multipliers.)

### 3. Graph storage

- **Identity SourceRecord** (`graph/queries/source_records.py:CREATE_SOURCE_RECORD`): **no change.** The `loyalty` block rides inside `raw_payload` (already SET verbatim as a JSON string at `source_records.py:62`). No new node property, no schema change, no `attributes` involvement.
- **Order node** (`graph/queries/sales.py:MERGE_ORDER`, line 54) + `_merge_order` (`pipeline_sales.py:370-388`) + `_OrderPayload` TypedDict (`pipeline_sales.py:70-79`): add `points_used` (int), `points_gained` (int), `did_redeem_discount` (bool), `is_purchase_points` (bool) — all nullable — to the TypedDict, pass them from `order.get("loyalty", {})` in `_merge_order`, and add them to the `MERGE_ORDER` `SET` clause. Source values come from the order payload's `loyalty` block (§2c). Use null-safe assignment so sales without the columns (older dumps) don't error.
- **MachineUnit** edges (`OWNS_UNIT`/`BOUGHT_UNIT`/`INVOLVES_UNIT`): already written; **no change**.

### 4. API (step 4, backend half)

`services/api/src/types.py`:

- New `LoyaltySummary` model:
  ```python
  class LoyaltySummary(BaseModel):
      source_system: str
      points: int | None
      disable_loyalty: bool | None
      current_spend_for_points: float | None
      current_sales_for_discount: float | None
      observed_at: str | None
  ```
- New `MachineUnitSummary` model:
  ```python
  class MachineUnitSummary(BaseModel):
      machine_unit_id: str
      machine_product: str | None
      lta_tag: str | None
      serial_number: str | None
      relationship: Literal["OWNS", "BOUGHT"]
      is_active: bool | None
      conflict_flag: bool | None
      observed_at: str | None
  ```
- `Person` model: add `loyalty: list[LoyaltySummary] | None` and `machine_units: list[MachineUnitSummary] | None`.
- Sales order view type (the `SalesOrder`-equivalent returned by `GET /persons/{id}/sales`): add `points_used: int | None`, `points_gained: int | None` (and optionally `did_redeem_discount`).

`services/api/src/repositories/neo4j/person.py` (or the person repo's detail fetch):

- Add a read that collects, for the person's identity `SourceRecord`s of `record_type='identity'`, the latest `raw_payload.loyalty` block per `source_system` (order by `observed_at` desc). The query returns `source_system`, `observed_at`, and the `raw_payload` JSON string; the mapper parses the JSON in Python and extracts the `loyalty` dict (skip records whose `raw_payload` has no `loyalty` key). Map to `LoyaltySummary`.
- Add a read of `(:Person {person_id})-[rel:OWNS_UNIT|BOUGHT_UNIT]->(u:MachineUnit)` returning `MachineUnitSummary` rows (rel type, `rel.is_active`, `u.conflict_flag`, `u.machine_product`, `u.lta_tag`, `u.serial_number`, `rel.observed_at`).
- Both reads are folded into the existing `GET_PERSON_BY_ID` query (`services/api/src/graph/queries/persons.py:40-78`) as additional `CALL {}` sub-queries / `OPTIONAL MATCH` (mirroring the existing `lifetime_value` sub-query pattern), and mapped in `map_person` (`services/api/src/graph/mappers.py:98`). New Cypher constants added to `services/api/src/graph/queries/persons.py`.
- **`Person` protocol** (`repositories/protocols/person.py`): the `get_by_id` return type is `Person | None`; since the new fields live on the `Person` model itself (not new repo methods), the protocol needs no new method — only the `Person` dataclass/model gains the fields.

Routes (`services/api/src/routes/persons.py`):

- `GET /persons/{id}`: include `loyalty` + `machine_units` in the `Person` response (envelope-wrapped). These are read-through; no survivorship/override endpoints.
- The sales endpoint (`services/api/src/routes/person_sales.py` + `repositories/neo4j/sales.py:get_person_sales` + `graph/queries/sales.py:GET_PERSON_SALES` + `graph/mappers_sales.py:map_sales_order`): extend the Cypher projection (`o.points_used AS points_used, o.points_gained AS points_gained`), the mapper, and the `SalesOrder` view type (`types_sales.py`).
- **Public share page exclusion (new code):** `GET /v1/public/persons/{token}` (`routes/public_pages.py:77-89`) currently reuses `repo.get_by_id` + `map_person` and returns the full `Person` (it does **not** strip anything today — `preferred_race_ethnicity` is also currently exposed). Implement a real strip: after fetching the `Person` for the public route, return `person.model_copy(update={"loyalty": None, "machine_units": None})`. For the public sales endpoint (`public_pages.py:135-144`, reuses `get_person_sales`/`map_sales_order`), strip the points fields via a public-side mapper or `model_copy` on each `SalesOrder` (set `points_used=None, points_gained=None`). Add a dedicated test asserting the public responses omit these fields.

`repositories/protocols/person.py`: extend the `Person` protocol return shape if the protocol is typed concretely (likely a TypedDict/dataclass update).

### 5. Frontend (step 4, frontend2 half)

`services/frontend2/src/lib/api-types.ts`:

- Mirror `LoyaltySummary`, `MachineUnitSummary`.
- Add `points_used?: number | null` / `points_gained?: number | null` to `SalesOrder` (the field name as the API exposes it).

`services/frontend2/src/app/persons/[personId]/page.tsx`:

- Add a **Loyalty** card near the profile header / key-fields grid (alongside the existing summary cards). Renders one row per `LoyaltySummary`: source system chip · points balance (large) · "disabled" badge if `disable_loyalty` · `current_spend_for_points` / `current_sales_for_discount` as secondary stats · "last observed {date}" via the existing `formatDob`/`formatDate` helpers. Empty state: "No loyalty data."
- Add a **Machine units** card: table with columns Product · LTA tag · Serial · Relationship (chip: OWNS/BOUGHT) · Active (badge) · Conflict (warning icon + tooltip from `conflict_reason` if present) · Observed. Empty state: "No machine units linked."
- Both sections are display-only (no actions, no editing) and consistent with the existing card styling (`sx` / theme tokens, per-component MUI imports).

Sales tab expanded view (`SalesTab`, `page.tsx:2607-2799`):

- Add **Points used** and **Points gained** to the expanded order header or as two chips next to the total badge. Source from `order.points_used` / `order.points_gained`. Hide both when null (sales without loyalty data).

BFF (`services/frontend2/src/app/bff/persons/[personId]/`):

- The existing person BFF handler passes the `Person` body through unchanged — new fields flow automatically as long as the API returns them in the envelope `data`. Verify `apiFetch`/`bffFetch` don't strip unknown keys (they don't, per the envelope contract). No new BFF route needed unless I split machine-units into a sub-resource (not planned).

### 6. Tests

Per repo policy, tests are written but **not run on the host** — pushed to the PR branch and validated via Woodpecker (`wpci home`).

Ingestion (`services/ingestion/tests`):
- `test_eko_loyalty_envelope` / `test_speedzone_loyalty_envelope`: assert the `loyalty` attribute block is present and correctly typed in a built identity envelope (live builder, with a fake row carrying `points` etc.).
- `test_eko_loyalty_dump_join`: assert `_join_eko_row` / `_join_speedzone_row` copy the four loyalty columns onto the joined row.
- `test_phppos_sales_loyalty_block`: assert the order payload `loyalty` block is populated (live + dump envelope builders) and that `_col_or_none` returns None when columns are absent.
- `test_sales_order_node_loyalty`: assert `MERGE_ORDER` writes `points_used`/`points_gained`/`did_redeem_discount`/`is_purchase_points` onto the `:Order` node (Cypher execution test against the existing test harness).

API (`services/api/tests`):
- `test_person_loyalty_and_machine_units`: assert `GET /persons/{id}` returns `loyalty` (one entry per source system with a loyalty block) and `machine_units` (from OWNS/BOUGHT edges).
- `test_public_person_excludes_loyalty_machine_units`: assert the public share endpoint omits `loyalty`, `machine_units`, and points fields on sales.
- `test_sales_endpoint_includes_points`: assert `GET /persons/{id}/sales` orders carry `points_used`/`points_gained`.

Frontend: typecheck-only (CI runs `tsc --noEmit` + eslint errors-only). No new runtime tests.

### 7. Out of scope

- Per-line `loyalty_multiplier` and item-level loyalty fields (remain in `raw` only).
- Golden-profile survivorship / merge-picker / field-options-override for loyalty balance.
- The machine-unit matching heuristic + review-comparison display from `2026-06-16-sales-machine-unit-matching.md` (separate effort).
- Loyalty identifier (`IdentifierType.LOYALTY_ID`) population — not present in phppos source columns; left for a future source that carries a loyalty membership number.
- Live `batch`-mode SSH-gateway path for eko/speedzone: loyalty columns are added to the same SQLAlchemy select, so the live path is covered; no separate work.

## File map

**Ingestion (modify):**
- `services/ingestion/src/connectors/eko/connector.py` — select list + `loyalty` attribute block + raw_payload
- `services/ingestion/src/connectors/speedzone/connector.py` — same
- `services/ingestion/src/connectors/dumps/connectors.py` — `_join_eko_row` / `_join_speedzone_row` loyalty columns; `_build_phppos_sales_envelope` loyalty block
- `services/ingestion/src/connectors/phppos_sales_common.py` — `_build_order_payload` loyalty block
- `services/ingestion/src/graph/queries/sales.py` — `MERGE_ORDER` loyalty properties
- (verify) identity ingestion path that writes `attributes` JSON onto `SourceRecord`

**API (modify):**
- `services/api/src/types.py` — `LoyaltySummary`, `MachineUnitSummary`, `Person.loyalty`, `Person.machine_units`, sales order points fields
- `services/api/src/repositories/neo4j/person.py` — loyalty + machine-unit reads
- `services/api/src/repositories/protocols/person.py` — protocol return shape
- `services/api/src/graph/queries/persons.py` (or equivalent) — new read Cypher
- `services/api/src/graph/mappers*.py` — map loyalty + machine-unit rows
- `services/api/src/routes/persons.py` — include fields; public-page exclusion
- `services/api/src/routes/public_pages.py` — exclusion

**Frontend (modify):**
- `services/frontend2/src/lib/api-types.ts` — type mirrors
- `services/frontend2/src/app/persons/[personId]/page.tsx` — Loyalty card, Machine units card, Sales tab points chips

**Tests (new):**
- `services/ingestion/tests/test_*loyalty*.py`, `test_*sales_loyalty*.py`
- `services/api/tests/test_*loyalty*.py`, `test_*machine_units*.py`

## Risks

- **`attributes` round-trip on SourceRecord:** if the identity ingestion path currently only persists a known subset of `attributes` keys (e.g. flattens `full_name`/`dob`/`address` into dedicated node properties and drops the rest), the `loyalty` block would be lost. Mitigation: verify during implementation; if needed, store `loyalty` as a JSON string property on the `SourceRecord` node (e.g. `sr.loyalty`) and read it back directly.
- **Public-page leakage:** loyalty balance and machine-unit ownership are customer-specific; must be stripped from all `/v1/public/persons/*` responses. This is **new** code (the public endpoint currently strips nothing). Covered by an explicit test.
- **Defensive column access:** the dump column set varies across historical dumps; all loyalty reads use `_col_or_none` / SQLAlchemy column-existence guards so older dumps without `points` don't crash ingestion.
- **`attributes` round-trip (resolved):** the original concern that `attributes` might be flattened/dropped is moot — loyalty lives in `raw_payload` (verbatim), not `attributes`. No graph schema change needed for the balance.