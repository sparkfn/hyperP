# Person-Pair Auto-Merge — Design

Date: 2026-07-10
Status: approved, pending implementation plan

## Background

The person-pair auditor (`services/ingestion/src/pipeline_person_pairs.py`,
`services/ingestion/src/matching/pair_score.py`) detects when a shared
identifier now bridges two *already-existing, distinct active* `Person`
nodes, and opens a `ReviewCase` (`engine_type='pair_audit'`) so a human can
adjudicate. It scores the pair with the same Layer-2 heuristic engine used
for source-record matching, but per the current matching spec
(`docs/profile-unifier-matching-spec.md`, "Person-Pair Auditing" section)
the decision is **always** `review`, regardless of the score — "a shared
identifier never auto-merges persons," because many bridges are
`relationship`-sourced contact rows whose subject is a different person.

This change introduces an auto-merge path for person-pairs at a high
confidence threshold, while leaving lower-confidence bridges on the
existing review-only path.

## Decision: threshold applies uniformly

The new threshold applies to all bridging identifier types (phone, email,
NRIC) — it does not special-case `relationship`-sourced bridges. The
heuristic score itself (name/DOB/address corroboration) is the safety
net: a bare shared phone with no other corroboration will not reach the
threshold on its own.

## Decision: merge executes locally in ingestion

Executing a real person-merge (rewiring `LINKED_TO`/`IDENTIFIED_BY`/
`LIVES_AT`/`HAS_FACT`, marking the absorbed person `merged`, creating
`MERGED_INTO`, path-compressing prior merge lineage, recomputing the
golden profile, and redirecting other open review cases) exists today
only in the API service (`services/api/src/repositories/neo4j/merge.py`,
`_merge_side_effects.py`, `graph/golden_profile.py`), reachable only via a
human-gated review-case action (`require_human_user`). The ingestion
worker (where pair bridges are detected, inside a Celery task) has no
path to that machinery.

Rather than add a new cross-service call (new internal OAuth2 client,
httpx dependency, auth plumbing) or extract a shared workspace package
(largest lift, touches the API's existing repository structure), this
merge executes **locally within ingestion**, reusing Cypher constants
already stubbed — but currently unused — in
`services/ingestion/src/graph/queries/merge.py`: `REWIRE_LINKED_TO`,
`REWIRE_IDENTIFIED_BY`, `REWIRE_LIVES_AT`, `REWIRE_HAS_FACT`,
`MARK_PERSON_MERGED`, `CREATE_MERGED_INTO`, `PATH_COMPRESS_MERGED_INTO`,
and the already-generic `CREATE_MERGE_EVENT_AUTO_MERGE` (takes
`from_person_id`/`to_person_id`/`reason`, `actor_type='system'`).

**Addendum (found during planning):** `services/ingestion/src/graph/queries/knows.py`
(`REWIRE_KNOWS_OUT`, `REWIRE_KNOWS_IN`) and `services/ingestion/src/graph/queries/sales.py`
(`REWIRE_PURCHASED`) also already exist, unused, with docstrings explicitly
noting they "mirror the rewire pattern for IDENTIFIED_BY / LIVES_AT" —
i.e. the same anticipated-but-never-wired-up person-merge. These must be
included alongside the `merge.py` rewires so KNOWS contacts and sales
orders move to the survivor instead of being orphaned on the absorbed
person.

This duplicates some logic from the API's merge repository (accepted
trade-off — see "Known limitation" below). The unmerge path
(`services/api/.../merge.py:unmerge`) is unaffected: it reverts by
`merge_event_id` generically and doesn't care whether the merge was
manual or an ingestion-driven auto-merge.

## Trigger & threshold

- New constant `PERSON_PAIR_AUTO_MERGE = 0.60` in
  `services/ingestion/src/matching/pair_score.py` (scoped to pair-audits,
  distinct from the general `CONFIDENCE_AUTO_MERGE = 0.40` used for
  source-record matching in `matching/heuristic.py`).
- In `pipeline_person_pairs.py:_create_pair_case_if_needed`, after
  `score_person_pair()` returns:
  - `score.confidence >= 0.60` → skip `ReviewCase` creation entirely;
    create a `MatchDecision` (`engine_type='pair_audit'`,
    `decision='merge'`) and execute the merge immediately. This mirrors
    how a normal source-record auto-merge never produces a `ReviewCase`
    — only a `MatchDecision` + `MergeEvent`.
  - `score.confidence < 0.60` → unchanged: open a `ReviewCase` as today.
