# Record-type taxonomy split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single `public_record` record type with `bankruptcy` (person register) and `rental_flat` (place register, already address-only), across ingestion, API, frontend, migration, and docs.

**Architecture:** A mechanical taxonomy rename. `bankruptcy` inherits `public_record`'s membership in `SYSTEM_FAMILY` (deterministic NRIC merge layer still applies); `rental_flat` is excluded and continues to route address-only via the existing `_is_address_only_source` (keyed on `source_system`, unchanged). A single idempotent startup migration reclassifies existing `system`/`public_record` data by `source_system`. No matching behaviour changes in this plan (that is Spec 2).

**Tech Stack:** Python 3.12 (uv workspace, `profile-unifier-ingestion` + `profile-unifier-api`), Neo4j/Cypher, Next.js/TypeScript (`frontend2`). Test runner: pytest. Lint/type: ruff + mypy --strict; tsc.

**Commit note:** This repo's standing rule is *never commit without explicit user instruction*. The `Commit` steps below are the intended rhythm, but the executor must get an explicit go-ahead before running any `git commit`. If running unattended, stage and stop at each commit boundary.

---

### Task 1: Split the `RecordType` enum and `SYSTEM_FAMILY`

**Files:**
- Modify: `services/ingestion/src/models.py:47-81`
- Test: `services/ingestion/tests/test_match_engine_system_family.py`

- [ ] **Step 1: Update the equivalence/membership test to the new family**

In `services/ingestion/tests/test_match_engine_system_family.py`, change the family tuple (line 26) and the module docstring references from `public_record` to `bankruptcy`:

```python
_FAMILY = (RecordType.IDENTITY, RecordType.BANKRUPTCY, RecordType.RELATIONSHIP)
```

The body of `test_system_family_membership_is_exactly_the_three_subtypes` already asserts `SYSTEM_FAMILY == frozenset(_FAMILY)` and that `CONVERSATION`/`SALES` are absent — leave those. Add one line to that test asserting `rental_flat` is excluded:

```python
    assert RecordType.RENTAL_FLAT not in SYSTEM_FAMILY
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_match_engine_system_family.py -q`
Expected: FAIL with `AttributeError: RECORDTYPE has no attribute 'BANKRUPTCY'` (and `RENTAL_FLAT`).

- [ ] **Step 3: Edit the enum and `SYSTEM_FAMILY`**

In `services/ingestion/src/models.py`, replace the members block (currently lines ~68-72):

```python
    IDENTITY = "identity"
    PUBLIC_RECORD = "public_record"
    RELATIONSHIP = "relationship"
    CONVERSATION = "conversation"
    SALES = "sales"
```

with:

```python
    IDENTITY = "identity"
    BANKRUPTCY = "bankruptcy"
    RENTAL_FLAT = "rental_flat"
    RELATIONSHIP = "relationship"
    CONVERSATION = "conversation"
    SALES = "sales"
```

Replace `SYSTEM_FAMILY` (currently lines ~79-81):

```python
SYSTEM_FAMILY: frozenset[RecordType] = frozenset(
    {RecordType.IDENTITY, RecordType.PUBLIC_RECORD, RecordType.RELATIONSHIP}
)
```

with:

```python
SYSTEM_FAMILY: frozenset[RecordType] = frozenset(
    {RecordType.IDENTITY, RecordType.BANKRUPTCY, RecordType.RELATIONSHIP}
)
```

