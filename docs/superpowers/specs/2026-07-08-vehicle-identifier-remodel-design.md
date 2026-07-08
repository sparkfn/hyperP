# Vehicle Identifier Remodel — Design

**Date:** 2026-07-08
**Supersedes:** `2026-05-14-machine-unit-graph-design.md`, `2026-05-18-machine-unit-product-identity-design.md`, `2026-06-16-sales-machine-unit-matching-design.md` (the MachineUnit model is removed; their matching/extraction intent is reworked here for vehicles).

## Goal

Replace the `MachineUnit` node and its relationships with a `Vehicle` node scoped to
**vehicle products only**. A Vehicle's unique identity is the product SKU plus a serial
number or an LTA tag. Non-vehicle product details (merchant, product name, serial, category,
model, etc.) are denormalized onto the `Order` node instead. Sales-to-person matching for
unresolved (pending-customer) sales becomes a **vehicle-identity + (mobile OR email)** match
that **auto-links** at ≥0.90 confidence unless an NRIC anti-match blocks it.

## Background — what is being removed

The `MachineUnit` node (introduced 2026-05-14, product-scoped 2026-05-18, sales-matching
2026-06-16, phppos-loyalty display 2026-07-01) is removed entirely:

- Node label `:MachineUnit` and constraint `machine_unit_id_unique` + indexes
  `idx_machine_unit_lta_tag`, `idx_machine_unit_serial_number` (`infra/neo4j/init.cypher`).
- Relationships `INVOLVES_UNIT` (Order→MachineUnit), `BOUGHT_UNIT` (Person→MachineUnit),
  `OWNS_UNIT` (Person→MachineUnit), `MENTIONS_UNIT` (SourceRecord→MachineUnit, chat).
- Ingestion modules `machine_units.py`, `machine_unit_extraction.py`,
  `graph/queries/machine_units.py`, `matching/machine_unit_heuristic.py`.
- Exclusion plumbing `MachineUnitIdentifierKey` / `machine_unit_identifiers`
  (`exclusions.py`, `exclusion_config.py`, `ingestion_config.py`).
- API types `MachineUnitSummary`, `SalesUnitSummary` (`types.py`) and mappers
  `_map_machine_units`, `_map_sales_summary`; frontend2 `MachineUnitsSidebarCard`.
- The pending-customer → review-only MachineUnit candidate flow.

The review-only matching posture (capped in `[0.60, 0.90)`, never auto-merge) is **replaced**
by an auto-link posture for the new vehicle path (§5).

## Graph model

### `:Vehicle` node

A real-world vehicle observed across one or more sales sources (and, for chat, mentioned).
Properties:

- `vehicle_id` — internal UUID, unique (`CONSTRAINT vehicle_id_unique`).
- `normalized_lta_tag` — normalized LTA tag / plate when available (uppercase, separators stripped).
- `normalized_serial_number` — normalized serial / chassis number when available.
- `product_sku` — the creating observation's source product SKU (string). For a cross-source
  LTA Vehicle observed by multiple sources, the per-source SKUs are recorded in
  `observed_product_skus` (a list of `{source_system_key, sku}`); `product_sku` itself stays the
  first-observed SKU for display.
- `observed_product_skus` — list of `{source_system_key, sku}` (one per observing source);
  empty for a per-source serial-only Vehicle until it gains another observation.
- `product` — display product name (first-observed; per-source names could be recorded
  analogously if needed — out of scope for MVP).
- `manufacturer`, `model` — when available.
- `source_systems` — list of source-system keys that have observed this Vehicle.
- `conflict_flag`, `conflict_reason` — identifier/ownership conflict flags.
- `created_at`, `updated_at`.

At least one of `normalized_lta_tag` / `normalized_serial_number` must be present, together
with a `product_sku`, to create a Vehicle (the identity needs both).

### Relationships

| Relationship | Direction | When |
|---|---|---|
| `INVOLVES_VEHICLE` | `Order -[:INVOLVES_VEHICLE]-> Vehicle` | a sales order's line involves this vehicle |
| `BOUGHT_VEHICLE` | `Person -[:BOUGHT_VEHICLE]-> Vehicle` | the resolved buyer purchased this vehicle |
| `OWNS_VEHICLE` | `Person -[:OWNS_VEHICLE]-> Vehicle` | explicit ownership assertion only (never inferred from purchase) |
| `MENTIONS_VEHICLE` | `SourceRecord -[:MENTIONS_VEHICLE]-> Vehicle` | chat inquiry mentions an existing vehicle (chat never creates Vehicles) |

