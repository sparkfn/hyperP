# Vehicle Identifier Remodel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `MachineUnit` node/relationships with a `Vehicle` node (vehicle products only, cross-source identity via LTA tag), denormalize non-vehicle product details onto `Order.non_vehicle_lines`, and auto-link unresolved sales to persons via vehicle-identity + (mobile|email) unless an NRIC anti-match blocks it.

**Architecture:** Rename/rework the ingestion `machine_units` stack into a `vehicles` stack with a per-source vehicle-category allowlist and a cross-source LTA / per-source serial identity upsert. Add `non_vehicle_lines` JSON to `Order`. Replace the review-only pending-customer heuristic with an auto-link-at-0.90 heuristic gated by an NRIC anti-match `NO_MATCH` decision. Mirror the rename through the API types/queries/mappers/routes and frontend2.

**Tech Stack:** Python 3.12 (uv workspace: `profile-unifier-api`, `profile-unifier-ingestion`), FastAPI, Neo4j 5.26 (Cypher), Celery, ruff + mypy --strict; TypeScript/Next.js 15 + MUI 6 (`frontend2`); Woodpecker CI (PR + DEV) via `wpci home`.

## Global Constraints

- **Strict typing:** every Python binding has a concrete type; `mypy --strict` clean; no `Any`/`cast(Any, …)`/`object` placeholders. Known pre-existing `Any` in `types_sales.py`/`types_requests.py` are not regressions.
- **TS strict:** `strict`, `noUncheckedIndexedAccess`, `noImplicitOverride`; no `any`/`as any`/`as unknown as T`; exported functions/components declare return types (`ReactElement`, `Promise<NextResponse>`, `Promise<void>`).
- **Verification gate is Woodpecker CI, not host runs.** Do NOT run `uv run pytest`, `npm run typecheck|lint|build`, or migrations on the host to verify. After each task: commit on a PR branch off the current branch (never `main`/`origin/main`), push, and read the verdict via `wpci home pipeline show sparkfn/hyperP <n>` (PR pipeline = ruff + mypy --strict + pytest + frontend2 typecheck + eslint errors-only; DEV = + production `next build`). Local structural checks (`git diff --check`, `git status -sb`) are allowed.
- **Targeted tests over strict TDD:** per repo norm, write the test alongside the implementation for novel logic; push and let CI verify. Do not block every trivial rename on a separate red-then-green push.
- **Frontend lint budget:** `npm run lint` is `eslint src --max-warnings 9` and is red on a clean tree (~18 pre-existing `react-hooks/set-state-in-effect` warnings). PR/DEV CI run `npx eslint src` (errors only). Verify your change adds **zero net warnings** (stash + compare), not a green local exit. Never `eslint-disable-next-line` for a `useEffect` that only calls a callback prop.
- **Cypher in query modules:** all Cypher lives in `services/*/src/graph/queries/*.py` as module-level constants/builders; routes/repos import by name. `E501` line-length is disabled in query modules.
- **Repo protocols:** Neo4j repos via `repositories/` layer; `ApiResponse[T]` envelope via `envelope()`; BFF proxy via `proxyToApi`; MUI imported per-component-path; `npm install --legacy-peer-deps` (never remove); package managers `uv` (Python) + `npm` (frontend2, never `npm ci`).
- **Commit discipline:** never commit without explicit user confirmation. End commit messages with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Worktrees:** when creating a worktree, branch from the **current branch/HEAD**, never `main`/`origin/main`.
- **docker-compose.yml sync:** any change to root `docker-compose.yml` must be mirrored to `.docker/staging/docker-compose.yml` in the same commit (not needed for this remodel — no compose changes expected).

---

## File Structure

**Ingestion (`services/ingestion/src/`)**
- `vehicles.py` — rename of `machine_units.py`: `VehicleObservation` dataclass, `VehicleSourceKind`, normalizers `normalize_lta_tag`/`normalize_serial_number`/`normalize_vehicle_product`, `valid_vehicle_observation`. Pure helpers, no I/O.
- `vehicle_extraction.py` — rename of `machine_unit_extraction.py`: `observations_from_sales_lines`, `observations_from_chat_inquiries`, plus per-source vehicle-category classification (`is_vehicle_line`).
- `vehicle_categories.py` — **new**: per-source vehicle-category allowlist config (by name) + resolver `category_is_vehicle(source_system_key, category_name)`.
- `graph/queries/vehicle.py` — rename of `graph/queries/machine_units.py`: `UPSERT_VEHICLE` (cross-source LTA + per-source serial + promotion + conflict), `RESOLVE_EXISTING_VEHICLE_FOR_CHAT`, `LINK_ORDER_INVOLVES_VEHICLE`, `LINK_PERSON_BOUGHT_VEHICLE`, `LINK_PERSON_OWNS_VEHICLE`, `LINK_SOURCE_RECORD_MENTIONS_VEHICLE`, `LINK_CHAT_SOURCE_RECORD_MENTIONS_VEHICLE`, `FLAG_VEHICLE_OWNER_CONFLICTS`.
- `graph/queries/sales.py` — modify: `MERGE_ORDER` += `non_vehicle_lines`; `CLEAR_SUPERSEDED_SALES_LINKS` → Vehicle rels; `FIND_VEHICLE_CANDIDATES_FOR_SALES` (rename + add mobile/email + NRIC fields in return); delete `FIND_MACHINE_UNIT_CANDIDATES_FOR_SALES`.
- `graph/queries/__init__.py` — re-export Vehicle constants; drop MachineUnit names from `__all__`.
- `pipeline_sales.py` — `_write_vehicle_observations` (rename), `_link_sale_vehicle_to_person`, `_propose_one_pending_sale` (vehicle+mobile/email+NRIC), `propose_vehicle_matches_for_pending_sales` (rename), `drain_pending_customer_sales`.
- `pipeline.py` — `_write_chat_vehicle_observations` (rename).
- `main.py` — call `propose_vehicle_matches_for_pending_sales`.
- `matching/vehicle_heuristic.py` — rename of `machine_unit_heuristic.py`: `VehicleCandidate`, `VEHICLE_MATCH_AUTO=0.90`, review band constants, `select_best_vehicle_candidate`, `build_vehicle_match_result`, `build_vehicle_no_match_result` (NRIC block).
- `exclusions.py` / `exclusion_config.py` / `ingestion_config.py` — `VehicleIdentifierKey` / `vehicle_identifiers` (rename).
- `llm_prompts.py` — inquiry field renames (`machine_*` → `vehicle_*`).
- `connectors/dumps/connectors.py` — `PHPPOS_SALES_TABLES` += `phppos_categories`, `phppos_customers`.
- `connectors/fundbox/schema.py` — `products` model += `has_serial_number`, `has_lta_tag`; `merchants` already has `name`/`official_name`.
- `connectors/fundbox/sales.py` — emit customer bike plate / merchant name where needed (fundbox already emits lta/serial; add merchant name on line).
- `connectors/speedzone/sales.py` (via `phppos_sales_common.py`) — emit customer `custom_field_8_value`/`custom_field_10_value` as the line LTA; emit customer `custom_field_1_value` (NRIC) for anti-match.
- `connectors/onediver/connector.py` — `ONEDIVER_SALES_TABLES` += `sales_order_items`, `products`; `_build_sales_envelope` reads them for `non_vehicle_lines`.
- `connectors/bitrix/connector.py:352` — rename `machine_unit_identifiers` pass-through → `vehicle_identifiers`.

**Graph schema (`infra/neo4j/init.cypher`)** — remove `machine_unit_*` constraint/indexes; add `vehicle_id_unique`, `vehicle_lta_unique`, `idx_vehicle_serial`.

**API (`services/api/src/`)**
- `types.py` — `MachineUnitSummary`→`VehicleSummary`, `Person.machine_units`→`Person.vehicles`, `SalesUnitSummary`→`SalesVehicleSummary`, new `NonVehicleLine` model, `SalesOrderSummary.non_vehicle_lines`.
- `graph/queries/persons.py` — `GET_PERSON_BY_ID` Vehicle projection.
- `graph/queries/review.py` — `GET_REVIEW_CASE` Vehicle + `non_vehicle_lines`; `LINK_REVIEW_SALES_BOUGHT_UNIT`→`LINK_REVIEW_SALES_BOUGHT_VEHICLE`; `MARK_REVIEW_SALES_RECORD_*` unchanged.
- `graph/queries/__init__.py` — exports.
- `graph/mappers.py` — `_map_vehicles` (rename), `_map_sales_summary` (Vehicle + non_vehicle_lines).
- `routes/persons.py` — unchanged shape (via types).
- `routes/public_pages.py` — strip `vehicles` + `non_vehicle_lines`.
- `repositories/neo4j/review.py` — imports + `_sales_link_merge_tx` Vehicle link queries.

**Frontend2 (`services/frontend2/src/`)**
- `lib/api-types.ts` — `MachineUnitSummary`→`VehicleSummary`, `Person.machine_units`→`Person.vehicles`, `NonVehicleLine`, `SalesOrderSummary.non_vehicle_lines`.
- `app/persons/[personId]/page.tsx` — `MachineUnitsSidebarCard`→`VehiclesSidebarCard`.

