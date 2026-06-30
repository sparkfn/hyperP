# Race/Ethnicity on the Person Profile — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task (inline execution; the implementer holds full context). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a display-only `race/ethnicity` golden-profile field to Person, sourced from fundbox (`basic_profiles.race` + `log_legacy_profiles.race`), surfaced in the UI and the override/merge editors, with no influence on matching.

**Architecture:** Race is a `HAS_FACT` attribute (`attribute_name = "race_ethnicity"`) stored as the golden `:Person` property `preferred_race_ethnicity`, mirroring `dob`. It is normalized at ingestion (trim + title-case). It is **not** an Identifier node and is **excluded** from completeness scoring and match/review signals.

**Tech Stack:** Python 3 / FastAPI / Neo4j (API + ingestion, `uv` workspace), Next.js 15 + TypeScript + MUI (frontend2).

**Spec:** `docs/superpowers/specs/2026-06-30-race-ethnicity-profile-design.md`

## Global Constraints

- Strict typing everywhere (mypy --strict API+ingestion; tsc --noEmit frontend2). No `Any`.
- Ruff format/check scoped to changed files. Frontend lint: zero net new warnings (budget already exceeded on clean tree).
- Ingestion dispatch is via Celery only (a re-ingest of fundbox populates existing persons).
- Never commit without explicit user confirmation (project commit discipline).
- Attribute name is `race_ethnicity`; golden field is `preferred_race_ethnicity`; normalization helper is `_norm_race`.
- Do **not** add `race_ethnicity` to `GOLDEN_FACT_FIELDS` (it would corrupt `_completeness_score`).

---

### Task 1: Ingestion — normalize + emit race fact (fundbox users + legacy + dump)

**Files:**
- Modify: `services/ingestion/src/connectors/fundbox/builders.py` (add `_norm_race` after `phone_region_hint`, ~line 49)
- Modify: `services/ingestion/src/connectors/fundbox/schema.py:45` (basic_profiles), `:154` (log_legacy_profiles)
- Modify: `services/ingestion/src/connectors/fundbox/users.py:~138` (attributes) and `:~152` (raw_payload profile dict)
- Modify: `services/ingestion/src/connectors/fundbox/legacy.py:62-68` (attributes)
- Modify: `services/ingestion/src/connectors/dumps/connectors.py` `_build_fundbox_user_row` (~line 585) — pass `race` into the synthetic row for both user + legacy dump paths
- Modify: `services/ingestion/src/golden_profile.py:59-65` (`_apply_survivorship` fields dict) and `services/ingestion/src/graph/queries/persons.py:179-192` (`UPDATE_GOLDEN_PROFILE` SET)
- Test: `services/ingestion/tests/test_fundbox_race.py` (create)

**Interfaces:**
- Produces: `_norm_race(value: object) -> str | None` in `builders.py`; the fundbox user/legacy envelopes now include an `attributes["race_ethnicity"]` entry and the raw_payload preserves `race`; ingestion `UPDATE_GOLDEN_PROFILE` accepts `$preferred_race_ethnicity`.

- [ ] **Step 1: Write the failing test**

```python
# services/ingestion/tests/test_fundbox_race.py
from __future__ import annotations

from src.connectors.fundbox.builders import _norm_race


def test_norm_race_title_cases_and_trims() -> None:
    assert _norm_race("MALAY") == "Malay"
    assert _norm_race("  chinese ") == "Chinese"
    assert _norm_race("BOYANESE") == "Boyanese"


def test_norm_race_none_for_empty_or_junk() -> None:
    assert _norm_race(None) is None
    assert _norm_race("") is None
    assert _norm_race("   ") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --package profile-unifier-ingestion pytest services/ingestion/tests/test_fundbox_race.py -v`
Expected: FAIL — `ImportError: cannot import name '_norm_race'`.

- [ ] **Step 3: Add `_norm_race` helper**

In `services/ingestion/src/connectors/fundbox/builders.py`, immediately after the `phone_region_hint` function (after line 49):

```python
def _norm_race(value: object) -> str | None:
    """Normalize a fundbox ``race`` value: trim, title-case, drop empties.

    Fundbox stores race as free text with inconsistent casing
    (``"MALAY"``/``"Malay"``). Title-casing collapses the duplicates while
    preserving sub-ethnicity detail (Javanese, Boyanese, ...). Returns
    ``None`` for empty/whitespace so ``build_envelope`` omits the attribute.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.title()
```