Update the enum docstring (lines ~50-65): replace the `public_record` bullet with two bullets — `bankruptcy` ("government register about a person — SG Bankruptcy Register; carries verified NRIC + name; person pipeline; member of SYSTEM_FAMILY") and `rental_flat` ("government register about a place — SG Rental Flats; address attributes only; routed address-only by source_system; NOT in SYSTEM_FAMILY"). Update the opening "system family" sentence to list `identity`, `bankruptcy`, `relationship`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest services/ingestion/tests/test_match_engine_system_family.py -q`
Expected: PASS.

- [ ] **Step 5: Commit** (after explicit go-ahead)

```bash
git add services/ingestion/src/models.py services/ingestion/tests/test_match_engine_system_family.py
git commit -m "feat(ingestion): split public_record record type into bankruptcy + rental_flat"
```

---

### Task 2: Rewrite the backfill migration to map both `system` and `public_record`

**Files:**
- Modify: `services/ingestion/src/graph/queries/maintenance.py:13-22`
- Modify: `services/ingestion/src/graph/migrations.py:21-36`
- Test: `services/ingestion/tests/test_migrations_record_type.py:12-19`

- [ ] **Step 1: Update the migration query test to the new mapping**

In `services/ingestion/tests/test_migrations_record_type.py`, replace the assertions in `test_backfill_query_is_exported_and_maps_sources_to_subtypes` (lines 13-19) with:

```python
    query = queries.BACKFILL_RECORD_TYPE_SUBTYPES
    # Both legacy 'system' rows and intermediate 'public_record' rows are reclassified.
    assert "sr.record_type IN ['system', 'public_record']" in query
    assert "ENDS WITH ':contacts' THEN 'relationship'" in query
    assert "= 'sgbankruptcy'  THEN 'bankruptcy'" in query
    assert "= 'sgrentalflats' THEN 'rental_flat'" in query
    assert "ELSE 'identity'" in query
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_migrations_record_type.py -q`
Expected: FAIL on the new `IN ['system', 'public_record']` assertion.

- [ ] **Step 3: Rewrite the Cypher and docstrings**

In `services/ingestion/src/graph/queries/maintenance.py`, replace the `BACKFILL_RECORD_TYPE_SUBTYPES` constant (lines 13-22) with:

```python
BACKFILL_RECORD_TYPE_SUBTYPES = """
MATCH (sr:SourceRecord)
WHERE sr.record_type IN ['system', 'public_record']
SET sr.record_type = CASE
    WHEN sr.source_system ENDS WITH ':contacts' THEN 'relationship'
    WHEN sr.source_system = 'sgbankruptcy'  THEN 'bankruptcy'
    WHEN sr.source_system = 'sgrentalflats' THEN 'rental_flat'
    ELSE 'identity'
END
RETURN count(sr) AS updated
"""
```

In `services/ingestion/src/graph/migrations.py`, update the `backfill_record_type_subtypes` docstring (lines 22-27) to read that it reclassifies legacy `record_type='system'` **and** intermediate `record_type='public_record'` records into the current subtypes (`identity` / `bankruptcy` / `rental_flat` / `relationship`) by `source_system`, idempotently.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest services/ingestion/tests/test_migrations_record_type.py -q`
Expected: PASS (all four tests — the runner tests are unchanged because still exactly one query runs).

- [ ] **Step 5: Commit** (after explicit go-ahead)

```bash
git add services/ingestion/src/graph/queries/maintenance.py services/ingestion/src/graph/migrations.py services/ingestion/tests/test_migrations_record_type.py
git commit -m "feat(ingestion): backfill public_record data to bankruptcy/rental_flat by source"
```

---

### Task 3: Emit the new types from the connectors

**Files:**
- Modify: `services/ingestion/src/connectors/sggov/bankruptcy.py:118`
- Modify: `services/ingestion/src/connectors/sggov/rental_flats.py:68`
- Modify: `services/ingestion/src/connectors/fundbox/builders.py:190-205`
- Test: `services/ingestion/tests/test_sggov_bankruptcy_connector.py`, `services/ingestion/tests/test_sggov_rental_flats_connector.py`

- [ ] **Step 1: Update the connector tests to expect the new types**

Locate the `record_type` assertion in each connector test:

Run: `grep -n "public_record" services/ingestion/tests/test_sggov_bankruptcy_connector.py services/ingestion/tests/test_sggov_rental_flats_connector.py`

