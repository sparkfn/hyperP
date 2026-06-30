# Race/Ethnicity on the Person Profile — Design

**Date:** 2026-06-30
**Status:** Approved (autonomous execution)
**Scope:** Add a race/ethnicity golden-profile field to Person, sourced from fundbox, surfaced in the UI, display-only.

## Goal

Add `race/ethnicity` to the Person golden profile so it is visible on the person detail page (and the public share page), sourced from fundbox ingestion. Race is treated as a **display-only golden attribute** — it does **not** influence the match/merge engine and is **not** a graph-traversable Identifier node.

## Key decisions

1. **Golden attribute, not an Identifier node.** Race is a `HAS_FACT` attribute (`attribute_name = "race_ethnicity"`) stored as a golden `:Person` property (`preferred_race_ethnicity`), mirroring how `dob` and `full_name` work. It is *not* modeled as an `Identifier` node of a new type, because race has very few distinct values (millions of people share each) — a shared Identifier node would cause fan-out explosion in candidate generation and would place sensitive data at a relationship hub. ("Weak identifier" in this codebase is a *derived runtime* concept — `_identifier_strength` returns `"strong"` only when NRIC is present, else `"weak"` — so any non-NRIC signal is already "weak"; no flag exists to set.)

2. **Display-only; no match influence.** The deterministic and heuristic engines are untouched. Race never enters candidate generation or scoring. This is deliberate: using race to merge people is ethically fraught. Race is a sensitive demographic, surfaced for human viewing only.

3. **Normalization = trim + title-case** at ingestion time. The fundbox `race` column is free-text with heavy case inconsistency (`MALAY`/`Malay`, `CHINESE`/`Chinese`, `BOYANESE`/`Boyanese`). Title-casing collapses the duplicates while preserving sub-ethnicity detail (Javanese, Boyanese, Filipino, Arab…). A controlled-vocabulary map to CMIO (Chinese/Malay/Indian/Others) is **deferred** — it would discard meaningful sub-ethnicity and needs a mapping spec we do not have yet.

## Data source

Only **fundbox** has race data, in two tables:
- `basic_profiles.race` (consumed by the `users` connector)
- `log_legacy_profiles.race` (consumed by the `legacy` connector)

eko/speedzone (PHPPOS), bitrix/whatsapp (chats), sgbankruptcy, sgrentalflats have no race column and are out of scope.

Observed value distribution (full fundbox dump, ~13k non-null rows): `MALAY`/`Malay`, `CHINESE`/`Chinese`, `INDIAN`/`Indian`, `JAVANESE`/`Javanese`, `BOYANESE`/`Boyanese`, `FILIPINO`, `INDONESIAN`, `ARAB`, … 91 raw distinct values (≈45 after title-casing), plus ~4600 NULLs.

## Backend (API) changes

- **`services/api/src/types.py`** — add `preferred_race_ethnicity: str | None = None` to `Person` (after `:155`). Widen the `GoldenFieldName` `Literal` (`:243-249`) to include `"preferred_race_ethnicity"`.
- **Survivorship — API recompute** (`services/api/src/graph/golden_profile.py`): add `"race_ethnicity"` to `GOLDEN_FACT_FIELDS` (`:35`) and an entry to `GOLDEN_FIELD_SPEC` (`:40`) → `("source_record_fact", "race_ethnicity")`. The recompute tx iterates this spec to resolve each field; adding the entry makes recompute derive race and lets pinned overrides survive recomputes.
- **Persistence — `UPDATE_GOLDEN_PROFILE`** (`services/api/src/graph/queries/survivorship.py:49`): the `SET` clause is static Cypher — add `p.preferred_race_ethnicity = $race_ethnicity,` and ensure the recompute tx passes a `$race_ethnicity` param (resolved via `GOLDEN_FIELD_SPEC`). This is the authoritative write used by survivorship/review recomputes.
- **Overridable field** (`services/api/src/routes/survivorship.py:27` `_FIELD_META`): add `("preferred_race_ethnicity", "Race / Ethnicity", "source_record_fact")`.
- **Field-options query** (`services/api/src/graph/queries/survivorship.py:123`): add `'race_ethnicity'` to the fact-options `attribute_name IN ['full_name', 'dob']` allowlist so candidate source values are returned for the override editor. If the field-options RETURN projects current golden values by name (`:177`), add `preferred_race_ethnicity` there too.
- **Read projections** — `services/api/src/graph/queries/persons.py` list (`:24`) and detail (`GET_PERSON_BY_ID`) projections, and `entities.py:100`, currently return `.preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob, .preferred_nric`. Add `.preferred_race_ethnicity` so the API actually exposes it on the Person response.
- **Review similarity — intentionally NOT touched.** `services/api/src/graph/queries/review.py:280-283` uses the golden-field list for a completeness/similarity score; race stays out of it (display-only, no match/review influence).

