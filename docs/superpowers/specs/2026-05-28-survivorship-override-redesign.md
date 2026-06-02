# Survivorship Override Dialog Redesign

**Date:** 2026-05-28
**Status:** Approved

## Problem

The existing `SurvivorshipOverrideDialog` has five bugs and UX gaps identified during a verify pass:

1. `preferred_address` in the attribute dropdown always returns HTTP 422 — address facts are on `LIVES_AT`, not `HAS_FACT`, so `GET_FACT_VALUE` never matches.
2. No audit `MergeEvent` is written when an override is applied, violating the spec's audit requirement.
3. Dialog state (attribute, source record PK, reason, error) is never reset on close; stale values persist on reopen.
4. Source record PK is a raw free-text UUID field — unusable without a database console.
5. Attribute dropdown and toast use raw snake_case API names instead of human-friendly labels.

## Scope

- `services/frontend/src/lib/golden-profile-choices.ts` — additive field only
- `services/frontend/src/components/SurvivorshipOverrideDialog.tsx` — full redesign
- `services/api/src/graph/queries/survivorship.py` — new Cypher constant
- `services/api/src/repositories/neo4j/survivorship.py` — call new audit query

No changes to: BFF route, API route, `SurvivorshipOverrideRequest`, `PersonDetailTabs.tsx`, `GoldenProfilePicker`, `ManualMergeDialog`.

---

## Design

### 1. `golden-profile-choices.ts` — add `observedAt`

Add one field to `GoldenProfileChoice`:

```ts
export interface GoldenProfileChoice {
  // ... existing fields ...
  observedAt: string;   // ISO string from PersonSourceRecord.observed_at; "" for identifier/address choices
}
```

Thread it through `makeChoice` (new `observedAt` argument) and populate it from `record.observed_at` in `sourceRecordFactChoices`. Set `""` in `sourceRecordAddressChoices` and `identifierChoices` — those paths are unused by the override dialog but must satisfy the interface.

`GoldenProfilePicker` and `ManualMergeDialog` ignore the new field.

---

### 2. `queries/survivorship.py` — `CREATE_OVERRIDE_AUDIT`

New Cypher constant, added after `UPDATE_GOLDEN_FIELD`:

```cypher
MATCH (p:Person {person_id: $person_id})
CREATE (me:MergeEvent {
  merge_event_id: randomUUID(),
  event_type: 'survivorship_override',
  actor_type: 'admin',
  actor_id: $actor_id,
  reason: $reason,
  metadata: '{}',
  created_at: datetime()
})
CREATE (me)-[:SURVIVOR]->(p)
```

Export via `__init__.py`.

---

### 3. `repositories/neo4j/survivorship.py` — call audit in `_override_tx`

After the existing `UPDATE_GOLDEN_FIELD` run, add:

```python
await tx.run(CREATE_OVERRIDE_AUDIT, person_id=person_id, actor_id=actor_id, reason=reason)
```

`person_id`, `actor_id`, and `reason` are already parameters of `_override_tx`.

---

### 4. `SurvivorshipOverrideDialog.tsx` — full redesign

#### Data loading

On `open → true`, call `loadGoldenProfileEvidence(personId)` (imported from `ManualMergeDialog` — extract to a shared location or re-declare locally), then `buildGoldenProfileChoices(personId, [evidence])` filtered to `sourceKind === "source_record_fact"`.

Sort filtered choices by `observedAt` descending (most recent first). Store as `choices: GoldenProfileChoice[]`.

Show `CircularProgress` while loading; `Alert` on error. If choices are empty after load, show an informational message ("No source record facts found for this person.").

#### State

`useEffect` keyed on `open`:
- When `open` becomes `true`: kick off data load, reset `selectedAttributeName` to `""`, `selectedChoiceKey` to `""`, `reason` to `""`, `error` to `null`.
- When `open` becomes `false`: cancel in-flight fetch (cancelled flag pattern from `ManualMergeDialog`), clear `choices`.

#### Body — three controls

**Attribute dropdown** (`TextField select`):
- Options: `preferred_full_name`, `preferred_dob`, `preferred_phone`, `preferred_email` only.
- Display text: `goldenProfileFieldLabel(fieldName)` → "Full name", "Date of birth", "Phone", "Email".
- Disabled while loading.
- On change: reset `selectedChoiceKey` to `""`.

**Value dropdown** (`TextField select`):
- Filtered to `choices` where `choice.fieldName === selectedAttributeName`.
- Disabled if no attribute selected or loading.
- Each `MenuItem` shows two lines (same pattern as `GoldenProfilePicker`):
  - Line 1: `choice.value` (`Typography variant="body2"`)
  - Line 2: `choice.sourceLabel` (`Typography variant="caption" color="text.secondary"`)
- Value bound to `choice.key`; on submit, look up `choices.find(c => c.key === selectedChoiceKey)` to get `sourceRecordPk`.

**Reason** (`TextField multiline minRows={2}`): unchanged.

#### Submit

```ts
const chosen = choices.find(c => c.key === selectedChoiceKey);
const body: SurvivorshipOverrideRequestBody = {
  attribute_name: selectedAttributeName,        // e.g. "preferred_phone"
  selected_source_record_pk: chosen!.sourceRecordPk!,
  reason: reason.trim(),
};
```

Toast: `Override saved for ${goldenProfileFieldLabel(result.attribute_name as GoldenProfileFieldName)}`.

`canSubmit`: `selectedChoiceKey !== "" && reason.trim().length > 0 && !submitting && !loading`.

#### `loadGoldenProfileEvidence` placement

Extract `loadGoldenProfileEvidence` from `ManualMergeDialog.tsx` into `golden-profile-choices.ts` (it belongs with the evidence/choice infrastructure). Update the import in `ManualMergeDialog.tsx`.

---

## Invariants

- `preferred_address` and `preferred_nric` never appear in the attribute dropdown; the backend's `fact_not_found` path for address is now unreachable from the UI.
- `selected_source_record_pk` is always a UUID from a real `GoldenProfileChoice` — never user-typed.
- Every successful override call produces a `MergeEvent` visible in `GET /v1/persons/{id}/audit`.
- Dialog state is always clean on open.
