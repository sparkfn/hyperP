# Spec 1 — Record-type taxonomy split (`public_record` → `bankruptcy` + `rental_flat`)

**Date:** 2026-06-08
**Status:** Approved (design, revised after code inspection)
**Sequel:** Spec 2 — Per-record-type merge criteria (bankruptcy NRIC+name gate, relationship/sales phone+name triggers). Out of scope here.

## Correction to the original brainstorm

During planning, inspection of the ingestion code showed the address-only path **already exists**:

- `services/ingestion/src/main.py` defines `_ADDRESS_ONLY_SOURCES = frozenset({"sgrentalflats"})` and `_is_address_only_source(source_key)`. The dispatcher routes `sgrentalflats` envelopes to `src/pipeline_addresses.py:ingest_address_record`, which persists the SourceRecord and links it to a shared Address (`DESCRIBES_ADDRESS`) **without** creating or resolving a Person.
- This routing is keyed on `source_system` (`sgrentalflats`), *before* the person pipeline (`pipeline.py`) is ever reached. A test pins it: `tests/test_rental_flats_address_pipeline.py`.

So the earlier "rental-flat ingestion creates a nameless junk Person" concern was incorrect — rental-flats never reaches the person pipeline. The "address-only, never a Person" decision is **already satisfied today**. This spec therefore does **not** add a place-record pipeline branch; it only renames the type and migrates data. Scope is smaller and lower-risk than first sketched.

## Goal

Replace the single `public_record` record type with two semantically distinct types so they can carry different matching behaviour (Spec 2):

- **`bankruptcy`** — a government register *about a person* (SG Bankruptcy Register). Emits a verified `nric` identifier + `full_name`. Runs through the person pipeline and is a member of `SYSTEM_FAMILY` (so the deterministic NRIC merge layer applies, as `public_record` does today).
- **`rental_flat`** — a government register *about a place* (SG Rental Flats). Emits no person identifier and no name — only address attributes. Already routed as address-only by `source_system`; **not** a member of `SYSTEM_FAMILY`.

## Background — current behavior (verified in code)

- `RecordType` (`services/ingestion/src/models.py`) has 5 active values: `identity`, `public_record`, `relationship`, `conversation`, `sales` (the legacy `system` value was already removed; only data may still carry it).
- `SYSTEM_FAMILY = {identity, public_record, relationship}` — members share matching behaviour (deterministic merge layer + heuristic scoring run identically). Pinned by `tests/test_match_engine_system_family.py`.
- `public_record` is emitted by two connectors with different shapes:
  - `connectors/sggov/bankruptcy.py` → verified `nric` identifier + `full_name` attribute (person register; runs the person pipeline).
  - `connectors/sggov/rental_flats.py` → `identifiers=[]`, no name, address attributes only (place register; routed address-only).
- Startup migration `graph/migrations.py:backfill_record_type_subtypes` runs the single Cypher in `graph/queries/maintenance.py:BACKFILL_RECORD_TYPE_SUBTYPES`, which reclassifies legacy `record_type='system'` rows by `source_system` (currently mapping `sgbankruptcy`/`sgrentalflats` → `public_record`).
- Display: the frontend renders record types via `titleCase(record.record_type)` (no per-type map), so new values self-label: `bankruptcy → "Bankruptcy"`, `rental_flat → "Rental Flat"`.

## Decisions (locked during brainstorming)

1. **Names:** `bankruptcy` + `rental_flat` (source-named). Replaces `public_record` entirely.
2. **`rental_flat` is address-only** — already true via `_is_address_only_source`; preserved, no new code path. Routing stays keyed on `source_system`.
3. **Existing junk Person nodes:** none exist (the premise was wrong); nothing to clean. Migration changes `record_type` strings only.
4. **Spec split:** taxonomy ships first (this spec); merge criteria ship second (Spec 2).

## Changes

### 1. `RecordType` enum — `services/ingestion/src/models.py`

- Remove `PUBLIC_RECORD = "public_record"`; add `BANKRUPTCY = "bankruptcy"` and `RENTAL_FLAT = "rental_flat"`.
- New active set (6 values): `identity`, `bankruptcy`, `rental_flat`, `relationship`, `conversation`, `sales`.
- `SYSTEM_FAMILY = frozenset({RecordType.IDENTITY, RecordType.BANKRUPTCY, RecordType.RELATIONSHIP})` — `bankruptcy` takes `public_record`'s former slot; `rental_flat` is **excluded**.
- Update the enum docstring to describe `bankruptcy` (person register) and `rental_flat` (place register, address-only).

### 2. Connectors

- `connectors/sggov/bankruptcy.py` → `record_type="bankruptcy"`.
- `connectors/sggov/rental_flats.py` → `record_type="rental_flat"`.
- `connectors/fundbox/builders.py:build_envelope` — its `record_type` parameter is typed with an inline `Literal["identity", "public_record", "relationship", "conversation", "sales"]`. Replace `"public_record"` with `"bankruptcy", "rental_flat"`; update the docstring mention.