Relationship properties mirror the old MachineUnit relationships: `source_system_key`,
`source_record_pk`, `source_order_id` (BOUGHT), `observed_at`, `is_active`, `last_confirmed_at`,
`confidence`, `quality_flag`, `conflict_flag` as applicable.

### Ownership conflicts

`OWNS_VEHICLE` conflict handling is unchanged in spirit from `OWNS_UNIT`: when ≥2 active
`OWNS_VEHICLE` from different active Persons point to the same Vehicle, flag the Vehicle
`conflict_flag=true, conflict_reason='multiple_active_owners'` and flag the relationships.
Do not auto-resolve.

## Vehicle identity — cross-source via LTA, per-source via serial

The user's identity formula is "product SKU + (serial OR LTA tag)". Product SKUs are
**source-local** (eko's `EB-2001` ≠ fundbox's `EB-2001`), so they cannot unify across
sources by themselves. The only identifier that is genuinely cross-source is the **LTA tag**
(Singapore-wide unique plate/tag). The resolution is therefore:

1. **LTA tag present → global identity.** Match/merge the Vehicle across ALL sources by
   `normalized_lta_tag`. The first source to observe it creates the Vehicle; later sources
   attach their `product_sku`, `product`, `manufacturer`, `model`, and `source_system` as
   additional observations on the same node. This is the cross-source unifier.
2. **Serial only (no LTA) → per-source identity.** Match within the source by
   `(source_system_key, product_sku, normalized_serial_number)`. No cross-source merge on
   serial alone — SKUs and serials are not safely comparable across sources without a shared
   product authority. A serial-only Vehicle carries `source_systems = [that one source]`.
3. **Promotion.** A serial-only Vehicle that later gains an LTA tag (a later observation, or
   the same observation carrying both) is reconciled onto the existing LTA-keyed Vehicle:
   - If the LTA matches exactly one existing Vehicle → merge observations onto it.
   - If the LTA matches one Vehicle and the serial matches a different Vehicle → do not
       auto-merge; flag a Vehicle identifier conflict for review (reuse the current
       MachineUnit conflict-flagging logic, renamed).

This realizes "cross-source unified" for the identifier that actually crosses sources (LTA),
while keeping the serial+SKU path honest. A Vehicle that never acquires an LTA stays
per-source; a Vehicle that has an LTA is cross-source.

**Neo4j schema for identity:**
- `CONSTRAINT vehicle_lta_unique IF NOT EXISTS FOR (v:Vehicle) REQUIRE v.normalized_lta_tag IS UNIQUE`
  (applies only when LTA is present — Neo4j enforces uniqueness among non-null values, so
  serial-only Vehicles with null LTA are not constrained).
- `INDEX idx_vehicle_serial FOR (v:Vehicle) ON (v.normalized_serial_number)` — supports
  the per-source serial lookup (the query additionally filters by `source_system_key` + `product_sku`).
- `CONSTRAINT vehicle_id_unique FOR (v:Vehicle) REQUIRE v.vehicle_id IS UNIQUE`.

## Vehicle classification rule (per source)