**Tests** — rename/update: `test_machine_units.py`→`test_vehicles.py`, `test_machine_unit_extraction.py`→`test_vehicle_extraction.py`, `test_machine_unit_queries.py`→`test_vehicle_queries.py`, `test_machine_unit_heuristic.py`→`test_vehicle_heuristic.py`, `test_sales_machine_unit_matching.py`→`test_sales_vehicle_matching.py`; update `test_exclusions.py`, `test_exclusion_config.py`, `test_ingestion_config.py`; API `test_person_loyalty_machine_units.py`→`test_person_loyalty_vehicles.py`, `test_review_mappers.py`, `test_review_repository_merge.py`, `test_public_person_excludes_loyalty.py`.

**Local-only (untracked, gitignored):** `.dumps/limited-100/generate_limited_dumps.py` (generator edits) + regenerated `*_100.sql`.

---

## Task 1: Graph schema — Vehicle constraints/indexes, remove MachineUnit

**Files:**
- Modify: `infra/neo4j/init.cypher:32-33, 72-77`

**Interfaces:**
- Produces: `:Vehicle` constraint `vehicle_id_unique`, uniqueness `vehicle_lta_unique`, index `idx_vehicle_serial`; removes `machine_unit_id_unique`, `idx_machine_unit_lta_tag`, `idx_machine_unit_serial_number`. Later tasks' Cypher depends on these existing.

- [ ] **Step 1: Edit `infra/neo4j/init.cypher`** — replace the MachineUnit block (lines 32-33) and the indexes (lines 72-77).

Replace:
```cypher
CREATE CONSTRAINT machine_unit_id_unique IF NOT EXISTS
  FOR (mu:MachineUnit) REQUIRE mu.machine_unit_id IS UNIQUE;
```
with:
```cypher
CREATE CONSTRAINT vehicle_id_unique IF NOT EXISTS
  FOR (v:Vehicle) REQUIRE v.vehicle_id IS UNIQUE;

CREATE CONSTRAINT vehicle_lta_unique IF NOT EXISTS
  FOR (v:Vehicle) REQUIRE v.normalized_lta_tag IS UNIQUE;
```

Replace:
```cypher
// Machine unit lookups
CREATE INDEX idx_machine_unit_lta_tag IF NOT EXISTS
  FOR (mu:MachineUnit) ON (mu.normalized_lta_tag);

CREATE INDEX idx_machine_unit_serial_number IF NOT EXISTS
  FOR (mu:MachineUnit) ON (mu.normalized_serial_number);
```
with:
```cypher
// Vehicle lookups
CREATE INDEX idx_vehicle_serial IF NOT EXISTS
  FOR (v:Vehicle) ON (v.normalized_serial_number);
```
(LTA lookups use the unique constraint index; no separate LTA index needed. The serial index supports the per-source `(source_system_key, product_sku, normalized_serial_number)` lookup, filtered in the query.)

- [ ] **Step 2: Commit**

```bash
git add infra/neo4j/init.cypher
git commit -m "feat(graph): add Vehicle constraints/indexes, remove MachineUnit

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Vehicle graph queries (`graph/queries/vehicle.py` + `sales.py` + `__init__.py`)

**Files:**
- Create: `services/ingestion/src/graph/queries/vehicle.py` (rename of `machine_units.py`)
- Delete: `services/ingestion/src/graph/queries/machine_units.py`
- Modify: `services/ingestion/src/graph/queries/sales.py:54-81` (`MERGE_ORDER`), `151-164` (`CLEAR_SUPERSEDED_SALES_LINKS`), `200-210` (`FIND_MACHINE_UNIT_CANDIDATES_FOR_SALES` → `FIND_VEHICLE_CANDIDATES_FOR_SALES`)
- Modify: `services/ingestion/src/graph/queries/__init__.py:27-36` + `__all__`
- Test: `services/ingestion/tests/test_vehicle_queries.py` (rename of `test_machine_unit_queries.py`)

**Interfaces:**
- Consumes: `VehicleObservation` from `vehicles.py` (Task 3) — for property names only; this task just defines the Cypher strings.
- Produces: `UPSERT_VEHICLE` (params: `source_system_key, product_sku, product, manufacturer, model, lta_tag, normalized_lta_tag, serial_number, normalized_serial_number, observed_at`; returns `vehicle_id, conflict`), `RESOLVE_EXISTING_VEHICLE_FOR_CHAT`, `LINK_ORDER_INVOLVES_VEHICLE`, `LINK_PERSON_BOUGHT_VEHICLE`, `LINK_PERSON_OWNS_VEHICLE`, `LINK_SOURCE_RECORD_MENTIONS_VEHICLE`, `LINK_CHAT_SOURCE_RECORD_MENTIONS_VEHICLE`, `FLAG_VEHICLE_OWNER_CONFLICTS`, `FIND_VEHICLE_CANDIDATES_FOR_SALES`, `MERGE_ORDER` (with `non_vehicle_lines` param), `CLEAR_SUPERSEDED_SALES_LINKS`.

- [ ] **Step 1: Write `test_vehicle_queries.py`** — rename from `test_machine_unit_queries.py`; update assertions to expect `:Vehicle`, `INVOLVES_VEHICLE`/`BOUGHT_VEHICLE`/`OWNS_VEHICLE`/`MENTIONS_VEHICLE`, and the cross-source LTA-first upsert. Add: (a) LTA present → single global Vehicle across two source_system_key calls; (b) serial-only → per-source Vehicle (two sources with same serial+sku create two Vehicles); (c) serial-only then LTA → reconciles onto the LTA Vehicle; (d) LTA matches one + serial matches a different Vehicle → `conflict=true` no merge; (e) `FIND_VEHICLE_CANDIDATES_FOR_SALES` returns persons sharing a Vehicle identity with the sale's vehicle lines (join via `INVOLVES_VEHICLE`), with `mobile`/`email`/`nric` columns returned for the heuristic; (f) `MERGE_ORDER` writes `non_vehicle_lines`; (g) `CLEAR_SUPERSEDED_SALES_LINKS` deletes `INVOLVES_VEHICLE` + `BOUGHT_VEHICLE`.

- [ ] **Step 2: Implement `vehicle.py`**

```python
"""Vehicle graph queries. Cross-source identity via LTA tag; per-source via serial+sku."""
from __future__ import annotations

