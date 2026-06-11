# Person↔Person Review Case Auditing — Design

**Date:** 2026-06-04
**Status:** Approved (design) — pending implementation plan
**Owner:** ingestion + API review/merge surfaces

## Problem

Today the review queue only audits **record→person** matches. When an incoming
source record reaches the merge/review band against an existing person, a
`ReviewCase` is created with the `MatchDecision` linking the **SourceRecord**
(`ABOUT_LEFT`) to the candidate **Person** (`ABOUT_RIGHT`).

Nothing audits whether two *existing* `Person` nodes are the same real-world
person. The graph schema, the review read query, and the resolve queries
(`GET_PERSONS_FOR_REVIEW_MERGE`, `CREATE_NO_MATCH_LOCK_FROM_REVIEW`) already
support a person↔person `MatchDecision` (`ABOUT_LEFT`/`ABOUT_RIGHT` may each be
a `Person` **or** a `SourceRecord`), but no producer ever creates one.

The strongest signal that two active persons might be duplicates is a **shared
identifier**: candidate generation already traverses shared `Identifier` nodes,
and the match engine deliberately does **not** auto-merge persons that share an
identifier (they may legitimately share a phone/household line — matching-spec
"Multi-Match Resolution", `pipeline.py:227`). That bridging event is exactly the
human-review trigger we are missing.

## Goal

Detect, during ingestion, when an identifier carried by an incoming record ends
up linking **2+ active persons**, and open **pairwise** person↔person review
cases for the bridged persons — with deduplication, and with merge/unmerge
side-effects that keep the queue consistent.

Non-goals (explicitly deferred):
- A standalone batch auditor that scans the whole graph for shared-identifier
  clusters independent of ingestion. The detection primitives are factored so a
  future batch job can reuse them, but the job itself is out of scope.
- LLM adjudication of person pairs.
- Cluster (N-way) review cases. We use pairwise cases (decision below).

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **Trigger**: a usable, within-fanout-cap identifier on the incoming record that is `IDENTIFIED_BY` 2+ **active** persons after the record is linked. | Graph-native; reuses candidate-generation traversal and the existing fanout cap. Identifier-level (not match-band-level) per product direction. |
| D2 | **Pairwise** cases using the existing `ABOUT_LEFT`/`ABOUT_RIGHT` person↔person model. | Reuses the existing review/merge/resolve machinery unchanged; no cluster modeling. |
| D3 | **Canonical pair ordering** `left.person_id < right.person_id` (same convention as `NO_MATCH_LOCK`). | Gives a stable pair key for dedup and lock checks; deterministic regardless of which side ingested first. |
| D4 | **Dedup gate (core)**: do not create a new case while an **open** (`open`/`assigned`/`deferred`) person↔person case already exists for the same ordered pair. | The duplicate check the user asked for. |
| D5 | **Dedup gate (correctness)**: skip pairs where either person is not `active` (already merged/suppressed), and skip self-pairs. | A merged person is not a reviewable distinct person. Implied by D1's "active" wording. |
| D6 | **Dedup gate (lock)**: skip pairs with an active `NO_MATCH_LOCK`. | The reviewer-workflow doc already says a `NO_MATCH_LOCK` must "suppress future candidate generation for the locked pair". Re-opening a rejected pair on the next bridging record would defeat the lock. Reuses `CHECK_NO_MATCH_LOCK`. |
| D7 | **Volume control**: reuse the existing per-identifier-type fanout cap (`fanout_cap_for` / `exceeds_fanout_cap`). An over-cap identifier produces no pairs. | Consistent with current matching policy; a household/business phone shared by many persons is treated as non-discriminating. |
| D8 | **Detection runs synchronously inside the ingest write transaction** (Approach A), in a new module `pipeline_person_pairs.py`. | Atomic with the link that caused the bridge; no window where the bridge exists but the case does not. Mirrors today's `create_review_case_if_needed`. |
| D9 | **Merge side-effects centralized** in one tx helper invoked from **both** merge call-sites (`review.py:_action_tx` and `merge.py:_manual_merge_tx`, right after `EXECUTE_MANUAL_MERGE`). | Both review-merge and direct admin merge must trigger the same side-effects. DRY. |
| D10 | On merge, **close** other open person↔person cases referencing the absorbed person; **redirect** open record↔person cases from absorbed→survivor. Both stamped with `merge_event_id`. | User requirement. Closing pair cases (vs redirecting) avoids producing self/duplicate pairs against the survivor. |
| D11 | **Unmerge reverts** the stamped side-effects, but only where a human has not acted on the case since the merge. | User requirement ("revert if case not yet closed"). Mirrors the existing `AFFECTED_RECORD` → `REVERT_MERGE` provenance pattern. |