A sales line creates a Vehicle **iff** (a) its product category is in the per-source
vehicle-category allowlist, AND (b) the line carries a non-empty `serial_number` OR `lta_tag`.
Everything else → non-vehicle → Order enrichment (§Order enrichment). The serial/LTA presence
is required for the identity anyway and also disambiguates the speedzone brand-category noise
(accessories filed under `Honda`/`Yamaha` don't carry a serial).

Vehicle-category allowlists are per-source config (auditable/editable), keyed by **category
name**, resolved at ingestion from the item's `category_id` via the source's categories table.
The allowlists below are derived from examining the full dumps (`.dumps/`, 2026-05-06).

| Source | Identity fields | Vehicle categories | Connector gaps to close |
|---|---|---|---|
| **eko** (`eko_phppos`) | sku=`phppos_items.item_number`; serial=`phppos_sales_items.serialnumber`; LTA = same `serialnumber` column (PMDs/e-bikes; "serial" is the LTA-equivalent identifier for eko) | `Bicycles, Foldable/Bi-fold/Tri-fold, Hybrid, Mountain, City, Road, Electric Bicycles, Electric Scooters, Personal Mobility Aids, Electric Wheelchairs, Rollators, Wheelchairs, Seated/Standing Electric Scooters, Foldable Electric Bicycles, Ji-Move, Mobot, YY Scooter, Soomax` (+ `Used *` variants) | none (read `phppos_categories` for name resolution) |
| **speedzone** (`speedzone_phppos`) | sku=`item_number`; serial=`phppos_sales_items.serialnumber`; **LTA plate from the customer** `phppos_customers.custom_field_8_value` (Bike Plate 1) / `custom_field_10_value` (Bike Plate 2) | bike-type cats `New/Used Motorbike, Road Bike, Scooter, Sport Bike, Scrambler, Cafe Racer, Tourer, Cruiser` (+ `Used *`); brand cats (`Honda`, `Yamaha`, `Aprilia`…) count only when the line carries a serial (disambiguates brand-accessory noise) | emit customer bike plate as the line's LTA |
| **fundbox** (`fundbox_consumer_backend`) | sku=`product_variants.sku`; serial=`order_items.serial_no`; LTA=`order_items.lta_tag`; merchant=`orders.merchant_id → merchants.name` | PMD/e-bike/e-wheelchair product categories (`Electric Scooters, Personal Mobility Aids, Electric Wheelchairs, Mobility Scooters, Power Assisted Bicycles, Electric Bicycles, …`) reached via `order_items → merchant_products → product_variants → products` (category), gated by `products.has_serial_number` / `products.has_lta_tag` | read `products.has_serial_number`/`has_lta_tag` + `merchants.name` (live SQLAlchemy model `fundbox/schema.py` products += those columns; dump reader auto-reflects them) |
| **onediver** (`onediver`) | — (no serial/LTA anywhere in the dump) | **none** — diving gear + courses, no vehicles | extend connector to read `sales_order_items` + `products` for Order enrichment only (no Vehicle) |

Non-vehicle categories per source (explicit non-vehicle, so they're not accidentally
classified): eko — parts/accessories/care/batteries/chargers/services/fees (`Bicycle Parts`,
`Bicycle Locks`, `Bicycle Helmets`, `Bicycle Pumps`, `External Batteries`,
`Electric Bicycle Chargers`, `Bicycle Servicing Packages`, `Delivery Fee`, `Gift Card`,
`Refund`, …); speedzone — `*Accessories`, `Tyres`, `Drivetrain`, `Brake Pads`,
`Labour Service`, `Inspection Service`, `Part & Accessory`, `Chemical & Fluids`, `Battery`,
`Tyre Valve Cap`, sensors, `Bush`, `Nut`, `Box`, etc.

## Order node enrichment (non-vehicle product details)

Add a `non_vehicle_lines` property (JSON array) to the `Order` node, populated for
**non-vehicle lines only** (vehicle lines carry their details on the `Vehicle`). Shape:

```
non_vehicle_lines: [
  {
    "source_line_item_id": "...",
    "sku": "...",
    "product_name": "...",
    "category": "...",
    "manufacturer": "...",            // when available
    "model": "...",                   // when available
    "serial_number": "...",           // when present on a non-vehicle line
    "lta_tag": "...",                 // when present on a non-vehicle line
    "quantity": 1,
    "unit_price": 1599.0,
    "line_total": 1599.0,
    "merchant": "..."                  // fundbox only (merchants.name)
  },
  ...
]
```

The existing `:LineItem` and `:Product` nodes and `CONTAINS` / `OF_PRODUCT` edges are **kept**
(used for structured per-line queries and the vehicle path). `:LineItem` already carries
`metadata`; we additionally persist the canonical fields above on `Order.non_vehicle_lines`
so display and the public share page need no `LineItem`/`Product` traversal.

`MERGE_ORDER` (`graph/queries/sales.py`) gains a `non_vehicle_lines` SET clause. The sales
connector assembles the array per order from the non-vehicle lines. Onediver: the connector
is extended to read `sales_order_items` + `products` to populate this array (no vehicles, no
`INVOLVES_VEHICLE`).

## Sales matching — vehicle + mobile/email, auto-link unless NRIC blocks

Replaces the review-only `machine_unit_heuristic.py`. New flow in `pipeline_sales.py`
(replacing `_propose_one_pending_sale` / `propose_machine_unit_matches_for_pending_sales`,
renamed to vehicle equivalents):

- **FK-first.** When `customer_link.identity_source_record_id` resolves to a Person
  (the existing deterministic path), keep that link unchanged. The vehicle observation is
  still upserted and `INVOLVES_VEHICLE` + `BOUGHT_VEHICLE` are written to that Person.
- **Pending-customer fallback.** When the FK is unresolved (`link_status='pending_customer'`),
  run `FIND_VEHICLE_CANDIDATES_FOR_SALES`: find active Persons that **share a Vehicle
  identity** with the pending sale's vehicle lines AND whose **mobile OR email matches** the
  sale's customer mobile/email.
  - **Auto-link (≥0.90).** Single surviving candidate, no NRIC block → set
    `link_status='linked'`, `MERGE (p)-[:PURCHASED]->(o)`,
    `MERGE (p)-[:BOUGHT_VEHICLE]->(v)`. No review case.
  - **NRIC anti-match (hard block).** If the sale's customer NRIC and the candidate Person's
    NRIC are **both present and differ** → block: do not link; persist a `NO_MATCH`
    `MatchDecision` for the sales↔person pair (engine=heuristic, `signal_source='vehicle'`,
    `reason='nric_anti_match'`) so the pair won't re-propose on subsequent runs, and skip the
    candidate. If either side has no NRIC, there is no block — vehicle identity + a contact
    channel is sufficient. (This is a sales↔person no-match decision, recorded on
    `MatchDecision`/`ReviewCase` per the existing decision model — distinct from the
    person-pair `NO_MATCH_LOCK` used for pair-ordering dedup.)
  - **Ambiguous (multiple candidates).** Demote to a **review case** at the review band
    `[0.60, 0.90)`, as today.

Confidence constants (in the renamed `matching/vehicle_heuristic.py`):
`VEHICLE_MATCH_AUTO = 0.90` (vehicle identity + mobile/email, no NRIC block);
review band `[0.60, 0.90)` for ambiguous multi-candidate cases. The `feature_snapshot`
carries `candidate_person_id, vehicle_id, rel_type, contact_channel_matched ('mobile'|'email'),
nric_blocked (false), signal_source='vehicle'`.

NRIC fields per source: eko/speedzone `phppos_customers.custom_field_1_value`; fundbox
`basic_profiles.nric`; onediver `profiles.ic_number` (and `passport_number`). Mobile/email
per source: eko/speedzone `phppos_people.email` / `phppos_people.phone_number`; fundbox
`users.email` / `users.mobile_number` (+ `basic_profiles`); onediver `profiles.email` /
`profiles.contact_number`.

`CLEAR_SUPERSEDED_SALES_LINKS` (`sales.py`) is updated to delete `INVOLVES_VEHICLE` and
`BOUGHT_VEHICLE` instead of `INVOLVES_UNIT` / `BOUGHT_UNIT` on supersession.

## Chat ingestion

Chat (bitrix/whatsapp) keeps the resolve-and-link posture, renamed to `MENTIONS_VEHICLE`.
Chat **never creates** a Vehicle (unchanged): it resolves an **existing** Vehicle by
`normalized_lta_tag` (global) or by `(source_system_key, product_sku, normalized_serial_number)`
(per-source fallback) and links the conversation `SourceRecord -[:MENTIONS_VEHICLE]-> Vehicle`
only when exactly one Vehicle matches. Only vehicle-category products are resolved — a chat
mention of a non-vehicle product (no Vehicle exists) is ignored. `llm_prompts.py` inquiry
fields are renamed: `machine_product`→`vehicle_product` (or kept as `product` with a vehicle
flag), `lta_tag`, `serial_number`; `weak_identifiers` enum `machine_lta_tag` /
`machine_serial_number` / `machine_unit` → `vehicle_lta_tag` / `vehicle_serial_number` /
`vehicle`.

## Migration / removal of MachineUnit

Source records are immutable and ingestion is idempotent, so we **drop, not convert**:

1. Remove `machine_unit_*` constraints/indexes from `infra/neo4j/init.cypher`; add the
   `:Vehicle` constraints/indexes (§Identity).
2. Delete all `MachineUnit` nodes and their relationships (`INVOLVES_UNIT`, `BOUGHT_UNIT`,
   `OWNS_UNIT`, `MENTIONS_UNIT`) via a one-shot Cypher cleanup (a startup migration or a
   manual script — see Implementation phases). Old MachineUnit nodes lack the new
   cross-source LTA key and the `product_sku`, so conversion would be lossy; re-ingestion
   recreates them correctly.
3. Remove the ingestion/graph/extract/matching/exclusion code for MachineUnit (rename or
   delete per the inventory in the brainstorming notes).
4. Re-ingest the four sales sources to recreate Vehicles, `BOUGHT_VEHICLE`, `OWNS_VEHICLE`,
   `INVOLVES_VEHICLE`, and Order `non_vehicle_lines`.
5. Open review cases referencing `sales_summary`/MachineUnit: the review-case mapper change
   (§API surface) handles the renamed shape; any in-flight review cases with stale
   `sales_units` are closed or migrated by the mapper.

No legacy `/app/v1` or retired-frontend concerns.

## API + frontend surface

### API types (`services/api/src/types.py`)
- `MachineUnitSummary` → `VehicleSummary` (fields: `vehicle_id`, `product_sku`, `product`,
  `lta_tag`, `serial_number`, `relationship` (`OWNS`|`BOUGHT`), `is_active`, `conflict_flag`,
  `observed_at`).
- `Person.machine_units` → `Person.vehicles`.
- `SalesUnitSummary` → `SalesVehicleSummary` (keyed on `vehicle_id`).
- `SalesOrderSummary` gains `non_vehicle_lines: list[NonVehicleLine]` (a new TypedDict/model:
  `source_line_item_id, sku, product_name, category, manufacturer?, model?, serial_number?,
  lta_tag?, quantity, unit_price, line_total, merchant?`).

### API graph queries + mappers
- `graph/queries/persons.py` (`GET_PERSON_BY_ID`): replace `MachineUnit` / `OWNS_UNIT|BOUGHT_UNIT`
  / `machine_units` projection with `Vehicle` / `OWNS_VEHICLE|BOUGHT_VEHICLE` / `vehicles`.
- `graph/queries/review.py` (`GET_REVIEW_CASE`): `INVOLVES_UNIT`→`INVOLVES_VEHICLE`,
  `MachineUnit`→`Vehicle`; `sales_units`→`sales_vehicles`; also collect `non_vehicle_lines`
  from the Order.
- `graph/queries/review.py` (`LINK_REVIEW_SALES_PURCHASED_ORDER`,
  `LINK_REVIEW_SALES_BOUGHT_UNIT`→`LINK_REVIEW_SALES_BOUGHT_VEHICLE`): Vehicle equivalents.
  `MARK_REVIEW_SALES_RECORD_LINKED` / `MARK_REVIEW_SALES_RECORD_UNRESOLVED`: unchanged
  (status flags).
- `graph/mappers.py`: `_map_machine_units`→`_map_vehicles`; `_map_sales_summary` rebuilt for
  `SalesVehicleSummary` + `non_vehicle_lines`; `_map_comparison_entity` /
  `_map_source_record_comparison` / `map_review_case_detail` forward the renamed fields.

### API routes + public page
- `routes/persons.py` `GET /{person_id}` returns `Person` with `vehicles` (shape via types).
- `routes/public_pages.py` `_strip_public_person`: strip `vehicles` + `non_vehicle_lines`
  (customer-specific) from `/v1/public/persons/*` (replaces the `machine_units` strip).
  `_strip_public_sales_order`: strip `non_vehicle_lines` if it carries customer-specific
  detail (merchant/serial). Covered by an explicit test.

### API repositories
- `repositories/neo4j/review.py`: imports + `_sales_link_merge_tx` use the renamed Vehicle link
  queries; the merge-fallback covers both the auto-link path's `BOUGHT_VEHICLE` and the
  review-approve path.

### frontend2
- `src/lib/api-types.ts`: `MachineUnitSummary`→`VehicleSummary`, `Person.machine_units`→
  `Person.vehicles`; add `NonVehicleLine` + `SalesOrderSummary.non_vehicle_lines` (if/when
  consumed).
- `src/app/persons/[personId]/page.tsx`: `MachineUnitsSidebarCard`→`VehiclesSidebarCard`
  ("Vehicles" header, empty-state "No vehicles linked to this person."), field reads
  `vehicle_id`, `product_sku`/`product`, `serial_number`, `lta_tag`, `relationship`,
  `is_active`, `conflict_flag`.
- Legacy v1 (`services/frontend/`) is retired; untouched.

## Limited-100 dump changes (local/dev — not CI)

`.dumps/` is gitignored (`.gitignore:54`, no negation); the limited-100 dumps **and** the
generator are local-only, untracked. The ingestion test suite uses inline/temp fixtures
(`test_dump_connectors.py` writes fixtures to `tmp_path`), so **CI is unaffected**. The
limited-100 dumps only matter for manual/local `docker compose` ingestion. The dump reader
(`load_dump_tables`) auto-reflects all columns from each table's CREATE TABLE (specs use
`None` for column selection), so adding a table name to a spec makes all its columns
available.