### 3. Startup migration — `graph/queries/maintenance.py` + `graph/migrations.py`

Rewrite the single `BACKFILL_RECORD_TYPE_SUBTYPES` Cypher so it reclassifies **both** legacy `system` rows and any existing `public_record` rows by `source_system`, in one idempotent statement:

```cypher
MATCH (sr:SourceRecord)
WHERE sr.record_type IN ['system', 'public_record']
SET sr.record_type = CASE
    WHEN sr.source_system ENDS WITH ':contacts' THEN 'relationship'
    WHEN sr.source_system = 'sgbankruptcy'  THEN 'bankruptcy'
    WHEN sr.source_system = 'sgrentalflats' THEN 'rental_flat'
    ELSE 'identity'
END
RETURN count(sr) AS updated
```

- Keeping it a single query means `apply_data_migrations` and its tests stay structurally unchanged (still exactly one query executed).
- Idempotent: after the run no row matches the `WHERE`, so a re-run updates 0.
- Update the `backfill_record_type_subtypes` docstring and the `BACKFILL_RECORD_TYPE_SUBTYPES` comment to reflect the `system` + `public_record` → 4-way mapping.

### 4. Address-only routing — `services/ingestion/src/main.py` (no functional change)

Routing is keyed on `source_system` (`sgrentalflats`), independent of `record_type`, so the split does not affect it. No code change required. (Optional robustness — *not* in scope: also treat `RecordType.RENTAL_FLAT` as address-only. Deferred; YAGNI.)

### 5. API mirror — `services/api/src/types.py`

- `SourceRecordTypeLiteral = Literal["identity", "public_record", "relationship", "conversation", "sales"]` → replace `"public_record"` with `"bankruptcy", "rental_flat"`.
- `graph/mappers.py` derives `_RECORD_TYPES` via `get_args(SourceRecordTypeLiteral)`, so it updates automatically. No other API code references `public_record`.

### 6. Frontend mirror — `services/frontend2/src/lib/api-types.ts`

- `SourceRecordType` union: replace `"public_record"` with `"bankruptcy"`, `"rental_flat"`.
- No label map change — `titleCase` self-labels the new values. Existing `record_type === "conversation"` colour/badge branches are unaffected.

### 7. Docs (bundled in the same implementation commit)

- `CLAUDE.md` — "Source record types" design note: describe the `bankruptcy`/`rental_flat` split, `SYSTEM_FAMILY` membership, and that `rental_flat` is address-only.
- `docs/profile-unifier-graph-schema.md` (record_type enum comment + system-family description + the `public_record` bullet) and `docs/profile-unifier-entity-and-sales.md` (the `record_type` enum list) — update the enumerations to the new taxonomy.
- Leave the historical specs under `docs/superpowers/specs/` untouched.

## Testing

Update the tests that pin the old taxonomy, and keep the matching-equivalence invariant (Spec 1 does **not** change matching behaviour):

- `tests/test_migrations_record_type.py` — assert the new Cypher: `WHERE sr.record_type IN ['system', 'public_record']`, `= 'sgbankruptcy'  THEN 'bankruptcy'`, `= 'sgrentalflats' THEN 'rental_flat'`, `ENDS WITH ':contacts' THEN 'relationship'`, `ELSE 'identity'`. The runner tests (`apply_data_migrations` runs exactly one query) stay as-is.
- `tests/test_match_engine_system_family.py` — change `_FAMILY` to `(IDENTITY, BANKRUPTCY, RELATIONSHIP)` and the membership assertion accordingly; the equivalence assertions are unchanged (identity/bankruptcy/relationship still match identically in Spec 1).
- `tests/test_sggov_bankruptcy_connector.py` — assert `record_type == "bankruptcy"`.
- `tests/test_sggov_rental_flats_connector.py` — assert `record_type == "rental_flat"`.
- API/frontend type unions accept the two new values and reject `public_record` (compile-time; covered by typecheck).

## Verification

- `uv run --package profile-unifier-ingestion ruff check` + `mypy --strict` (scoped to changed files).
- `uv run --package profile-unifier-api ruff check services/api/src` + `mypy --strict services/api/src`.
- `cd services/frontend2 && npm run typecheck` (zero **net** new ESLint warnings).
- `uv run pytest services/ingestion/tests` green.
- Dev reset-and-ingest of `sgbankruptcy` + `sgrentalflats`: bankruptcy records resolve to Persons with `record_type="bankruptcy"`; rental-flat records produce Address + SourceRecord (`record_type="rental_flat"`) with no Person.

## Out of scope (→ Spec 2)

- `bankruptcy`: require a partial name match on top of the exact-NRIC deterministic merge (gate in `deterministic.py`). This will deliberately break the identity/bankruptcy matching-equivalence invariant — Spec 2 updates `test_match_engine_system_family.py` accordingly.
- `relationship`: phone + partial-name trigger → auto-merge (promotion in `heuristic.py`), guarded by the shared hard-conflict blockers.
- `sales`: phone + partial-name **fallback** for orphan orders with no usable customer FK (`pipeline_sales.py`).
