# Spec 2 — Per-record-type merge criteria (bankruptcy gate + relationship trigger)

**Date:** 2026-06-08
**Status:** Approved (design)
**Prequel:** Spec 1 — record-type taxonomy split (`bankruptcy` / `rental_flat`). Done.
**Sequel:** Spec 3 — sales connector expansion + orphan-order phone+name fallback. Deferred (needs source-dump profiling; touches three POS connectors).

## Goal

Give two record types their own merge behaviour, layered onto the existing 4-layer match engine without changing behaviour for `identity`, `conversation`, or `sales`:

- **`bankruptcy`** — *tighten*: the exact-NRIC deterministic auto-merge now additionally requires a partial name match **when both sides carry a name**. NRIC-alone still merges when a name is absent on either side. A matching NRIC with a strongly conflicting name no longer auto-merges.
- **`relationship`** — *loosen*: a candidate pair that matches on phone **and** a partial name is promoted to auto-merge even if its additive heuristic score is below 0.90, unless a hard conflict blocks it. This generalizes the existing `conversation`-promotion mechanism.

"Partial name match" = Jaro-Winkler ≥ 0.50 (the existing `NAME_MISMATCH_THRESHOLD` floor — at/above it is a partial-or-better match; below it is a strong mismatch).

## Decisions (locked during brainstorming)

1. Semantics: a named signal **combo** triggers/permits a merge, guarded by hard-conflict blockers (the "trigger, but block by NRIC anti-match" model).
2. `bankruptcy`: NRIC + partial-name. Name required only when present on both sides (bankruptcy records carry a name; absent-name falls back to NRIC-alone).
3. `relationship`: phone + partial-name → auto-merge trigger.
4. "Partial" = JW ≥ 0.50.
5. Hard-conflict blockers (shared): NRIC anti-match and NO_MATCH_LOCK (already enforced by the deterministic layer for all types), plus DOB conflict, strong name mismatch (JW < 0.50), and high-fanout phone (>5 persons) — the existing conversation-promotion blocker set.
6. Sales deferred to Spec 3 (orphan orders carry no customer phone/name today).

## Background — current behaviour (verified in code)

- `matching/engine.py:MatchEngine._evaluate_one` runs `evaluate_deterministic` then `evaluate_heuristic` per candidate. Deterministic NO_MATCH (lock / conflicting NRIC) drops the candidate before heuristic.
- `matching/deterministic.py:evaluate_deterministic(tx, candidate_person_id, identifiers, record_type)` — for `record_type in SYSTEM_FAMILY`, an exact valid-NRIC match returns a hard MERGE (confidence 1.0) **with no name check**. It does **not** currently receive `attributes`.
- `matching/heuristic.py` — additive scoring → bands (≥0.90 MERGE, 0.60–0.89 REVIEW, <0.90… <0.60 NO_MATCH). `_promote_conversation_confidence` + `_can_promote_conversation` promote a sub-0.90 `conversation` pair to `CONVERSATION_PROMOTED_CONFIDENCE` (0.91) when corroborated and not blocked. `HeuristicSignals` / the feature snapshot already expose `phone_exact_match`, `name_similarity`, `dob_conflict`, `name_mismatch`, `phone_high_fanout`.
- `_score_name` already computes the best incoming↔candidate Jaro-Winkler similarity; `jaro_winkler_similarity` is in `matching/similarity.py`; candidate names come from `snapshot.fetch_candidate_snapshot(tx, pid).names()`.
- `tests/test_match_engine_system_family.py` pins that `identity`/`bankruptcy`/`relationship` produce identical `MatchResult`s. Spec 2 deliberately breaks this for `relationship` (heuristic promotion) and for `bankruptcy` only in the NRIC-with-conflicting-name case; it is updated here.

## Changes

### 1. Shared name-match helper (DRY) — `matching/similarity.py`

Extract the "best similarity between incoming names and a list of candidate names" logic (currently inline in `heuristic._score_name`) into a reusable function so the deterministic gate and the heuristic scorer share one implementation:

```python
NAME_INCOMING_FIELDS: tuple[str, ...] = ("full_name", "preferred_name", "legal_name")
NAME_PARTIAL_THRESHOLD: float = 0.50  # JW >= this is a partial-or-better match

def incoming_names(attributes: list[NormalizedAttribute]) -> list[str]: ...
def best_name_similarity(incoming: list[str], candidate_names: list[str]) -> float: ...
def is_partial_name_match(attributes, candidate_names) -> bool | None:
    """True/False when both sides have names; None when either side has none."""
```

`heuristic._score_name` is refactored to call `incoming_names` + `best_name_similarity` (no behaviour change). `heuristic.NAME_MISMATCH_THRESHOLD` is set to / re-exported from `NAME_PARTIAL_THRESHOLD` so the "mismatch < 0.50" and "partial ≥ 0.50" boundaries stay a single source of truth.