**Tracked table-spec changes** (committed code — drive what the dump reader loads + connectors
read):
1. `PHPPOS_SALES_TABLES` (`connectors/dumps/connectors.py:119`) += `phppos_categories`,
   `phppos_customers` — so the phppos sales connector reads category name (vehicle allowlist)
   and the customer's bike plate (`custom_field_8_value`/`custom_field_10_value`, speedzone) +
   NRIC (`custom_field_1_value`, anti-match) at vehicle-write time.
2. `ONEDIVER_SALES_TABLES` (`connectors/onediver/connector.py`) += `sales_order_items`,
   `products` — so onediver Order `non_vehicle_lines` enrichment has data.

**Untracked generator changes** (`.dumps/limited-100/generate_limited_dumps.py`, local):
3. `eko_sales_100` / `speedzone_sales_100` `write_mysql` block (≈lines 257-266): add
   `phppos_categories` (full, small) and `phppos_customers` filtered to the sampled sales'
   `customer_id`s (reuse the `by_int` pattern).
4. `onediver_sales_100` `write_mysql` block (≈lines 447-453): add `sales_order_items`
   (filtered by `sales_order_id`) and `products` (filtered by the `product_id`s in those
   items).

**No change needed:**
5. `fundbox_sales_100` already includes the full product/merchant chain; the first 100 orders
   by id carry populated `lta_tag`/`serial_no` (verified), so Vehicle creation is exercised.
   The phppos sampler already deliberately includes serial-bearing sales (up to 40), so
   eko/speedzone already have vehicle candidates.