UPSERT_VEHICLE = """
// 1. Try global match by normalized LTA tag (when present).
OPTIONAL MATCH (lta_match:Vehicle {normalized_lta_tag: $normalized_lta_tag})
WHERE $normalized_lta_tag IS NOT NULL
// 2. Try per-source match by (source_system_key, product_sku, normalized_serial_number).
OPTIONAL MATCH (ser_match:Vehicle)
WHERE $normalized_serial_number IS NOT NULL
  AND $source_system_key IN ser_match.source_systems
  AND $product_sku IN ser_match.observed_product_skus_s
  AND ser_match.normalized_serial_number = $normalized_serial_number
WITH lta_match, ser_match
// 3. Conflict: LTA matches one Vehicle and serial matches a different one.
WITH lta_match, ser_match,
     CASE WHEN lta_match IS NOT NULL AND ser_match IS NOT NULL
          AND lta_match <> ser_match THEN true ELSE false END AS id_conflict
FOREACH (_ IN CASE WHEN id_conflict THEN [1] ELSE [] END |
  MERGE (v:Vehicle {vehicle_id: randomUUID()})
    SET v.conflict_flag = true,
        v.conflict_reason = 'identifier_conflict'
)
WITH lta_match, ser_match, id_conflict
// 4. Choose target: prefer LTA match (cross-source), else serial match, else create.
WITH coalesce(lta_match, ser_match) AS target, id_conflict
FOREACH (v IN CASE WHEN target IS NULL THEN [{id: randomUUID()}] ELSE [] END |
  MERGE (node:Vehicle {vehicle_id: v.id})
    SET node.created_at = $observed_at
)
WITH target, coalesce(lta_match, ser_match) AS existing, id_conflict
OPTIONAL MATCH (created:Vehicle) WHERE NOT existing IS NULL AND created = coalesce(lta_match, ser_match)
WITH coalesce(existing, created) AS v, id_conflict
SET v.updated_at = $observed_at,
    v.normalized_lta_tag = coalesce(v.normalized_lta_tag, $normalized_lta_tag),
    v.normalized_serial_number = coalesce(v.normalized_serial_number, $normalized_serial_number),
    v.lta_tag = coalesce(v.lta_tag, $lta_tag),
    v.serial_number = coalesce(v.serial_number, $serial_number),
    v.product_sku = coalesce(v.product_sku, $product_sku),
    v.product = coalesce(v.product, $product),
    v.manufacturer = coalesce(v.manufacturer, $manufacturer),
    v.model = coalesce(v.model, $model),
    v.source_systems = (CASE WHEN $source_system_key IN v.source_systems
                             THEN v.source_systems
                             ELSE v.source_systems + [$source_system_key] END),
    v.conflict_flag = coalesce(v.conflict_flag, id_conflict)
RETURN v.vehicle_id AS vehicle_id, coalesce(v.conflict_flag, false) AS conflict
"""
```

(Note: `observed_product_skus_s` is a list property stored for membership checks; see Task 3 for how it's populated. If the per-source serial lookup proves unwieldy with a list, fall back to filtering by `source_system_key IN v.source_systems` and `v.product_sku = $product_sku` accepting that a cross-source LTA Vehicle's `product_sku` is the first-observed — the LTA path already handles cross-source, so serial-only per-source matching by `source_systems` + first `product_sku` is acceptable. Prefer the `observed_product_skus_s` list for correctness.)

`LINK_ORDER_INVOLVES_VEHICLE`:
```python
LINK_ORDER_INVOLVES_VEHICLE = """
MATCH (o:Order {source_system_key: $source_system_key, source_order_id: $source_order_id})
MATCH (v:Vehicle {vehicle_id: $vehicle_id})
MERGE (o)-[rel:INVOLVES_VEHICLE {
    source_system_key: $source_system_key, source_record_pk: $source_record_pk
}]->(v)
SET rel.observed_at = $observed_at,
    rel.confidence = $confidence,
    rel.quality_flag = $quality_flag,
    rel.created_at = coalesce(rel.created_at, $observed_at)
"""
```

`LINK_PERSON_BOUGHT_VEHICLE` (params `source_system_key, source_order_id, source_record_pk, is_active, confidence, quality_flag, observed_at`):
```python
LINK_PERSON_BOUGHT_VEHICLE = """
MATCH (p:Person {person_id: $person_id})
MATCH (v:Vehicle {vehicle_id: $vehicle_id})
MERGE (p)-[rel:BOUGHT_VEHICLE {
    source_system_key: $source_system_key, source_order_id: $source_order_id
}]->(v)
SET rel.source_record_pk = $source_record_pk,
    rel.is_active = $is_active,
    rel.confidence = $confidence,
    rel.quality_flag = $quality_flag,
    rel.observed_at = $observed_at,
    rel.last_confirmed_at = coalesce(rel.last_confirmed_at, $observed_at),
    rel.created_at = coalesce(rel.created_at, $observed_at)
"""
```

`LINK_PERSON_OWNS_VEHICLE` (explicit ownership; params incl. `last_confirmed_at, is_active, confidence, quality_flag`):
```python
LINK_PERSON_OWNS_VEHICLE = """
MATCH (p:Person {person_id: $person_id})
MATCH (v:Vehicle {vehicle_id: $vehicle_id})
MERGE (p)-[rel:OWNS_VEHICLE {
    source_system_key: $source_system_key, source_record_pk: $source_record_pk
}]->(v)
SET rel.is_active = $is_active,
    rel.last_confirmed_at = $last_confirmed_at,
    rel.first_seen_at = coalesce(rel.first_seen_at, $observed_at),
    rel.confidence = $confidence,
    rel.quality_flag = $quality_flag,
    rel.observed_at = $observed_at,
    rel.created_at = coalesce(rel.created_at, $observed_at)
"""
```

`LINK_SOURCE_RECORD_MENTIONS_VEHICLE` and `LINK_CHAT_SOURCE_RECORD_MENTIONS_VEHICLE` and `RESOLVE_EXISTING_VEHICLE_FOR_CHAT` and `FLAG_VEHICLE_OWNER_CONFLICTS`: rename of the corresponding `*_MACHINE_UNIT*` / `*_UNIT*` constants, swapping `:MachineUnit`→`:Vehicle`, `MENTIONS_UNIT`→`MENTIONS_VEHICLE`, `OWNS_UNIT`→`OWNS_VEHICLE`, `machine_unit_id`→`vehicle_id`, `normalized_machine_product` removed. `RESOLVE_EXISTING_VEHICLE_FOR_CHAT` matches by `normalized_lta_tag` (global, when present) OR `(source_system_key IN v.source_systems AND v.product_sku = $product_sku AND v.normalized_serial_number = $normalized_serial_number)`, returning `collect(v.vehicle_id)`.

- [ ] **Step 3: Modify `sales.py`**

`MERGE_ORDER` — add `non_vehicle_lines` SET (append after the existing property SETs around line 78):
```python
    o.non_vehicle_lines = $non_vehicle_lines,
```
And keep the param `non_vehicle_lines` (JSON-encoded list; pass `[]` when no non-vehicle lines).

`CLEAR_SUPERSEDED_SALES_LINKS` — replace `INVOLVES_UNIT` (line 157) and `BOUGHT_UNIT` (line 161) with:
```cypher
OPTIONAL MATCH (o)-[unit_rel:INVOLVES_VEHICLE]->(:Vehicle)
WHERE unit_rel.source_record_pk = $old_source_record_pk
DELETE unit_rel
...
OPTIONAL MATCH (:Person)-[bought:BOUGHT_VEHICLE {source_system_key: $source_system_key, source_order_id: $source_order_id}]->(:Vehicle)
WHERE bought.source_record_pk = $old_source_record_pk
DELETE bought
```

`FIND_VEHICLE_CANDIDATES_FOR_SALES` (rename + expand). Replace `FIND_MACHINE_UNIT_CANDIDATES_FOR_SALES` (lines 200-210):
```python
FIND_VEHICLE_CANDIDATES_FOR_SALES = """
// Active Persons sharing a Vehicle identity with a pending-customer sale's vehicle lines,
// with a mobile OR email match on the sale's customer.
MATCH (sr:SourceRecord {source_record_pk: $sales_source_record_pk, link_status: 'pending_customer'})
MATCH (o:Order)-[inv:INVOLVES_VEHICLE {source_record_pk: $sales_source_record_pk}]->(v:Vehicle)
MATCH (v)<-[rel:BOUGHT_VEHICLE|OWNS_VEHICLE]-(p:Person {status: 'active'})
// Contact channel match: sale customer' mobile/email hits one of the candidate Person's identifiers.
MATCH (pi:Identifier)
WHERE (pi.value IN $customer_emails AND pi.kind IN ['email'])
   OR (pi.value IN $customer_phones AND pi.kind IN ['mobile','phone'])
MATCH (p)-[:IDENTIFIED_BY]->(pi)
WITH sr, v, p, rel,
     collect(DISTINCT pi.kind) AS contact_channels,
     $customer_nric AS customer_nric
OPTIONAL MATCH (p)-[:IDENTIFIED_BY]->(ni:Identifier)
WHERE ni.kind IN ['nric','nric_hash'] AND customer_nric IS NOT NULL AND customer_nric <> '' AND ni.value <> customer_nric
WITH sr, v, p, rel, contact_channels,
     collect(DISTINCT ni.value) AS mismatched_nrics
RETURN p.person_id AS person_id,
       v.vehicle_id AS vehicle_id,
       type(rel) AS rel_type,
       rel.is_active AS is_active,
       v.conflict_flag AS conflict_flag,
       rel.last_confirmed_at AS last_confirmed_at,
       contact_channels,
       size(mismatched_nrics) > 0 AS nric_blocked
ORDER BY rel_type, rel.is_active DESC, rel.last_confirmed_at DESC
"""
```
(Params: `sales_source_record_pk, customer_emails: list[str], customer_phones: list[str], customer_nric: str | None`. The heuristic consumes `contact_channels` + `nric_blocked`.) Delete `FIND_MACHINE_UNIT_CANDIDATES_FOR_SALES`.

- [ ] **Step 4: Update `__init__.py`** — replace the `machine_units` import block (lines 27-36) with the `vehicle` block; rename `__all__` entries; re-export `FIND_VEHICLE_CANDIDATES_FOR_SALES` (drop `FIND_MACHINE_UNIT_CANDIDATES_FOR_SALES`).

- [ ] **Step 5: Delete `machine_units.py`** and its old test; confirm no remaining `from src.graph.queries.machine_units` / `import machine_units` references (`Grep` for `machine_units` → should be zero in `src/`).

- [ ] **Step 6: Commit + push; verify via `wpci home pipeline show sparkfn/hyperP <n>`** — PR pipeline ruff + mypy --strict + pytest green for the ingestion package. (Tests in Task 3+ may still reference `vehicles.py` not yet created — if collection fails on missing import, that's expected until Task 3; either keep `test_vehicle_queries.py` importing only from `src.graph.queries.vehicle` for the query-string shape tests, or stage Tasks 2+3 together. Recommended: implement Task 3 before pushing 2 alone.)

```bash
git add services/ingestion/src/graph/queries/vehicle.py services/ingestion/src/graph/queries/sales.py services/ingestion/src/graph/queries/__init__.py services/ingestion/tests/test_vehicle_queries.py
git rm services/ingestion/src/graph/queries/machine_units.py services/ingestion/tests/test_machine_unit_queries.py
git commit -m "feat(ingestion): Vehicle graph queries (cross-source LTA + per-source serial)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Vehicle normalization + extraction + per-source classification

**Files:**
- Create: `services/ingestion/src/vehicles.py` (rename of `machine_units.py`)
- Create: `services/ingestion/src/vehicle_extraction.py` (rename of `machine_unit_extraction.py`)
- Create: `services/ingestion/src/vehicle_categories.py`
- Delete: `services/ingestion/src/machine_units.py`, `services/ingestion/src/machine_unit_extraction.py`
- Test: `services/ingestion/tests/test_vehicles.py`, `test_vehicle_extraction.py`