## Architecture

### Component map

```
ingestion (services/ingestion)
  pipeline.py
    _execute_ingest ── after link + golden recompute ──► audit_person_pairs(...)   [NEW call]
  pipeline_person_pairs.py                                                          [NEW module]
    audit_person_pairs(tx, identifiers, fanout-capped) -> list[review_case_id]
  graph/queries/person_pairs.py                                                     [NEW queries]
    FIND_SHARED_IDENTIFIER_PERSON_SETS
    CHECK_OPEN_PERSON_PAIR_CASE
    CREATE_PERSON_PAIR_REVIEW_CASE
    (reuse) CHECK_IDENTIFIER_FANOUT, NO_MATCH_LOCK check

api (services/api)
  graph/queries/merge.py / review.py                                               [NEW queries]
    CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED
    REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED
    REVERT_PERSON_PAIR_CASE_CLOSURES
    REVERT_RECORD_PERSON_CASE_REDIRECTS
  repositories/neo4j/_merge_side_effects.py                                         [NEW shared helper]
    apply_merge_review_side_effects(tx, merge_event_id, absorbed_id, survivor_id)
    revert_merge_review_side_effects(tx, merge_event_id)
  repositories/neo4j/review.py   _action_tx        ── call apply_… after EXECUTE_MANUAL_MERGE
  repositories/neo4j/merge.py    _manual_merge_tx  ── call apply_… after EXECUTE_MANUAL_MERGE
  repositories/neo4j/merge.py    _unmerge_tx       ── call revert_… inside REVERT flow
```

### Data model

A person↔person review case reuses the existing nodes/edges with no schema
migration:

```
(ReviewCase)-[:FOR_DECISION]->(MatchDecision)
(MatchDecision)-[:ABOUT_LEFT  {entity_type:'person'}]->(Person {lower person_id})
(MatchDecision)-[:ABOUT_RIGHT {entity_type:'person'}]->(Person {higher person_id})
```

`MatchDecision` fields for a pair-audit decision:
- `engine_type = 'pair_audit'` (new marker value, distinguishing it from
  `deterministic`/`heuristic` record matches; the read query is unaffected).
- `engine_version`, `policy_version` set as for other decisions.
- `decision = 'review'`.
- `confidence`: omitted/0 — this is an audit prompt, not a scored match.
- `reasons`: human-readable, e.g. `["Shared phone +65… links 2 active persons"]`.
- `feature_snapshot`: typed JSON `{ "bridging_identifier_type": "...",
  "bridging_identifier_id": "...", "trigger_source_record_pk": "..." }` so the
  signal is structured, not parsed from reason text (matching-spec convention).

`ReviewCase` fields: `priority` and `sla_due_at` mirror record cases
(priority 100, SLA +7 days) for v1; can be tuned later.

### Provenance stamps for revert (D11)

- **Closed pair case** (D10 close): on the `ReviewCase`, set
  `closed_by_merge_event_id`, `queue_state='cancelled'`,
  `resolution='cancelled_superseded'`. Provenance lives in these dedicated
  properties plus the `MergeEvent` node — the system side-effect does **not**
  append to the `actions` list (that list is the human review-action log, and
  ingestion-created cases store it as a string). Unmerge reopens it **iff**
  `closed_by_merge_event_id == this event` AND the case is still in exactly that
  system-set state (no human action since).
- **Redirected record case** (D10 redirect): rewire the `ABOUT_*→Person` edge
  from absorbed to survivor; on the `ReviewCase` set
  `redirected_by_merge_event_id` and `redirected_from_person_id`. Unmerge
  rewires survivor→absorbed **iff** `redirected_by_merge_event_id == this event`
  AND the case is still open (`open`/`assigned`/`deferred`) — i.e. a reviewer
  did not resolve it against the survivor in the meantime.

## Data flow

### Ingestion — detection (D1, D7, D8)

`_execute_ingest`, after the record is linked and golden profiles recomputed
(including the multi-match `additional_linked_person_ids` loop), calls
`audit_person_pairs(tx, identifiers)`:

1. For each incoming identifier that is `is_usable` and **not** over its fanout
   cap, find the set of **active** persons `IDENTIFIED_BY` it
   (`FIND_SHARED_IDENTIFIER_PERSON_SETS`, which applies the cap in-query).
2. For each identifier-person-set with size ≥ 2, form ordered pairs
   `{(a,b) : a.person_id < b.person_id}` (D3).