**Regenerate (local, one-shot — generation, not verification):**
6. `uv run --package profile-unifier-ingestion python .dumps/limited-100/generate_limited_dumps.py`
   (full dumps present at `.dumps/`). No commit — dumps + generator are gitignored.

**Live-connector counterpart** (tracked, part of the main implementation, not the limited-100
concern): `fundbox/schema.py` products SQLAlchemy model += `has_serial_number`,
`has_lta_tag` (the dump reader auto-reflects them already, so dump mode works without this;
live mode needs it).

## Implementation phases (for the plan)

1. **Graph schema + Vehicle queries** — `infra/neo4j/init.cypher` (remove MachineUnit
   constraints/indexes, add Vehicle ones); `graph/queries/vehicle.py` (rename of
   `machine_units.py`: `UPSERT_VEHICLE` with cross-source LTA + per-source serial logic,
   `LINK_ORDER_INVOLVES_VEHICLE`, `LINK_PERSON_BOUGHT_VEHICLE`, `LINK_PERSON_OWNS_VEHICLE`,
   `LINK_SOURCE_RECORD_MENTIONS_VEHICLE`, `FLAG_VEHICLE_OWNER_CONFLICTS`,
   `RESOLVE_EXISTING_VEHICLE_FOR_CHAT`); `graph/queries/sales.py` (`CLEAR_SUPERSEDED_SALES_LINKS`,
   `FIND_VEHICLE_CANDIDATES_FOR_SALES`, `MERGE_ORDER` += `non_vehicle_lines`); `__init__.py`
   exports.