### 2. `bankruptcy` deterministic name gate — `matching/deterministic.py` + `engine.py`

- Add an `attributes: list[NormalizedAttribute]` parameter to `evaluate_deterministic` (and thread it from `MatchEngine._evaluate_one`, which already has `attributes`).
- In the government-ID MERGE branch (`_check_government_id`), when the candidate has the matching valid NRIC **and** `record_type == RecordType.BANKRUPTCY`:
  - Compute `is_partial_name_match(attributes, fetch_candidate_snapshot(tx, candidate_person_id).names())`.
  - `True` (both sides named, JW ≥ 0.50) → return the hard MERGE (as today).
  - `None` (a name missing on either side) → return the hard MERGE (NRIC-alone fallback, per decision 2).
  - `False` (both named, JW < 0.50) → **do not** return the NRIC MERGE; return `None` so the pair falls through to heuristic. Heuristic will score the name mismatch (penalty) → NO_MATCH → a separate Person; the shared NRIC then opens a person-pair review case via the existing `audit_person_pairs`. Add a reason string documenting the gate.
- `identity` (and any other SYSTEM_FAMILY member) keep NRIC-alone merge. Conflicting-NRIC NO_MATCH is unchanged for all types.
- The snapshot fetch only happens on the rare bankruptcy-NRIC-match path; when it merges, heuristic is skipped anyway.

### 3. `relationship` heuristic trigger + generalized promotion — `matching/heuristic.py`

- Extract `_has_hard_conflict(features) -> bool` = `dob_conflict or name_mismatch or phone_high_fanout`. Use it inside the existing conversation path (`_can_promote_conversation` keeps its corroboration logic but defers the conflict check to this helper — no behaviour change).
- Replace `_promote_conversation_confidence` with `_promote_by_record_type(record_type, confidence, reasons, features)` that:
  - returns `confidence` unchanged when already ≥ `CONFIDENCE_AUTO_MERGE`;
  - `conversation` → existing corroboration gate;
  - `relationship` → promote when `phone_exact_match` **and** `name_similarity ≥ NAME_PARTIAL_THRESHOLD` **and** not `_has_hard_conflict`;
  - (sales slot reserved for Spec 3);
  - on promotion, set `confidence = PROMOTED_CONFIDENCE` (rename `CONVERSATION_PROMOTED_CONFIDENCE` → shared `PROMOTED_CONFIDENCE = 0.91`, keep the old name as an alias if referenced elsewhere), set a `promotion: <record_type>` feature flag, and append a typed reason.
- NRIC anti-match for `relationship` is already handled: a conflicting valid NRIC produces a deterministic NO_MATCH that drops the candidate before heuristic runs.

### 4. Tests — `tests/test_match_engine_system_family.py` + new

- `test_match_engine_system_family.py`:
  - Update the `evaluate_deterministic(...)` calls to pass `attributes=[]`. With no incoming name, `bankruptcy` takes the NRIC-alone fallback, so the deterministic-equivalence assertion across `(identity, bankruptcy, relationship)` still holds and stays.
  - Split the heuristic-equivalence assertion: `identity` and `bankruptcy` still produce identical results; `relationship` now promotes (phone + name "Ada Lovelace" ≥ 0.50) to MERGE. Assert `identity == bankruptcy` and that `relationship` is MERGE with the promotion reason. Update the module docstring.
- New `tests/test_bankruptcy_name_gate.py`:
  - NRIC match + incoming name JW ≥ 0.50 → deterministic MERGE.
  - NRIC match + no incoming name → deterministic MERGE (fallback).
  - NRIC match + incoming name JW < 0.50 (both sides named) → `evaluate_deterministic` returns `None` (gate blocks the hard merge).
  - Conflicting NRIC still → NO_MATCH for bankruptcy.
- New `tests/test_relationship_promotion.py`:
  - phone match + name ≥ 0.50, sub-0.90 additive → promoted MERGE with reason + feature flag.
  - blocked by `dob_conflict`, by high-fanout phone, and by name < 0.50 (each stays ≤ REVIEW).
  - `identity` with the same inputs is **not** promoted (stays at its additive band).

## Verification

- `uv run --package profile-unifier-ingestion ruff check` + `mypy --strict services/ingestion/src` (clean).
- `uv run pytest services/ingestion/tests` green.
- Spot-check: a `bankruptcy` envelope sharing an NRIC with a same-name person auto-merges; with a clearly different name it does not (separate person + person-pair review). A `relationship` envelope (emergency contact) with a matching phone + partial name auto-merges to the contact person.

## Out of scope (→ Spec 3)

- `sales`: expand the phppos/fundbox sales connectors to join the POS customer table and emit customer phone + name, then add a phone+name fallback in `pipeline_sales.py:_resolve_and_link_customer` for orders that cannot resolve a Person via the customer FK, registering `sales` in `_promote_by_record_type`. Requires profiling the limited-100 sales/customer dumps first.