- [ ] **Step 4: Add `race` columns to schema**

`services/ingestion/src/connectors/fundbox/schema.py` — add after the `nationality` line in each table:
- `basic_profiles` (after line 45): `    Column("race", String(255)),`
- `log_legacy_profiles` (after line 154): `    Column("race", String(255)),`

- [ ] **Step 5: Emit race in `users.py`**

In `FundboxConnector._build_one` attributes dict (alongside `"nationality": row.nationality`) add:
```python
        "race_ethnicity": _norm_race(row.race),
```
And in the `raw_payload` `profile` sub-dict (alongside `"nationality": row.nationality`) add:
```python
                    "race": row.race,
```
Import `_norm_race` from `src.connectors.fundbox.builders` (add to the existing import block at `users.py:12-19`).

- [ ] **Step 6: Emit race in `legacy.py`**

In `FundboxLegacyConnector` attributes dict (alongside `"nationality": row.nationality`, ~line 66) add:
```python
        "race_ethnicity": _norm_race(row.race),
```
(legacy `raw_payload` uses `serialize_row(row)` which auto-includes `race` once the column exists — no raw_payload change needed.) Import `_norm_race` from builders.

- [ ] **Step 7: Pass `race` through the fundbox dump row assembler**

`services/ingestion/src/connectors/dumps/connectors.py` `_build_fundbox_user_row`: wherever the synthetic row is populated with `gender`/`nationality` aliases for the user and legacy paths, add the matching `race` alias so `_build_one`/legacy see `row.race`. (Read the function first; mirror the exact key used for `nationality`.)

- [ ] **Step 8: Ingestion golden write**

`services/ingestion/src/golden_profile.py` `_apply_survivorship` fields dict (lines 59-65) — add:
```python
        "preferred_race_ethnicity": _pick_best_fact(facts, "race_ethnicity"),
```
`services/ingestion/src/graph/queries/persons.py` `UPDATE_GOLDEN_PROFILE` SET clause (after the `preferred_nric` line, 186) — add:
```
    p.preferred_race_ethnicity = $preferred_race_ethnicity,
```

- [ ] **Step 9: Run tests + typecheck**

Run: `uv run --package profile-unifier-ingestion pytest services/ingestion/tests/test_fundbox_race.py -v` → PASS.
Run: `uv run --package profile-unifier-ingestion ruff check services/ingestion/src/connectors/fundbox services/ingestion/src/golden_profile.py` → clean.
Run: `uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src` → clean.

---

### Task 2: API — persist + resolve + project `preferred_race_ethnicity`

**Files:**
- Modify: `services/api/src/types.py:155` (Person) and `:243-249` (`GoldenFieldName`)
- Modify: `services/api/src/graph/golden_profile.py:40` (`GOLDEN_FIELD_SPEC`), `:223` (override-survival loop), `:311-331` (`recompute_golden_profile_tx`)
- Modify: `services/api/src/graph/queries/survivorship.py:49-61` (`UPDATE_GOLDEN_PROFILE` SET)
- Modify: `services/api/src/graph/queries/persons.py:24-28` (list projection), `GET_PERSON_BY_ID` (detail projection)
- Modify: `services/api/src/graph/queries/entities.py:100` (entity-person projection)
- Test: extend an existing golden-profile/survivorship test (e.g. `services/api/tests/test_survivorship*.py`) — assert `preferred_race_ethnicity` is resolved from a fundbox race fact and returned by `GET /v1/persons/{id}`.

**Interfaces:**
- Consumes: ingestion now writes `preferred_race_ethnicity` and `HAS_FACT{attribute_name:'race_ethnicity'}`.
- Produces: Person API responses include `preferred_race_ethnicity`; recompute resolves it; overrides survive recomputes.

- [ ] **Step 1: Person model + GoldenFieldName**

`services/api/src/types.py`:
- After `preferred_nric` (line 155): `    preferred_race_ethnicity: str | None = None`
- `GoldenFieldName` Literal (243-249): add `    "preferred_race_ethnicity",` member.

- [ ] **Step 2: Register in GOLDEN_FIELD_SPEC (NOT GOLDEN_FACT_FIELDS)**