2. **Ingestion extraction + per-source classification + connector gaps** — `vehicles.py` +
   `vehicle_extraction.py` (rename), with the per-source vehicle-category allowlist config;
   close connector gaps: fundbox read `has_serial_number`/`has_lta_tag` + `merchants.name`
   (live `fundbox/schema.py` += columns), speedzone emit customer bike plate as line LTA,
   onediver read `sales_order_items` + `products`. Table-spec additions (§Limited-100 §1-2).
3. **Order enrichment** — sales connectors assemble `non_vehicle_lines`; `MERGE_ORDER` SET.
4. **Matching / auto-link + NRIC block** — `matching/vehicle_heuristic.py` (rename),
   `pipeline_sales.py` vehicle candidate flow with auto-link-at-0.90 / NRIC-block+lock /
   multi-candidate-review.
5. **Chat** — `pipeline.py` `_write_chat_vehicle_observations` (rename) +
   `MENTIONS_VEHICLE`; `llm_prompts.py` field renames.
6. **API + frontend** — types, queries, mappers, routes, public-page strip, repo,
   frontend2 card.
7. **Migration + re-ingest** — drop MachineUnit nodes/rels, remove old constraints/indexes,
   re-ingest the four sales sources.
8. **Limited-100 regen** (local) — generator edits + regenerate.