In `test_sggov_bankruptcy_connector.py`, change the asserted value from `"public_record"` to `"bankruptcy"`. In `test_sggov_rental_flats_connector.py`, change `"public_record"` to `"rental_flat"`. (These are equality assertions on the emitted envelope's `record_type`.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest services/ingestion/tests/test_sggov_bankruptcy_connector.py services/ingestion/tests/test_sggov_rental_flats_connector.py -q`
Expected: FAIL — emitted `record_type` is still `"public_record"`.

- [ ] **Step 3: Widen the `build_envelope` Literal and switch the connectors**

In `services/ingestion/src/connectors/fundbox/builders.py`, change the `record_type` parameter type (lines ~190-192) from:

```python
    record_type: Literal[
        "identity", "public_record", "relationship", "conversation", "sales"
    ] = "identity",
```

to:

```python
    record_type: Literal[
        "identity", "bankruptcy", "rental_flat", "relationship", "conversation", "sales"
    ] = "identity",
```

and in the docstring (lines ~199-203) replace the `"public_record"` mention with `"bankruptcy"` / `"rental_flat"` for government registers about a person / a place respectively.

In `services/ingestion/src/connectors/sggov/bankruptcy.py:118`, change `record_type="public_record"` to `record_type="bankruptcy"`.

In `services/ingestion/src/connectors/sggov/rental_flats.py:68`, change `record_type="public_record"` to `record_type="rental_flat"`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest services/ingestion/tests/test_sggov_bankruptcy_connector.py services/ingestion/tests/test_sggov_rental_flats_connector.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full ingestion suite + type/lint**

Run: `uv run pytest services/ingestion/tests -q`
Expected: PASS (no remaining `public_record` references in ingestion).
Run: `uv run --package profile-unifier-ingestion ruff check services/ingestion/src/models.py services/ingestion/src/graph/queries/maintenance.py services/ingestion/src/graph/migrations.py services/ingestion/src/connectors/sggov/bankruptcy.py services/ingestion/src/connectors/sggov/rental_flats.py services/ingestion/src/connectors/fundbox/builders.py`
Run: `uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src`
Expected: both clean.

- [ ] **Step 6: Commit** (after explicit go-ahead)

```bash
git add services/ingestion/src/connectors services/ingestion/tests/test_sggov_bankruptcy_connector.py services/ingestion/tests/test_sggov_rental_flats_connector.py
git commit -m "feat(ingestion): emit bankruptcy/rental_flat record_type from sggov connectors"
```

---

### Task 4: Update the API record-type literal

**Files:**
- Modify: `services/api/src/types.py:14-15`

- [ ] **Step 1: Edit the literal**

In `services/api/src/types.py`, replace:

```python
SourceRecordTypeLiteral = Literal[
    "identity", "public_record", "relationship", "conversation", "sales"
]
```

with:

```python
SourceRecordTypeLiteral = Literal[
    "identity", "bankruptcy", "rental_flat", "relationship", "conversation", "sales"
]
```

(`graph/mappers.py` derives its `_RECORD_TYPES` set from `get_args(SourceRecordTypeLiteral)`, so it updates automatically. No other API source references `public_record`.)

- [ ] **Step 2: Type-check and lint the API**

Run: `uv run --package profile-unifier-api mypy --strict services/api/src`
Run: `uv run --package profile-unifier-api ruff check services/api/src/types.py`
Expected: both clean.

- [ ] **Step 3: Run the API test suite**

Run: `uv run pytest services/api/tests -q`
Expected: PASS. If any fixture asserts `"public_record"`, update it to `"bankruptcy"` or `"rental_flat"` as appropriate to the fixture's source, then re-run.

- [ ] **Step 4: Commit** (after explicit go-ahead)

```bash
git add services/api/src/types.py
git commit -m "feat(api): add bankruptcy/rental_flat to SourceRecordTypeLiteral"
```

---

### Task 5: Update the frontend record-type union

**Files:**
- Modify: `services/frontend2/src/lib/api-types.ts:69-74`

- [ ] **Step 1: Edit the union and comment**

In `services/frontend2/src/lib/api-types.ts`, replace:

```typescript
export type SourceRecordType =
  | "identity"
  | "public_record"
  | "relationship"
  | "conversation"
  | "sales";
```

with:

```typescript
export type SourceRecordType =
  | "identity"
  | "bankruptcy"
  | "rental_flat"
  | "relationship"
  | "conversation"
  | "sales";
```

Update the preceding comment (lines 65-68) to note the taxonomy mirrors the API `SourceRecordTypeLiteral` and that `bankruptcy`/`rental_flat` replaced `public_record`. No label map exists — `titleCase` self-labels these as "Bankruptcy" / "Rental Flat".

- [ ] **Step 2: Type-check the frontend**

Run: `cd services/frontend2 && npm run typecheck`
Expected: clean (`tsc --noEmit` passes).

- [ ] **Step 3: Confirm no net-new lint warnings**

Run: `cd services/frontend2 && npm run lint` (note: the tree has a known ~18-warning baseline; confirm your change adds zero net warnings vs. a stash of your change).
Expected: no new warnings attributable to this change.

- [ ] **Step 4: Commit** (after explicit go-ahead)

```bash
git add services/frontend2/src/lib/api-types.ts
git commit -m "feat(frontend2): add bankruptcy/rental_flat to SourceRecordType"
```

---

### Task 6: Update documentation (bundled)

**Files:**
- Modify: `CLAUDE.md` (Source record types design note)
- Modify: `docs/profile-unifier-graph-schema.md:244,262-274`
- Modify: `docs/profile-unifier-entity-and-sales.md:241`

- [ ] **Step 1: Update `docs/profile-unifier-graph-schema.md`**

- Line ~244 enum comment: `record_type: 'identity', // identity | public_record | relationship | conversation | sales` → `// identity | bankruptcy | rental_flat | relationship | conversation | sales`.
- Lines ~262-263 "system family" sentence: list `identity`, `bankruptcy`, `relationship`.
- Replace the `**`public_record`**` bullet (line ~273) with two bullets: `**`bankruptcy`**` — government register about a person (SG Bankruptcy Register); verified NRIC + name; same matching behaviour as `identity` today (member of the system family). `**`rental_flat`**` — government register about a place (SG Rental Flats); address attributes only; routed address-only by `source_system`, never reaching the person pipeline; not a member of the system family.

- [ ] **Step 2: Update `docs/profile-unifier-entity-and-sales.md`**

Line ~241: change `(\`"identity"\`, \`"public_record"\`, \`"relationship"\`)` to `(\`"identity"\`, \`"bankruptcy"\`, \`"rental_flat"\`, \`"relationship"\`)`.

- [ ] **Step 3: Update `CLAUDE.md`**

In the "Source record types" design-decision paragraph, replace the description of the single `public_record` value with the `bankruptcy` (person register) + `rental_flat` (place register, address-only) split, note `SYSTEM_FAMILY = {identity, bankruptcy, relationship}`, and that `rental_flat` is routed address-only by `source_system` and is excluded from the system family. Update the "three system-family values" phrasing to reflect the new membership.

- [ ] **Step 4: Sanity-check no stray references remain**

Run: `grep -rn "public_record" services/ docs/profile-unifier-graph-schema.md docs/profile-unifier-entity-and-sales.md CLAUDE.md`
Expected: no matches in `services/`, `CLAUDE.md`, or the two canonical docs. (Matches may remain only in `docs/superpowers/specs/` historical specs — leave those.)

- [ ] **Step 5: Commit** (after explicit go-ahead)

```bash
git add CLAUDE.md docs/profile-unifier-graph-schema.md docs/profile-unifier-entity-and-sales.md docs/superpowers/specs/2026-06-08-record-type-taxonomy-design.md docs/superpowers/plans/2026-06-08-record-type-taxonomy.md
git commit -m "docs: describe bankruptcy/rental_flat record-type taxonomy"
```

---

## Self-Review

**Spec coverage:**
- Spec §Change 1 (enum + SYSTEM_FAMILY) → Task 1. ✓
- Spec §Change 2 (connectors + builders Literal) → Task 3. ✓
- Spec §Change 3 (migration query + docstrings) → Task 2. ✓
- Spec §Change 4 (address-only routing, no change) → no task needed; noted in plan architecture. ✓
- Spec §Change 5 (API literal) → Task 4. ✓
- Spec §Change 6 (frontend union) → Task 5. ✓
- Spec §Change 7 (docs) → Task 6. ✓
- Spec §Testing → test updates embedded in Tasks 1-4. ✓

**Placeholder scan:** Each code step shows the exact replacement text. The only `grep`-to-locate steps (connector-test assertion, API fixture) are because those exact line numbers weren't captured; the change to make is fully specified. No TBD/TODO.

**Type/name consistency:** `RecordType.BANKRUPTCY = "bankruptcy"` and `RecordType.RENTAL_FLAT = "rental_flat"` are used identically across enum, `SYSTEM_FAMILY`, migration Cypher string values, `build_envelope` Literal, `SourceRecordTypeLiteral`, and `SourceRecordType` union. Migration source-system mapping (`sgbankruptcy → bankruptcy`, `sgrentalflats → rental_flat`) matches the connectors' emitted values. `SYSTEM_FAMILY` excludes `rental_flat` consistently.