- All existing guards before scoring are unchanged and still apply
  first: fanout cap, `NO_MATCH_LOCK` check, existing open
  person-pair-case check, both-persons-active check.

## Survivor selection

No human is choosing sides, so the rule must be deterministic and
explainable:

1. Higher `profile_completeness_score` (already computed on every
   `Person` by `golden_profile.py`) survives.
2. Tie → earlier `created_at` survives.
3. Tie → lower `person_id` survives (consistent with the existing
   canonical pair-ordering convention used elsewhere, e.g.
   `NO_MATCH_LOCK`, multi-match primary selection in
   `docs/profile-unifier-matching-spec.md`).

## Merge execution steps

New function, e.g. `merge_person_pair(tx, absorbed_id, survivor_id,
reason)` in a new ingestion module (`services/ingestion/src/pipeline_person_merge.py`
or similar — final placement decided during planning):

1. Create `MergeEvent` via `CREATE_MERGE_EVENT_AUTO_MERGE`
   (`actor_type='system'`, `actor_id='match_engine'`).
2. Rewire `LINKED_TO`, `IDENTIFIED_BY`, `LIVES_AT`, `HAS_FACT`, `KNOWS`
   (both directions), and `PURCHASED` from absorbed → survivor via the
   existing `REWIRE_*` queries (`merge.py`, `knows.py`, `sales.py`).
3. Mark absorbed person `status='merged'` via `MARK_PERSON_MERGED`.
4. Create `MERGED_INTO` via `CREATE_MERGED_INTO`.
5. Path-compress any prior `MERGED_INTO` chain pointing at the absorbed
   person via `PATH_COMPRESS_MERGED_INTO`.
6. Recompute the survivor's golden profile via the existing
   `compute_golden_profile(tx, survivor_id)`
   (`services/ingestion/src/golden_profile.py`) — already used elsewhere
   in the ingestion pipeline, no new logic needed.
7. Redirect side effects (new, ported from
   `services/api/src/graph/queries/merge.py`): close/redirect any other
   open review case referencing the absorbed person to the survivor —
   ingestion-local equivalents of `CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED`,
   `REDIRECT_PERSON_PAIR_CASES_ABSORBED_LEFT`/`_RIGHT`,
   `REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED`.

All of the above runs in the same Neo4j write transaction as the
bridge-detection/audit step, so it is atomic with the rest of the ingest
transaction.

## Known limitation (accepted, not solved in this pass)

Ingestion's `compute_golden_profile` does not know about
`survivorship_overrides` (a human-pinned golden field, API-only
concept). If the survivor already has an active override pinned and gets
auto-merged the same day, this recompute could silently overwrite that
pinned field. This is a narrow edge case (requires both an active
override *and* a same-day auto-merge landing on that exact person) and
is deliberately left unhandled in this pass rather than porting the
override-aware recompute logic now. Flagged here as a follow-up
candidate, not a blocker.

## Documentation follow-up

Once implemented, `docs/profile-unifier-matching-spec.md`'s "Person-Pair
Auditing" section must be updated to reflect that bridges scoring ≥ 0.60
now auto-merge rather than "a shared identifier never auto-merges
persons" unconditionally. Per project convention, this doc update ships
in the same commit as the implementation, not standalone.

## Testing

New tests in `services/ingestion/tests/` (exact file TBD during
planning) covering:

- Threshold boundary: confidence 0.59 → review case created as today
  (band/behavior unchanged); confidence 0.60 → merge executes, no
  `ReviewCase` created.
- Survivor selection: completeness-score, `created_at`, and `person_id`
  tie-break ordering, each in isolation.
- Rewiring correctness: `LINKED_TO`/`IDENTIFIED_BY`/`LIVES_AT`/`HAS_FACT`
  end up on the survivor and are gone from the absorbed person.
- Golden profile recomputed for the survivor after merge.
- Other open review cases referencing the absorbed person get redirected
  to the survivor.
- Existing pre-scoring guards (fanout cap, `NO_MATCH_LOCK`, existing open
  case, inactive person) still short-circuit before the new auto-merge
  branch is reached — unchanged behavior, regression-tested.