**Interfaces:**
- Produces: `VehicleObservation` (frozen dataclass: `source_kind: VehicleSourceKind, source_system_key: str, source_record_id: str, lta_tag: str | None, serial_number: str | None, product_sku: str | None, product: str | None, manufacturer: str | None, model: str | None, unit_label: str | None, observed_at: str | None, confidence: float, quality_flag: QualityFlag, raw_context: str | None`), normalizers `normalize_lta_tag`, `normalize_serial_number`, `normalize_vehicle_product`, `valid_vehicle_observation`, and `observations_from_sales_lines`/`observations_from_chat_inquiries` returning `list[VehicleObservation]`.

- [ ] **Step 1: Write `test_vehicles.py`** (rename of `test_machine_units.py`) — assert: `normalize_lta_tag` uppercases + strips separators; `normalize_serial_number` preserves meaningful punctuation, returns None for placeholders; `normalize_vehicle_product` collapses whitespace, rejects placeholders; `valid_vehicle_observation` requires `product_sku` (non-None) AND at least one of `lta_tag`/`serial_number` normalized. (Compared to the old `machine_product`, the required field is now `product_sku`.)

- [ ] **Step 2: Implement `vehicles.py`** — copy `machine_units.py`, rename `MachineUnitSourceKind`→`VehicleSourceKind = Literal["sales","chat_inquiry","explicit_ownership_claim"]`, `MachineUnitObservation`→`VehicleObservation`, add `product_sku/manufacturer/model` fields, replace `machine_product` with `product` (display) + `product_sku` (key). Keep `normalize_lta_tag`/`normalize_serial_number`/`normalize_machine_product`→`normalize_vehicle_product` identical logic. `valid_vehicle_observation` requires `product_sku` non-None AND one identifier.

- [ ] **Step 3: Write `vehicle_categories.py`**

```python
"""Per-source vehicle-category allowlists (by category name)."""
from __future__ import annotations

EKO_VEHICLE_CATEGORIES: frozenset[str] = frozenset({
    "Bicycles", "Foldable Bicycles", "Bi-fold Bicycles", "Tri-fold Bicycles",
    "Brompton Alternatives", "Hybrid Bicycles", "Mountain Bikes", "City Bicycles",
    "Road Bikes", "Electric Bicycles", "Electric Scooters", "Personal Mobility Aids",
    "Electric Wheelchairs", "Rollators", "Wheelchairs", "Wheelchairs Cerebral Palsy",
    "Seated Electric Scooters", "Standing Electric Scooters", "Foldable Electric Bicycles",
    "Used Electric Bicycles", "Used Electric Scooters", "Ji-Move", "Mobot",
    "YY Scooter", "Soomax",
})

SPEEDZONE_VEHICLE_CATEGORIES: frozenset[str] = frozenset({
    "New Motorbike", "Used Motorbike", "Road Bike", "Scooter", "Sport Bike",
    "Scrambler", "Cafe Racer", "Used Road Bike", "Used Scooter", "Used Scrambler",
    "Used Cafe Racer", "Tourer", "Used Tourer", "Used Sport Bike", "Cruiser",
    "Used Cruiser", "Motorbike",
})

# Fundbox vehicle categories (product.category string).
FUNDBOX_VEHICLE_CATEGORIES: frozenset[str] = frozenset({
    "Electric Scooters", "Personal Mobility Aids", "Electric Wheelchairs",
    "Motorised Wheelchairs", "Mobility Scooters", "Power Assisted Bicycles",
    "Electric Bicycles", "Bicycles",
})

_VEHICLE_CATEGORIES: dict[str, frozenset[str]] = {
    "eko_phppos": EKO_VEHICLE_CATEGORIES,
    "speedzone_phppos": SPEEDZONE_VEHICLE_CATEGORIES,
    "fundbox": FUNDBOX_VEHICLE_CATEGORIES,
}


def category_is_vehicle(source_system_key: str, category_name: str | None) -> bool:
    """True if the product category is a vehicle category for the source."""
    if not category_name:
        return False
    allow = _VEHICLE_CATEGORIES.get(source_system_key)
    return bool(allow and category_name in allow)
```
(OneDiver has no entry — `category_is_vehicle("onediver", ...)` returns False, so no Vehicle is ever created from onediver, as intended. Brand categories (`Honda`, `Yamaha`) are intentionally NOT in the speedzone allowlist; a speedzone brand-category line still classifies as a vehicle only if the brand category name is in the allowlist — since brand cats aren't listed, brand-cat lines rely on the bike-type categorization. If a speedzone motorcycle is filed under `Honda` only (not a bike-type cat), it won't classify — to cover that, add a speedzone fallback: when `is_serialized=1` AND the line has a serial AND the item name/brand matches a known motorcycle brand set, classify as vehicle. Add that fallback here if dump inspection shows real motorcycles under brand cats without bike-type. Re-examine during Task 4 connector work; for now the bike-type allowlist covers the common case.)

- [ ] **Step 4: Write `test_vehicle_extraction.py`** — assert: (a) eko sales line with `is_serialized=1` in `Electric Bicycles` + serial → one VehicleObservation with `product_sku` from `item_number`, `serial_number` set; (b) eko line in `Bicycle Locks` (non-vehicle) + serial → **no** observation (non-vehicle, stays on Order); (c) fundbox line with `has_serial_number=1` + `serial_no` + vehicle category → observation with `product_sku` from `product_variants.sku`, `lta_tag` from `order_items.lta_tag`, `manufacturer`/`model`/`merchant` carried; (d) speedzone line with serial + customer bike plate → observation with `serial_number`=chassis, `lta_tag`=customer plate; (e) chat inquiry → observation with `source_kind="chat_inquiry"`, confidence 0.6; (f) `observations_from_chat_inquiries` does not create observations for non-vehicle product names.