3. For each pair, in order: skip if either side inactive / self-pair (D5);
   skip if active `NO_MATCH_LOCK` (D6); skip if an open person↔person case
   already exists (D4, `CHECK_OPEN_PERSON_PAIR_CASE`); else create the decision
   + case (`CREATE_PERSON_PAIR_REVIEW_CASE`), recording the bridging identifier
   in `feature_snapshot`.
4. Return created `review_case_id`s (surfaced in logs; `IngestResult` may carry
   a count — optional).

This is additive and side-effect free with respect to identity resolution: it
creates audit cases only and never merges or links persons.

### API — merge side-effects (D9, D10)

`apply_merge_review_side_effects(tx, merge_event_id, absorbed_id, survivor_id)`,
called immediately after `EXECUTE_MANUAL_MERGE` in both merge paths:

1. `CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED` — match open person↔person cases whose
   decision points (`ABOUT_LEFT`/`ABOUT_RIGHT`, `entity_type='person'`) at
   `absorbed_id`, excluding the case currently being resolved; set them
   cancelled + stamp `closed_by_merge_event_id`.
2. `REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED` — match open record↔person cases
   whose person side is `absorbed_id`; rewire that `ABOUT_*` edge to
   `survivor_id`; stamp `redirected_by_merge_event_id` + `redirected_from_person_id`.

Record↔person cases produced by ingestion always carry the person on
`ABOUT_RIGHT` (the source record is `ABOUT_LEFT`), so the redirect specializes
to the `ABOUT_RIGHT` person edge and is guarded by `ABOUT_LEFT` being a
`SourceRecord` (which excludes person↔person cases).

Edge case (deferred to v2): if redirecting collides with an existing open
record↔person case for the same source record against the survivor, the result
is two cases for that (record, survivor) — harmless duplicates a reviewer
resolves independently, not a graph inconsistency. v1 always redirects; explicit
collision-dedup is out of scope.

### API — unmerge revert (D11)

`revert_merge_review_side_effects(tx, merge_event_id)`, called inside
`_unmerge_tx` alongside `REVERT_MERGE`:

1. `REVERT_RECORD_PERSON_CASE_REDIRECTS` — for cases stamped with this event and
   still open, rewire the person edge survivor→absorbed and clear the stamps.
2. `REVERT_PERSON_PAIR_CASE_CLOSURES` — for cases stamped closed by this event
   and untouched since, restore `queue_state='open'`, clear `resolution` and
   `closed_by_merge_event_id`. (Reopens to `open` even if the case was
   `assigned` before the merge cancelled it — a minor fidelity trade-off; the
   reviewer can reassign.)

## Error handling & invariants

- All detection writes occur inside the single ingest write transaction; a
  failure rolls back the whole record ingest (same guarantee as today).
- Detection never creates a pair case that violates D3–D6; the create query is
  guarded so concurrent ingests cannot both insert a case for the same pair
  (re-check inside the write).
- Merge/unmerge side-effects run in the same transaction as the merge/unmerge
  graph mutation, so they are atomic with it.
- Self-pairs and inactive persons are filtered before any write.
- `engine_type='pair_audit'` is additive; existing readers ignore unknown engine
  types (they render the stored string).

## Testing strategy

Ingestion (pytest, `services/ingestion/tests`):
- Two active persons sharing one identifier via a new bridging record → exactly
  one open person↔person case, correctly ordered.
- Re-ingesting another bridging record for the same pair → no second case (D4).
- Pair under an active `NO_MATCH_LOCK` → no case (D6).
- Identifier over fanout cap → no cases (D7).
- 3 persons sharing an identifier → 3 pairwise cases (C(3,2)); 4 → 6 (D2),
  bounded by the cap.
- Inactive/merged person in the set → excluded (D5).

API (pytest, `services/api/tests`):
- Merge resolving pair A–B closes other open pair case A–C; leaves B–C / unrelated
  cases untouched (D10 close).
- Merge redirects an open record→A case to record→survivor (D10 redirect),
  with stamps set.
- Unmerge reopens the auto-closed A–C case and re-points the redirected record
  case back to A — **only** when untouched since merge; a case a human resolved
  post-merge is left as-is (D11).
- Both merge entry points (review action + admin merge) trigger identical
  side-effects (D9).

## Open considerations (non-blocking)

- Priority/SLA tuning for pair-audit cases vs record cases (kept identical for
  v1).
- Whether `IngestResult`/ingest-run summary should surface a
  `person_pair_cases_created` count (nice-to-have telemetry).
- A future batch auditor (Approach C) can call `audit_person_pairs` primitives
  over arbitrary person sets; the module boundary is drawn to allow it.