`services/api/src/graph/golden_profile.py:40` `GOLDEN_FIELD_SPEC` — add entry:
```python
    "preferred_race_ethnicity": ("source_record_fact", "race_ethnicity"),
```
Do **not** edit `GOLDEN_FACT_FIELDS` (line 35) — it drives `_completeness_score`.

- [ ] **Step 3: Custom-override survival for race (no-fact edge case)**

`services/api/src/graph/golden_profile.py:223` — widen the loop so a custom-value race override survives even when no race fact exists:
```python
    for attr_name in (*GOLDEN_FACT_FIELDS, "race_ethnicity"):
```

- [ ] **Step 4: Resolve race in recompute + pass param**

`services/api/src/graph/golden_profile.py` `recompute_golden_profile_tx` — after the `dob = ...` line (312), add:
```python
    race_ethnicity = best_by_field.get("race_ethnicity", _empty_fact()).value
```
In the `tx.run(UPDATE_GOLDEN_PROFILE, ...)` call (320-331), add param:
```python
        race_ethnicity=race_ethnicity,
```

- [ ] **Step 5: UPDATE_GOLDEN_PROFILE SET clause**

`services/api/src/graph/queries/survivorship.py` — in `UPDATE_GOLDEN_PROFILE` SET (after the `preferred_nric` line, ~54), add:
```
    p.preferred_race_ethnicity = $race_ethnicity,
```

- [ ] **Step 6: Read projections**

`services/api/src/graph/queries/persons.py` list projection (line 26): add `.preferred_race_ethnicity` to the property list. Do the same in `GET_PERSON_BY_ID` detail projection (read it; it lists the same `.preferred_*` props). `services/api/src/graph/queries/entities.py:100` entity-person projection: same addition.

- [ ] **Step 7: Test + typecheck**

Run the extended survivorship/golden-profile test → PASS.
Run: `uv run --package profile-unifier-api mypy --strict services/api/src` → clean.
Run: `uv run --package profile-unifier-api ruff check services/api/src/graph services/api/src/types.py` → clean.

---

### Task 3: API — field-options + override support

**Files:**
- Modify: `services/api/src/graph/queries/survivorship.py:123` (GET_FIELD_OPTIONS fact allowlist), `:177-182` (current-value RETURN)
- Modify: `services/api/src/repositories/protocols/survivorship.py:32-45` (`FieldOptionsData`)
- Modify: `services/api/src/repositories/neo4j/survivorship.py:225-236` (mapper)
- Modify: `services/api/src/routes/survivorship.py:27-34` (`_FIELD_META`), `:69-80` (`_current_value`)

- [ ] **Step 1: Fact-options allowlist + current value**

`services/api/src/graph/queries/survivorship.py`:
- Line 123: `WHERE f.attribute_name IN ['full_name', 'dob', 'race_ethnicity']`
- RETURN (after `p.preferred_nric AS preferred_nric,` ~181): add `       p.preferred_race_ethnicity AS preferred_race_ethnicity,`

- [ ] **Step 2: FieldOptionsData + mapper**

`services/api/src/repositories/protocols/survivorship.py` `FieldOptionsData` — add field (after `preferred_nric`):
```python
    preferred_race_ethnicity: str | None
```
`services/api/src/repositories/neo4j/survivorship.py` `get_field_options` mapper (read the `preferred_nric=preferred_nric` extraction + `FieldOptionsData(...)` construction ~225-236): extract `preferred_race_ethnicity` from the result record and pass `preferred_race_ethnicity=preferred_race_ethnicity` into the dataclass.

- [ ] **Step 3: Route metadata + current-value dict**

`services/api/src/routes/survivorship.py`:
- `_FIELD_META` (27-34) — add (keep display order; place after NRIC, before Address):
```python
    ("preferred_race_ethnicity", "Race / Ethnicity", "source_record_fact"),
```
- `_current_value` dict (73-80) — add entry:
```python
        "preferred_race_ethnicity": data.preferred_race_ethnicity,
```

- [ ] **Step 4: Test + typecheck**

Run: `uv run --package profile-unifier-api pytest services/api/tests -k "field_options or survivorship" -v` → PASS.
Run: `uv run --package profile-unifier-api mypy --strict services/api/src` → clean.

---

### Task 4: Frontend — display race/ethnicity + wire override/merge