- [ ] **Step 5: Implement `vehicle_extraction.py`** — rename `machine_unit_extraction.py`. `observations_from_sales_lines(*, source_system_key, source_record_id, observed_at, lines)`:
  - For each line: read `category = line["product"].get("category")` (resolved name), `is_vehicle = category_is_vehicle(source_system_key, category)`. If not `is_vehicle` → skip (non-vehicle → handled by Order enrichment in Task 5). If `is_vehicle` → read `serial_number = line["metadata"].get("serial_number") or line["metadata"].get("serial_no")`, `lta_tag = line["metadata"].get("lta_tag") or line["metadata"].get("lta_tag")` (and for speedzone, the connector emits the customer plate into `line["metadata"]["lta_tag"]` per Task 4). `product_sku = line["product"].get("sku") or line["product"].get("item_number")`. Build `VehicleObservation(source_kind="sales", confidence=1.0, quality_flag=QualityFlag.VALID, product_sku=product_sku, product=_product_name(line), manufacturer=line["product"].get("manufacturer"), model=line["product"].get("model"), ...)`; keep only if `valid_vehicle_observation(...)`.
  - `observations_from_chat_inquiries`: same shape as before, gated on `category_is_vehicle` where the product category can be inferred (chat inquiries carry a free-text product; classify heuristically — if the inquiry's product matches a vehicle-category keyword, treat as vehicle; otherwise skip). Keep confidence 0.6, `QualityFlag.PARTIAL_PARSE`.

- [ ] **Step 6: Delete old files**, commit + push, verify via `wpci home`.

```bash
git add services/ingestion/src/vehicles.py services/ingestion/src/vehicle_extraction.py services/ingestion/src/vehicle_categories.py services/ingestion/tests/test_vehicles.py services/ingestion/tests/test_vehicle_extraction.py
git rm services/ingestion/src/machine_units.py services/ingestion/src/machine_unit_extraction.py services/ingestion/tests/test_machine_units.py services/ingestion/tests/test_machine_unit_extraction.py
git commit -m "feat(ingestion): Vehicle normalization, extraction, per-source category allowlist

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Connector gaps (fundbox flags, speedzone plate, onediver line items, table-specs)

**Files:**
- Modify: `services/ingestion/src/connectors/dumps/connectors.py:119` (`PHPPOS_SALES_TABLES`)
- Modify: `services/ingestion/src/connectors/fundbox/schema.py` (`products` model)
- Modify: `services/ingestion/src/connectors/fundbox/sales.py` (emit merchant name)
- Modify: `services/ingestion/src/connectors/phppos_sales_common.py` (speedzone bike plate + NRIC)
- Modify: `services/ingestion/src/connectors/onediver/connector.py` (`ONEDIVER_SALES_TABLES` + `_build_sales_envelope`)
- Test: existing connector tests + new ones for the gaps

**Interfaces:**
- Produces: sales line envelopes carry `product.category` (name), `product.sku`, `product.manufacturer`, `product.model`, `metadata.serial_number`/`metadata.lta_tag` (speedzone lta_tag = customer plate), `metadata.nric` (customer NRIC for anti-match), `metadata.merchant` (fundbox merchant name); `customer_link` already carries the customer identity. Onediver sales envelopes carry `non_vehicle_lines` (assembled from `sales_order_items` + `products`).

- [ ] **Step 1: `PHPPOS_SALES_TABLES` += categories + customers** (`connectors/dumps/connectors.py:119`):
```python
PHPPOS_SALES_TABLES: TableSpec = {
    "phppos_sales": None,
    "phppos_sales_items": None,
    "phppos_items": None,
    "phppos_categories": None,   # new — category_id -> name for vehicle classification
    "phppos_customers": None,    # new — customer bike plate (custom_field_8/10) + NRIC (custom_field_1)
}
```

- [ ] **Step 2: fundbox `products` model += flags** (`connectors/fundbox/schema.py` products class): add `has_serial_number: Mapped[int]`, `has_lta_tag: Mapped[int]` columns (the dump reader already auto-reflects them, but the live SQLAlchemy model needs them for live ingestion). Verify against the dump CREATE TABLE column types (`int(1)`). Also confirm `merchants` model exposes `name`/`official_name` (already present).

- [ ] **Step 3: fundbox sales connector emits merchant name** (`connectors/fundbox/sales.py`) — in the line metadata, add `merchant` resolved from `merchants.name` for the order's `merchant_id`. The connector already emits `lta_tag`/`serial_no`/`merchant_product_id`; add `merchant` and the resolved product `category`/`manufacturer`/`model` from the `products` row (joined via `merchant_products → product_variants → products`). Add `has_serial_number`/`has_lta_tag` to the emitted product dict so `vehicle_extraction` could use them as a secondary gate (the primary gate is category).

- [ ] **Step 4: phppos speedzone bike plate + NRIC** (`connectors/phppos_sales_common.py`) — when building a sales line envelope, for speedzone (detect via `source_system_key == "speedzone_phppos"`), set `line["metadata"]["lta_tag"]` from the joined `phppos_customers.custom_field_8_value` (fall back to `custom_field_10_value`) of the sale's `customer_id`. Also emit `line["metadata"]["nric"]` = `phppos_customers.custom_field_1_value` for all phppos sources (eko + speedzone) for the anti-match. The customers table is now loaded (Step 1) so the join is available in dump mode; for live mode, the SQLAlchemy customer model already has `custom_field_*` columns. Resolve `category` name from `phppos_categories` (now loaded) by `category_id`.

- [ ] **Step 5: onediver sales connector reads line items + products** (`connectors/onediver/connector.py`):
  - `ONEDIVER_SALES_TABLES` += `"sales_order_items": None, "products": None`.
  - In `_build_sales_envelope`, after loading `sales_orders`, load `sales_order_items` (filtered by the sampled `sales_orders.id`) and `products` (filtered by `sales_order_items.product_id`). Build `non_vehicle_lines` per order: `[{source_line_item_id, sku, product_name, category (from products.product_type or sales_order_items fields), quantity, unit_price, line_total}]` (no serial/lta — onediver has none; no merchant). Put `non_vehicle_lines` on the order's `raw_payload` (or a top-level envelope field consumed by the pipeline in Task 5). Emit `customer_link` by `billing_contact_email` as today; also emit `customer_nric` = `profiles.ic_number` for the anti-match (resolved from the profile).

- [ ] **Step 6: Tests** — update `test_dump_connectors.py`: fundbox line carries `merchant` + `has_serial_number`; speedzone line carries `lta_tag` (customer plate) + `nric`; onediver envelope carries `non_vehicle_lines` from `sales_order_items`+`products`. Add `ONEDIVER_SALES_TABLES` includes the two new tables.

- [ ] **Step 7: Commit + push; verify via `wpci home`.**

```bash
git add services/ingestion/src/connectors/ services/ingestion/tests/test_dump_connectors.py
git commit -m "feat(ingestion): connector gaps — fundbox flags/merchant, speedzone plate/NRIC, onediver line items

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Order enrichment (`non_vehicle_lines`) + pipeline vehicle writes

**Files:**
- Modify: `services/ingestion/src/pipeline_sales.py:312-368, 527-538` (`_write_vehicle_observations` rename + call site; `MERGE_ORDER` `non_vehicle_lines` assembly)
- Modify: `services/ingestion/src/graph/queries/sales.py` — `MERGE_ORDER` already edited in Task 2 to SET `non_vehicle_lines`
- Test: `services/ingestion/tests/test_sales_vehicle_matching.py` (partial — the Order-enrichment part)

**Interfaces:**
- Consumes: `observations_from_sales_lines` (Task 3), `UPSERT_VEHICLE`/`LINK_ORDER_INVOLVES_VEHICLE`/`LINK_PERSON_BOUGHT_VEHICLE` (Task 2), the sales line envelopes with `metadata.merchant`/`nric` (Task 4).
- Produces: per sales record, `Order.non_vehicle_lines` populated from non-vehicle lines; Vehicle nodes + `INVOLVES_VEHICLE` + `BOUGHT_VEHICLE` for vehicle lines.

- [ ] **Step 1: Assemble `non_vehicle_lines` in the sales write flow** (`pipeline_sales.py`) — where the order is built (before `MERGE_ORDER`), partition the order's lines into vehicle vs non-vehicle using `category_is_vehicle(source_system_key, line["product"]["category"])` AND line has serial/lta. For non-vehicle lines, build the `non_vehicle_lines` list (dict per §Order enrichment shape in the spec: `source_line_item_id, sku, product_name, category, manufacturer, model, serial_number, lta_tag, quantity, unit_price, line_total, merchant`). Pass `non_vehicle_lines` (JSON-encoded) to `MERGE_ORDER`. Vehicle lines are NOT in this array (their details live on the Vehicle).

- [ ] **Step 2: Rename + rework `_write_machine_unit_observations` → `_write_vehicle_observations`** (`pipeline_sales.py:312-368`) — use `observations_from_sales_lines` (which already filters to vehicle lines), `UPSERT_VEHICLE` (capture `vehicle_id, conflict`), `LINK_ORDER_INVOLVES_VEHICLE` (with `vehicle_id`), and if `person_id` resolved, `LINK_PERSON_BOUGHT_VEHICLE`. Keep the exclusion skip (`is_excluded_vehicle_observation`, Task 8). Drop `FLAG_MACHINE_UNIT_OWNER_CONFLICTS` calls for BOUGHT (ownership conflicts only apply to OWNS, Task 7 / explicit ownership — not in this task).

- [ ] **Step 3: Call site** (`pipeline_sales.py:527-538`) — rename the call to `_write_vehicle_observations(...)`.

- [ ] **Step 4: Test** — `test_sales_vehicle_matching.py`: an order with one vehicle line (eko e-bike + serial) + one non-vehicle line (helmet) → `Order.non_vehicle_lines` contains only the helmet; a `Vehicle` node exists for the e-bike; `INVOLVES_VEHICLE` links the Order to the Vehicle; if customer resolved, `BOUGHT_VEHICLE` links Person→Vehicle; the helmet is NOT a Vehicle.

- [ ] **Step 5: Commit + push; verify via `wpci home`.**

```bash
git add services/ingestion/src/pipeline_sales.py services/ingestion/tests/test_sales_vehicle_matching.py
git commit -m "feat(ingestion): Order non_vehicle_lines enrichment + Vehicle writes

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Matching heuristic + pending-customer auto-link + NRIC block

**Files:**
- Create: `services/ingestion/src/matching/vehicle_heuristic.py` (rename of `machine_unit_heuristic.py`)
- Delete: `services/ingestion/src/matching/machine_unit_heuristic.py`
- Modify: `services/ingestion/src/pipeline_sales.py:587-647` (`_propose_one_pending_sale`, `propose_machine_unit_matches_for_pending_sales`→vehicle)
- Modify: `services/ingestion/src/main.py:47-50, 334-342`
- Test: `services/ingestion/tests/test_vehicle_heuristic.py`, `test_sales_vehicle_matching.py` (matching part)

**Interfaces:**
- Consumes: `FIND_VEHICLE_CANDIDATES_FOR_SALES` rows (`person_id, vehicle_id, rel_type, is_active, conflict_flag, last_confirmed_at, contact_channels, nric_blocked`) from Task 2.
- Produces: `VehicleCandidate` dataclass, `select_best_vehicle_candidate(candidates) -> VehicleCandidate | None`, `build_vehicle_match_result(candidate) -> MatchResult` (decision=AUTO_MERGE when single + no nric_blocked; confidence `VEHICLE_MATCH_AUTO=0.90`; `signal_source='vehicle'`, `feature_snapshot` with `contact_channels`), `build_vehicle_no_match_result(candidate) -> MatchResult` (decision=NO_MATCH, `reason='nric_anti_match'`).

- [ ] **Step 1: Write `test_vehicle_heuristic.py`** — assert: (a) `select_best_vehicle_candidate([])` is None; (b) single candidate, `nric_blocked=False`, `contact_channels=['email']` → `build_vehicle_match_result` returns `decision=AUTO_MERGE, confidence=0.90`; (c) candidate with `nric_blocked=True` → `build_vehicle_no_match_result` returns `decision=NO_MATCH, reason='nric_anti_match'`; (d) multiple candidates → caller path creates a review case (review band); (e) `_is_best_tier` prefers `OWNS_VEHICLE` active no-conflict over `BOUGHT_VEHICLE`; tie-break by `last_confirmed_at` desc then `person_id` asc.

- [ ] **Step 2: Implement `vehicle_heuristic.py`** — rename `machine_unit_heuristic.py`. Constants: `VEHICLE_MATCH_AUTO: float = 0.90`, `VEHICLE_REVIEW_MIN: float = 0.60`. `VehicleCandidate` dataclass: `person_id, vehicle_id, rel_type, is_active, conflict_flag, last_confirmed_at, contact_channels: list[str], nric_blocked: bool`. `_is_best_tier(c)`: `rel_type == "OWNS_VEHICLE" and is_active and not conflict_flag`. `select_best_vehicle_candidate`: rank best-tier first, then most-recent `last_confirmed_at`, then smallest `person_id`; None sorts last; return None for empty. `build_vehicle_match_result(candidate)`: `MatchResult(decision=Decision.AUTO_MERGE, engine_type=EngineType.HEURISTIC, confidence=VEHICLE_MATCH_AUTO, ...)` with `feature_snapshot={candidate_person_id, vehicle_id, rel_type, conflict_flag, contact_channels, nric_blocked: False, signal_source: "vehicle"}`, reason `"vehicle_identity + {contact_channels} match"`. `build_vehicle_no_match_result(candidate)`: `MatchResult(decision=Decision.NO_MATCH, confidence=0.0, reason="nric_anti_match", feature_snapshot={..., nric_blocked: True, signal_source: "vehicle"})`.

- [ ] **Step 3: Rework `_propose_one_pending_sale`** (`pipeline_sales.py:587-617`) — gather the sale's customer `emails`/`phones`/`nric` (from the sales envelope `customer_link` + `metadata.nric`/customer record), run `FIND_VEHICLE_CANDIDATES_FOR_SALES` with `customer_emails/customer_phones/customer_nric`. Build `VehicleCandidate` list. If empty → return False. If any candidate has `nric_blocked=True` against the best candidate → `build_vehicle_no_match_result`, `persist_match_decision`, return True (no re-propose). If exactly one candidate (after `select_best_vehicle_candidate` resolves a clear winner with no other contenders) → `build_vehicle_match_result` (AUTO_MERGE), persist decision, **auto-link**: set `link_status='linked'`, `LINK_PERSON_PURCHASED_ORDER`, `LINK_PERSON_BOUGHT_VEHICLE`, return True. If multiple distinct persons remain → `build_vehicle_match_result` at review band (0.60–0.89), `create_review_case_if_needed`, `MARK_SALES_RECORD_PENDING_REVIEW`, return True.

- [ ] **Step 4: Rename `propose_machine_unit_matches_for_pending_sales` → `propose_vehicle_matches_for_pending_sales`** (`pipeline_sales.py:620-647`) — same orchestration, calls the renamed `_propose_one_pending_sale`. Update the log message to "Proposed N vehicle matches for pending sales".

- [ ] **Step 5: `main.py`** — update import (`propose_vehicle_matches_for_pending_sales`) and the call at lines 340-342 + log message.

- [ ] **Step 6: Rename test file**, commit + push, verify via `wpci home`.

```bash
git add services/ingestion/src/matching/vehicle_heuristic.py services/ingestion/src/pipeline_sales.py services/ingestion/src/main.py services/ingestion/tests/test_vehicle_heuristic.py services/ingestion/tests/test_sales_vehicle_matching.py
git rm services/ingestion/src/matching/machine_unit_heuristic.py services/ingestion/tests/test_machine_unit_heuristic.py services/ingestion/tests/test_sales_machine_unit_matching.py
git commit -m "feat(ingestion): vehicle+mobile/email auto-link heuristic with NRIC anti-match block

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Chat ingestion (`MENTIONS_VEHICLE`) + `OWNS_VEHICLE` explicit ownership + llm_prompts

**Files:**
- Modify: `services/ingestion/src/pipeline.py:22, 26-31, 194, 278-322` (`_write_chat_vehicle_observations` rename)
- Modify: `services/ingestion/src/llm_prompts.py:95, 106-110, 122-124`
- Test: `services/ingestion/tests/test_vehicle_queries.py` (chat mention part), `test_chat_*` if present

**Interfaces:**
- Consumes: `observations_from_chat_inquiries` (Task 3), `RESOLVE_EXISTING_VEHICLE_FOR_CHAT`, `LINK_CHAT_SOURCE_RECORD_MENTIONS_VEHICLE` (Task 2).
- Produces: chat conversations resolve to exactly-one existing Vehicle and link `SourceRecord -[:MENTIONS_VEHICLE]-> Vehicle`; chat never creates Vehicles.

- [ ] **Step 1: Rename `_write_chat_machine_unit_observations` → `_write_chat_vehicle_observations`** (`pipeline.py:278-322`) — for each chat inquiry observation: run `RESOLVE_EXISTING_VEHICLE_FOR_CHAT` (returns `vehicle_ids`); if `len(vehicle_ids) == 1`, `LINK_CHAT_SOURCE_RECORD_MENTIONS_VEHICLE`; else skip (no creation). Same exactly-one-match posture as before. Imports (lines 22, 26-31) swap to vehicle equivalents.

- [ ] **Step 2: Update call site** (`pipeline.py:194`) to the renamed method.

- [ ] **Step 3: `llm_prompts.py`** — rename inquiry fields: `machine_product`→`vehicle_product` (or keep `product` and add `is_vehicle` flag — pick `vehicle_product` for clarity), `lta_tag`, `serial_number` stay; `weak_identifiers` enum `machine_lta_tag`→`vehicle_lta_tag`, `machine_serial_number`→`vehicle_serial_number`, `machine_unit`→`vehicle` (lines 122-124). Update the prompt text at line 95 (`"product": product name...`) to reference vehicle where relevant.

- [ ] **Step 4: `OWNS_VEHICLE` explicit ownership** — search for any source that asserts explicit ownership (the old `LINK_PERSON_OWNS_UNIT` call sites). If none exist in the current code (the agents found BOUGHT/INVOLVES/MENTIONS but OWNS was defined but not clearly wired), document `LINK_PERSON_OWNS_VEHICLE` as available for a future explicit-ownership source; add `FLAG_VEHICLE_OWNER_CONFLICTS` invocation only where `OWNS_VEHICLE` is written. If no current source writes OWNS, leave the query defined but uncalled (no test needed beyond query shape in Task 2). Note this in the plan's follow-ups.

- [ ] **Step 5: Tests + commit + push + verify via `wpci home`.**

```bash
git add services/ingestion/src/pipeline.py services/ingestion/src/llm_prompts.py services/ingestion/tests/
git commit -m "feat(ingestion): chat MENTIONS_VEHICLE + llm_prompts vehicle fields

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Exclusions rename (`VehicleIdentifierKey` / `vehicle_identifiers`)

**Files:**
- Modify: `services/ingestion/src/exclusions.py:26-29, 41, 97-108, 165-181`
- Modify: `services/ingestion/src/exclusion_config.py:13-18, 21-23, 64-81, 104-106`
- Modify: `services/ingestion/src/ingestion_config.py:13, 59-61`
- Modify: `services/ingestion/src/connectors/bitrix/connector.py:352`
- Test: `services/ingestion/tests/test_exclusions.py`, `test_exclusion_config.py`, `test_ingestion_config.py`

**Interfaces:**
- Produces: `VehicleIdentifierKey(machine_product→vehicle_product, lta_tag, serial_number)` (keep `vehicle_product` for the exclusion match key — the exclusion is by product name + identifier, unchanged semantics), `ExclusionContext.vehicle_identifiers: frozenset[VehicleIdentifierKey]`, `normalized_vehicle_identifier_set`, `is_excluded_vehicle_observation`, config `VehicleIdentifierExclusion` TypedDict, `VEHICLE_IDENTIFIER_KEYS`, `_vehicle_identifier_list`.

- [ ] **Step 1: Rename** in `exclusions.py`: `MachineUnitIdentifierKey`→`VehicleIdentifierKey` (field `machine_product`→`vehicle_product`), `ExclusionContext.machine_unit_identifiers`→`vehicle_identifiers`, `normalized_machine_unit_identifier_set`→`normalized_vehicle_identifier_set`, `is_excluded_machine_unit_observation`→`is_excluded_vehicle_observation`. The observation passed in is now a `VehicleObservation` (Task 3) — match on `vehicle_product` + `lta_tag`/`serial_number`.

- [ ] **Step 2: Rename** in `exclusion_config.py`: `MachineUnitIdentifierExclusion`→`VehicleIdentifierExclusion` (field `machine_product`→`vehicle_product`), `MACHINE_UNIT_IDENTIFIER_KEYS`→`VEHICLE_IDENTIFIER_KEYS = frozenset({"vehicle_product","lta_tag","serial_number"})`, `_machine_unit_identifier_list`→`_vehicle_identifier_list`, `ExclusionFile.machine_unit_identifiers`→`vehicle_identifiers` + loader wiring.

- [ ] **Step 3: `ingestion_config.py`** — import `_vehicle_identifier_list`, wire `vehicle_identifiers` at lines 59-61.

- [ ] **Step 4: `connectors/bitrix/connector.py:352`** — rename the pass-through `machine_unit_identifiers=...`→`vehicle_identifiers=...`.

- [ ] **Step 5: Update tests** in `test_exclusions.py` / `test_exclusion_config.py` / `test_ingestion_config.py` to the new names.

- [ ] **Step 6: Commit + push + verify via `wpci home`.**

```bash
git add services/ingestion/src/exclusions.py services/ingestion/src/exclusion_config.py services/ingestion/src/ingestion_config.py services/ingestion/src/connectors/bitrix/connector.py services/ingestion/tests/test_exclusions.py services/ingestion/tests/test_exclusion_config.py services/ingestion/tests/test_ingestion_config.py
git commit -m "refactor(ingestion): rename machine_unit exclusions to vehicle

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: API types + mappers (`VehicleSummary`, `SalesVehicleSummary`, `NonVehicleLine`)

**Files:**
- Modify: `services/api/src/types.py:156-165, 188, 429-461`
- Modify: `services/api/src/graph/mappers.py:38, 54, 136-163, 216, 678-700, 717-762, ~899`
- Test: `services/api/tests/test_person_loyalty_machine_units.py`→`test_person_loyalty_vehicles.py`, `test_review_mappers.py`

**Interfaces:**
- Produces: `VehicleSummary` (Pydantic), `Person.vehicles: list[VehicleSummary] | None`, `SalesVehicleSummary`, `NonVehicleLine`, `SalesOrderSummary.non_vehicle_lines: list[NonVehicleLine]`; mappers `_map_vehicles`, `_map_sales_summary` (Vehicle + non_vehicle_lines).

- [ ] **Step 1: `types.py`** — rename `MachineUnitSummary`→`VehicleSummary` (fields: `vehicle_id, product_sku, product, lta_tag, serial_number, relationship, is_active, conflict_flag, observed_at`); `Person.machine_units`→`Person.vehicles`; `SalesUnitSummary`→`SalesVehicleSummary` (fields `vehicle_id, product, normalized_lta_tag, normalized_serial_number, conflict_flag`); add `NonVehicleLine(BaseModel)` (fields per spec §Order enrichment); `SalesOrderSummary` += `non_vehicle_lines: list[NonVehicleLine] = Field(default_factory=list)`.

- [ ] **Step 2: `mappers.py`** — rename `_map_machine_units`→`_map_vehicles` (sort OWNS_VEHICLE before BOUGHT_VEHICLE, dedup by `vehicle_id`), update the import at line 38, the call in `map_person` at line 216. Rebuild `_map_sales_summary(sales_order, sales_vehicles, non_vehicle_lines)` (line 678) to produce `SalesVehicleSummary` list + `NonVehicleLine` list. Update `_map_comparison_entity` (717) / `_map_source_record_comparison` (744) / `map_review_case_detail` (~899) to forward `sales_vehicles` + `non_vehicle_lines`.

- [ ] **Step 3: Tests** — rename `test_person_loyalty_machine_units.py`→`test_person_loyalty_vehicles.py`; update `test_review_mappers.py` (`_map_sales_summary` with `non_vehicle_lines`, `map_review_case_detail` carries `sales_vehicles`).

- [ ] **Step 4: Commit + push + verify via `wpci home`** (API package: ruff + mypy --strict + pytest).

```bash
git add services/api/src/types.py services/api/src/graph/mappers.py services/api/tests/test_person_loyalty_vehicles.py services/api/tests/test_review_mappers.py
git rm services/api/tests/test_person_loyalty_machine_units.py
git commit -m "feat(api): VehicleSummary, SalesVehicleSummary, NonVehicleLine types + mappers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: API graph queries (persons, review) + public-page strip

**Files:**
- Modify: `services/api/src/graph/queries/persons.py:40, 78-108`
- Modify: `services/api/src/graph/queries/review.py:166-214, 335-366` (+ rename `LINK_REVIEW_SALES_BOUGHT_UNIT`→`LINK_REVIEW_SALES_BOUGHT_VEHICLE`)
- Modify: `services/api/src/graph/queries/__init__.py:113-125, 217-230`
- Modify: `services/api/src/routes/public_pages.py:36-40, 188`
- Test: `services/api/tests/test_public_person_excludes_loyalty.py`, `test_review_repository_merge.py` (query-name imports)

- [ ] **Step 1: `persons.py` `GET_PERSON_BY_ID`** (line 78) — replace `OPTIONAL MATCH (person)-[rel:OWNS_UNIT|BOUGHT_UNIT]->(u:MachineUnit)` with `OPTIONAL MATCH (person)-[rel:OWNS_VEHICLE|BOUGHT_VEHICLE]->(v:Vehicle)`; rewrite the `collect(CASE ...)` to project `vehicle_id, product_sku, product, lta_tag, serial_number, rel_type, is_active, conflict_flag, observed_at` (rename the collected key `machine_units`→`vehicles` at lines 105/108).

- [ ] **Step 2: `review.py` `GET_REVIEW_CASE`** (line 174) — `INVOLVES_UNIT`→`INVOLVES_VEHICLE`, `MachineUnit`→`Vehicle`; collect `sales_vehicles` (from `v:Vehicle`) with `vehicle_id, product, normalized_lta_tag, normalized_serial_number, conflict_flag`; also `collect` the `Order.non_vehicle_lines` (single value, pass through). Return `sales_vehicles` + `non_vehicle_lines` at lines 213-214.

- [ ] **Step 3: `review.py` link queries** — `LINK_REVIEW_SALES_PURCHASED_ORDER` (line 340): `INVOLVES_UNIT`→`INVOLVES_VEHICLE`, `MachineUnit`→`Vehicle`. `LINK_REVIEW_SALES_BOUGHT_UNIT` (line 352) → rename `LINK_REVIEW_SALES_BOUGHT_VEHICLE`: `MATCH (o:Order)-[:INVOLVES_VEHICLE {...}]->(v:Vehicle)` then `MERGE (p)-[rel:BOUGHT_VEHICLE {...}]->(v)`. Update `__init__.py` exports + `__all__` (drop `LINK_REVIEW_SALES_BOUGHT_UNIT`, add `LINK_REVIEW_SALES_BOUGHT_VEHICLE`).

- [ ] **Step 4: `public_pages.py`** — `_strip_public_person` (line 37): `model_copy(update={"loyalty": None, "vehicles": None, "non_vehicle_lines": None})` (strip customer-specific vehicle ownership + non-vehicle line detail). Update docstring (line 36). `_strip_public_sales_order` (line 40): also strip `non_vehicle_lines` if it carries customer-specific detail (merchant/serial). Public `GET /persons/{token}/sales` (line 188) uses the updated stripper.

- [ ] **Step 5: Tests** — `test_public_person_excludes_loyalty.py`: assert `stripped.vehicles is None` and `stripped.non_vehicle_lines is None` (rename fixture from `machine_units`/`MachineUnitSummary`→`vehicles`/`VehicleSummary`). `test_review_repository_merge.py`: update query-name imports (`LINK_REVIEW_SALES_BOUGHT_VEHICLE`).

- [ ] **Step 6: Commit + push + verify via `wpci home`.**

```bash
git add services/api/src/graph/queries/persons.py services/api/src/graph/queries/review.py services/api/src/graph/queries/__init__.py services/api/src/routes/public_pages.py services/api/tests/test_public_person_excludes_loyalty.py services/api/tests/test_review_repository_merge.py
git commit -m "feat(api): Vehicle graph queries + public-page strip of vehicles/non_vehicle_lines

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: API repository (`review.py`) link path

**Files:**
- Modify: `services/api/src/repositories/neo4j/review.py:30-33, 250-266, 286-314, 396`
- Test: `services/api/tests/test_review_repository_merge.py`

- [ ] **Step 1: Imports** (lines 30-33) — `LINK_REVIEW_SALES_BOUGHT_UNIT`→`LINK_REVIEW_SALES_BOUGHT_VEHICLE` (the other imports `LINK_REVIEW_SALES_PURCHASED_ORDER`, `MARK_REVIEW_SALES_RECORD_LINKED`, `MARK_REVIEW_SALES_RECORD_UNRESOLVED` unchanged).

- [ ] **Step 2: `_sales_link_merge_tx`** (lines 250-266) — `tx.run(LINK_REVIEW_SALES_BOUGHT_VEHICLE, ...)` instead of `LINK_REVIEW_SALES_BOUGHT_UNIT`. The approve path now links `PURCHASED` + `BOUGHT_VEHICLE` to the candidate Person. The merge-fallback (`if linked_result.single() is None: return ActionResult(merge_not_applicable=True)`) unchanged.

- [ ] **Step 3: `_action_tx`** (lines 286-314, 396) — the merge fallback into `_sales_link_merge_tx` stays; the reject path `MARK_REVIEW_SALES_RECORD_UNRESOLVED` unchanged (status flag, vehicle-agnostic).

- [ ] **Step 4: Tests** — `test_review_repository_merge.py`: `test_merge_sales_link_approves_and_links` asserts `LINK_REVIEW_SALES_BOUGHT_VEHICLE` is called (rename); `test_merge_returns_not_applicable_when_no_persons_and_no_sales_link` mocks the renamed query; `test_reject_marks_sales_record_unresolved` unchanged (still `MARK_REVIEW_SALES_RECORD_UNRESOLVED`); `test_manual_no_match_creates_review_lock_after_action` unchanged.

- [ ] **Step 5: Commit + push + verify via `wpci home`.**

```bash
git add services/api/src/repositories/neo4j/review.py services/api/tests/test_review_repository_merge.py
git commit -m "refactor(api): review repo uses BOUGHT_VEHICLE link path

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: frontend2 — `VehicleSummary` + `VehiclesSidebarCard`

**Files:**
- Modify: `services/frontend2/src/lib/api-types.ts:70, 84-89`
- Modify: `services/frontend2/src/app/persons/[personId]/page.tsx:6, 490, 654-714`
- Test: `npm run typecheck` (via CI; no host run)

- [ ] **Step 1: `api-types.ts`** — rename `MachineUnitSummary`→`VehicleSummary` (fields: `vehicle_id, product_sku, product, lta_tag, serial_number, relationship: "OWNS" | "BOUGHT", is_active, conflict_flag, observed_at`); `Person.machine_units`→`Person.vehicles`. Add `NonVehicleLine` interface + `SalesOrderSummary.non_vehicle_lines: NonVehicleLine[]` (only if frontend2 consumes the review-case detail; if not currently consumed, add the types for completeness but they may be unused until the review UI surfaces them — keep them to avoid drift).

- [ ] **Step 2: `page.tsx`** — update import (line 6): `MachineUnitSummary`→`VehicleSummary`; render `<VehiclesSidebarCard units={person.vehicles} />` (line 490); rename the component (line 654) `MachineUnitsSidebarCard`→`VehiclesSidebarCard` with header "Vehicles", empty-state "No vehicles linked to this person.", and field reads `u.vehicle_id` (key), `u.product_sku`/`u.product`, `u.serial_number`, `u.lta_tag`, `u.relationship`, `u.is_active`, `u.conflict_flag`, `u.observed_at`.

- [ ] **Step 3: Verify zero net warnings** — `git stash`, note `npx eslint src` baseline (errors only, should be 0 on clean tree); `git stash pop`; confirm no new errors/warnings from the rename. Do NOT run `npm run lint`/`typecheck`/`build` on the host to verify — push and read the frontend2 typecheck + eslint-errors-only + (DEV) `next build` steps via `wpci home`.

- [ ] **Step 4: Commit + push + verify via `wpci home`.**

```bash
git add services/frontend2/src/lib/api-types.ts services/frontend2/src/app/persons/[personId]/page.tsx
git commit -m "feat(frontend2): VehiclesSidebarCard + VehicleSummary types

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 13: Migration — drop MachineUnit nodes/rels

**Files:**
- Modify: `infra/neo4j/init.cypher` (already done in Task 1 — constraints/indexes)
- Create: `services/api/src/migrations/vehicle_remodel.py` (or a one-shot Cypher script invoked once) — drop MachineUnit nodes + relationships
- Test: `services/api/tests/test_migration_vehicle_remodel.py` (if migrations are tested)

- [ ] **Step 1: Migration script** — a one-shot Cypher cleanup (run once against the existing graph before re-ingestion):
```cypher
MATCH ()-[r:INVOLVES_UNIT|BOUGHT_UNIT|OWNS_UNIT|MENTIONS_UNIT]->() DELETE r;
MATCH (mu:MachineUnit) DETACH DELETE mu;
```
Run as a startup migration or a manual `cypher-shell` invocation against the dev graph (NOT staging/prod without explicit authorization). Place the script under `services/api/src/migrations/` following the existing migration pattern (check how prior startup migrations are registered — the spec mentions "A startup migration reclassifies legacy system/public_record records"; follow that pattern).

- [ ] **Step 2: Test** — if the repo has migration tests, add one asserting the script drops all `:MachineUnit` nodes + the four relationship types and leaves `:Vehicle` untouched (empty before re-ingestion).

- [ ] **Step 3: Commit + push + verify via `wpci home`.**

```bash
git add services/api/src/migrations/vehicle_remodel.py services/api/tests/test_migration_vehicle_remodel.py
git commit -m "feat(api): migration to drop MachineUnit nodes and relationships

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 14: Limited-100 regeneration (local-only) + re-ingest

**Files (untracked, gitignored — local dev only):**
- Modify: `.dumps/limited-100/generate_limited_dumps.py:257-266, 447-453`
- Regenerate: `.dumps/limited-100/eko_sales_100.sql`, `speedzone_sales_100.sql`, `onediver_sales_100.sql`

- [ ] **Step 1: Generator — phppos sales_100 block** (lines 257-266) — add `phppos_categories` and filtered `phppos_customers`:
```python
write_mysql(
    OUT / f"{prefix}_sales_100.sql",
    {
        "phppos_sales": sales,
        "phppos_sales_items": by_int(tables.rows("phppos_sales_items"), "sale_id", sale_ids),
        "phppos_items": by_int(tables.rows("phppos_items"), "item_id", item_ids),
        "phppos_categories": tables.rows("phppos_categories"),  # new — full (small)
        "phppos_customers": by_int(                              # new — filtered to sales' customers
            tables.rows("phppos_customers"), "id",
            {_row_int(r, "customer_id") for r in sales},
        ),
    },
)
```
(Verify `_row_int(r, "customer_id")` is the right key for `phppos_sales`; adjust if the customer FK column differs.)

- [ ] **Step 2: Generator — onediver sales_100 block** (lines 447-453) — add `sales_order_items` + `products`:
```python
od_item_ids = {_row_int(r, "product_id") for r in od_tables.rows("sales_order_items") if _row_int(r, "sales_order_id") in od_sale_ids}
write_mysql(
    OUT / "onediver_sales_100.sql",
    {
        "sales_orders": od_sales,
        "profiles": od_profiles,
        "sales_order_items": by_int(od_tables.rows("sales_order_items"), "sales_order_id", od_sale_ids),  # new
        "products": by_int(od_tables.rows("products"), "id", od_item_ids),                                # new
    },
)
```
(`od_sale_ids` must be computed from `od_sales` before this block — check the existing onediver section for the variable name and adjust.)

- [ ] **Step 3: Regenerate (one-shot, local)** — from the repo root with the ingestion venv:
```bash
uv run --package profile-unifier-ingestion python .dumps/limited-100/generate_limited_dumps.py
```
(DUMPS_ROOT defaults to `.dumps`; full dumps present at `.dumps/`.) Confirm the regenerated `eko_sales_100.sql` / `speedzone_sales_100.sql` now contain `CREATE TABLE phppos_categories` + `CREATE TABLE phppos_customers` blocks, and `onediver_sales_100.sql` contains `sales_order_items` + `products`. No commit — these files are gitignored.

- [ ] **Step 4: Re-ingest locally** (manual, `docker compose up -d` then dispatch via Celery per the repo's ingestion dispatch rule — never call `run_ingestion()` directly):
```bash
docker compose up -d
# dispatch each sales source in dump mode (limited-100) via the Celery task
# e.g. for eko: run_ingestion_task.delay("eko_phppos:sales", mode="dump", dump_path="limited-100/eko_sales_100.sql")
# (follow the source_key/dump_path table in CLAUDE.md; use the :sales source keys)
```
Verify in Neo4j browser: `:Vehicle` nodes exist for e-bikes/PMDs/motorcycles; `Order.non_vehicle_lines` populated for helmets/locks/tyres; `BOUGHT_VEHICLE` from resolved persons; `MachineUnit` nodes are gone. Poll the worker every 5 min while ingestion tasks are running.

- [ ] **Step 5: No commit** (gitignored). Document the regen + re-ingest in the PR description as a manual verification step (not a CI gate).

---

## Self-Review

**Spec coverage:** §1 overview/naming → Tasks 1-12 (all renames + new node). §2 Vehicle identity (LTA-global + per-source serial + promotion) → Task 2 `UPSERT_VEHICLE` + Task 3 validation. §3 classification rule → Task 3 `vehicle_categories.py` + Task 4 connector category resolution. §4 Order enrichment → Task 5 (`non_vehicle_lines`). §5 matching (FK-first, auto-link 0.90, NRIC block) → Task 6. §6 chat → Task 7. §7 migration → Task 13. §8 API+frontend → Tasks 9-12. §9 phasing → matches task order. §10 limited-100 → Task 14. All spec sections covered.

**Placeholder scan:** No TBD/TODO. Task 7 Step 4 flags `OWNS_VEHICLE` as "defined but possibly uncalled" — that's an honest unknown to resolve during implementation (search for `LINK_PERSON_OWNS_UNIT` call sites), not a placeholder. Task 4 Step 4 notes the speedzone brand-category fallback to re-examine — explicit, not a placeholder.

**Type consistency:** `VehicleObservation` (Task 3) fields `product_sku, product, manufacturer, model, lta_tag, serial_number` match `UPSERT_VEHICLE` params (Task 2) and `_write_vehicle_observations` usage (Task 5). `VehicleCandidate` (Task 6) fields match `FIND_VEHICLE_CANDIDATES_FOR_SALES` returns (Task 2: `person_id, vehicle_id, rel_type, is_active, conflict_flag, last_confirmed_at, contact_channels, nric_blocked`). `VehicleSummary`/`SalesVehicleSummary`/`NonVehicleLine` (Task 9) match `_map_vehicles`/`_map_sales_summary` (Task 9) and frontend2 types (Task 12). Relationship types `INVOLVES_VEHICLE`/`BOUGHT_VEHICLE`/`OWNS_VEHICLE`/`MENTIONS_VEHICLE` consistent across Tasks 2, 5, 7, 10, 11.

One known tension to resolve during implementation: `UPSERT_VEHICLE` per-source serial lookup uses `observed_product_skus_s` (a list property) for membership — if list-property membership in Cypher proves unwieldy, fall back to `v.product_sku = $product_sku` (first-observed SKU) + `source_system_key IN v.source_systems`; the LTA path already handles cross-source so serial-only per-source matching by first-SKU + source is acceptable. Document the chosen approach in the code comment.