## Ingestion changes

No Identifier node, no `IdentifierBag.add`, no fanout-cap entry, no match scoring — pure attribute. Ingestion writes the initial golden value so race appears on the profile after a plain ingest (without waiting for a merge/override-triggered API recompute).

- **`services/ingestion/src/connectors/fundbox/builders.py`** — add a shared `_norm_race(value) -> str | None` helper (trim → title-case → `None` if empty). Both connectors use it (DRY).
- **`services/ingestion/src/connectors/fundbox/schema.py`** — add `Column("race", String(255))` to both `basic_profiles` (~`:46`) and `log_legacy_profiles` (~`:155`). The live DB reflection needs the columns declared; dumps already contain them.
- **`services/ingestion/src/connectors/fundbox/users.py`** (`_build_one`, ~`:138`): emit `"race_ethnicity": _norm_race(row.race)` alongside `gender`/`nationality`; add `race` to the `raw_payload` profile dict (~`:152`).
- **`services/ingestion/src/connectors/fundbox/legacy.py`** (~`:65`): same emit, since `log_legacy_profiles` has race too.
- **`services/ingestion/src/connectors/dumps/connectors.py`** (`_build_fundbox_user_row`, ~`:585`): pass `race` through the synthetic row for both user + legacy dump paths so `_build_one`/legacy `_build_one` receive it.
- **Ingestion golden write** (`services/ingestion/src/golden_profile.py:47` `_apply_survivorship`): the `fields` dict (lines 59-65) is a hardcoded set (`preferred_full_name/dob/phone/email/address_id`). Add `"preferred_race_ethnicity": _pick_best_fact(facts, "race_ethnicity")` so the initial golden value is chosen at ingest time. Then add the matching `p.preferred_race_ethnicity = $preferred_race_ethnicity,` line to the ingestion `UPDATE_GOLDEN_PROFILE` query (`services/ingestion/src/graph/queries/persons.py:179`), whose `SET` clause is also static Cypher.

A re-ingestion of fundbox is required to populate existing persons; new ingestion fills it going forward.

## Matching — no change

Deterministic engine (`nric`-only, `services/ingestion/src/matching/deterministic.py`) and heuristic engine (phone/email/dob-weighted, `heuristic.py`) are untouched. Race does not participate.

## Frontend changes (`services/frontend2`)

- **`src/lib/api-types.ts`** (~`:55`): add `preferred_race_ethnicity: string | null` to `Person`.
- **`src/lib/api-types-person.ts`** (`:141`, `:161`): widen `GoldenFieldName` and the request-body `field_name` union with `| "preferred_race_ethnicity"`.
- **Internal sidebar** (`src/app/persons/[personId]/page.tsx:333` `gpFields`): push `{ label: "Race / Ethnicity", value: person.preferred_race_ethnicity ?? "—" }`.
- **Public share page** (`src/app/public/persons/[token]/page.tsx:78`): add a `fieldGroup` block, rendered only when `preferred_race_ethnicity` is present.
- **`src/lib/golden-profile-choices.ts`** (`:35`, `:43`): add `preferred_race_ethnicity: "Race / Ethnicity"` to `FIELD_LABELS` and `{ fieldName: "preferred_race_ethnicity", attributeName: "race_ethnicity" }` to `FACT_FIELDS` — this makes the override editor and merge modal populate automatically.
- **Merge modal** field list (`page.tsx:3975`) and `validateCustomOverrideValue` / `overridePlaceholder` (`:138`, `:161`): add the field so merge and custom-value entry handle it.
- Source-record "pills" already render attributes generically, so the fundbox `race_ethnicity` fact appears there with no change.

## Tests

- **Ingestion**: fundbox dump row with `race='MALAY'` → emits `race_ethnicity='Malay'` fact; `NULL`/empty → omitted. Cover both `users` and `legacy` paths.
- **API**: golden recompute picks race; `field-options` returns `preferred_race_ethnicity`; override round-trips through recompute.
- **Frontend**: `npm run typecheck` and `npm run lint` (zero net new warnings, per the project's ESLint warning budget).

## Documentation

- `docs/profile-unifier-graph-schema.md` — document the new `race_ethnicity` `HAS_FACT` attribute.
- `docs/profile-unifier-policy-decisions.md` (or equivalent) — note that race is display-only and is **not** a match signal (sensitive-data handling).

## Out of scope

- CMIO controlled-vocabulary canonicalization (deferred).
- Race from non-fundbox sources (none have it).
- Any matching/scoring influence by race.
- Backfill automation beyond a standard re-ingestion.