**Files:**
- Modify: `services/frontend2/src/lib/api-types.ts:~55` (`Person`)
- Modify: `services/frontend2/src/lib/api-types-person.ts:141` and `:161-167` (`GoldenFieldName` + request-body `field_name`)
- Modify: `services/frontend2/src/lib/golden-profile-choices.ts:35` (`FIELD_LABELS`) and `:43` (`FACT_FIELDS`)
- Modify: `services/frontend2/src/app/persons/[personId]/page.tsx:333` (`gpFields`), `:3975-3978` (merge field list), `:138` (`validateCustomOverrideValue`), `:161` (`overridePlaceholder`)
- Modify: `services/frontend2/src/app/public/persons/[token]/page.tsx:~78` (public field grid)

- [ ] **Step 1: Types**

`api-types.ts` `Person` — after `preferred_nric`: `  preferred_race_ethnicity: string | null;`
`api-types-person.ts`:
- `field_name` union (141) + `GoldenFieldName` (161-167): add `| "preferred_race_ethnicity"`.

- [ ] **Step 2: Choice client**

`golden-profile-choices.ts`:
- `FIELD_LABELS`: add `  preferred_race_ethnicity: "Race / Ethnicity",`
- `FACT_FIELDS`: add `  { fieldName: "preferred_race_ethnicity", attributeName: "race_ethnicity" },`

- [ ] **Step 3: Internal sidebar field**

`persons/[personId]/page.tsx` `gpFields` (333-339) — push:
```tsx
  { label: "Race / Ethnicity", value: person.preferred_race_ethnicity ?? "—" },
```

- [ ] **Step 4: Public page field**

`public/persons/[token]/page.tsx` field grid — add (gated on presence, mirroring the NRIC block):
```tsx
{person.preferred_race_ethnicity && (
  <div className={styles.fieldGroup}>
    <div className={styles.fieldLabel}>Race / Ethnicity</div>
    <div className={styles.fieldValue}>{person.preferred_race_ethnicity}</div>
  </div>
)}
```

- [ ] **Step 5: Merge modal + override helpers**

`persons/[personId]/page.tsx`:
- Merge golden-field list (3975-3978): add an entry `{ key: "preferred_race_ethnicity", label: "Race / Ethnicity", ... }` mirroring the existing NRIC entry's shape.
- `validateCustomOverrideValue` (138) and `overridePlaceholder` (161): add a `"preferred_race_ethnicity"` branch (free-text validation: non-empty; placeholder `"e.g. Chinese"`). If these helpers have a sane fallthrough, a minimal branch is enough — read them first.

- [ ] **Step 6: typecheck + lint**

Run: `cd services/frontend2 && npm run typecheck` → clean.
Run: `cd services/frontend2 && npm run lint` → zero net new warnings (stash-and-compare vs clean tree).

---

### Task 5: Docs

**Files:**
- Modify: `docs/profile-unifier-graph-schema.md` (document `race_ethnicity` `HAS_FACT` attribute)
- Modify: `docs/profile-unifier-policy-decisions.md` (note: race is display-only, not a match signal; sensitive-data handling)

- [ ] **Step 1: Graph schema** — add `race_ethnicity` to the `HAS_FACT` attribute-name list alongside `full_name`/`dob`, noting it is sourced from fundbox and is display-only.

- [ ] **Step 2: Policy decisions** — add a short decision: race/ethnicity is captured for display only; it is deliberately excluded from match/review scoring and from completeness; modeled as a `HAS_FACT` attribute, not an Identifier node, due to low cardinality and sensitivity.

---

## Self-Review (completed)

- **Spec coverage:** every spec section maps to a task (data source/normalization→T1; backend model/persistence/projections→T2; field-options/override→T3; frontend→T4; docs→T5). Review-similarity intentionally excluded (display-only) — noted in T2.
- **Type consistency:** attribute name `race_ethnicity`, golden field `preferred_race_ethnicity`, helper `_norm_race`, query param `$race_ethnicity` (API) / `$preferred_race_ethnicity` (ingestion) — used consistently and match existing param-naming conventions in each `UPDATE_GOLDEN_PROFILE`.
- **Completeness safeguard:** race is kept out of `GOLDEN_FACT_FIELDS`; only the override-survival loop is widened (T2 step 3).
- **Placeholder scan:** no TBD/TODO; T1 step 7 and T4 step 5 instruct reading the function first because the exact surrounding keys differ — this is read-then-mirror, not a placeholder.