## Testing

Mirror current coverage, renamed to Vehicle, plus new cases:

- Vehicle normalization/upsert: LTA-global match across sources; per-source serial match;
  promotion (serial-only → gains LTA → reconciles onto LTA Vehicle); identifier conflict
  when LTA matches one Vehicle and serial matches another.
- Classification allowlist per source: vehicle-category line with serial → Vehicle;
  vehicle-category line without serial → non-vehicle (Order enrichment); non-vehicle
  category line → non-vehicle even with a serial; speedzone brand-category line with serial →
  Vehicle, brand-category accessory without serial → non-vehicle.
- Order `non_vehicle_lines` population: non-vehicle lines denormalized; vehicle lines
  excluded; fundbox merchant included; onediver line items + products read.
- `FIND_VEHICLE_CANDIDATES_FOR_SALES` + auto-link: single-candidate auto-link at 0.90
  (sets `linked`, PURCHASED, BOUGHT_VEHICLE, no review case); NRIC-mismatch → block +
  `NO_MATCH` decision (does not re-propose); NRIC absent on one side → no block, auto-link;
  multi-candidate → review case at review band; FK-resolved sale → FK link, vehicle
  observation still written.
- `CLEAR_SUPERSEDED_SALES_LINKS` deletes `INVOLVES_VEHICLE`/`BOUGHT_VEHICLE`.
- Chat `MENTIONS_VEHICLE`: resolves exactly-one existing Vehicle; no creation; non-vehicle
  mention ignored.
- API mappers: `_map_vehicles`, `_map_sales_summary` with `sales_vehicles` +
  `non_vehicle_lines`; review-case detail carries vehicle summary.
- Public-page strip: `vehicles` + `non_vehicle_lines` nulled on `/v1/public/persons/*`;
  `_strip_public_sales_order` strips customer-specific detail.
- frontend2 `VehiclesSidebarCard` renders `vehicle_id`/`product`/`serial`/`lta`/`relationship`.
- Exclusion plumbing renamed to `VehicleIdentifierKey` / `vehicle_identifiers` (exclusion
  tests updated).

Verification is via the Woodpecker PR/DEV pipeline (`wpci home`), not host-side lint/test
runs, per the repo's CI policy. Limited-100 regeneration is a one-shot local generation step
(allowed as "generating, not verifying").

## Out of scope

- Forcing cross-source vehicle matching on serial+SKU without an LTA (rejected — false
  merges; serial-only Vehicles stay per-source).
- Removing the `:LineItem` / `:Product` nodes (kept).
- A separate `VehicleModel`/`Product` link for vehicles beyond the existing `:Product` node.
- Migrating in-flight review cases beyond the mapper rename (close-or-migrate is enough).
- Retired v1 frontend.
- Staging/production use of `limited-100` dumps (local/dev only, per repo policy).