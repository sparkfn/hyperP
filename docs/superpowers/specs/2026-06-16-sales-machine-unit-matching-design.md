# Sales ↔ Person Matching via Shared Machine Units (Review-Only)

## Goal

Give `pending_customer` sales records — those whose connector-supplied
`customer_link.identity_source_record_id` never resolves — a second,
review-only candidate-generation path based on shared `MachineUnit` evidence
(`BOUGHT_UNIT` / `OWNS_UNIT`), per the matching semantics already agreed in
[2026-05-14-machine-unit-graph-design.md](./2026-05-14-machine-unit-graph-design.md#matching-semantics):
machine-unit evidence may generate candidates and open review cases, but must
never drive an auto-merge/auto-link.

## Background

`pipeline_sales.py` links a sales `SourceRecord` to a `Person` only via a
deterministic FK (`customer_link.identity_source_record_id` →
`FOR_CUSTOMER_RECORD` → `LINKED_TO`). If that FK never resolves, the record
stays `link_status='pending_customer'` forever; `drain_pending_customer_sales`
retries the FK lookup every ingestion run but has no fallback. Sales records
otherwise bypass the matching engine (`evaluate_heuristic`) entirely.

Today, `_write_machine_unit_observations` already creates `MachineUnit` nodes
and `INVOLVES_UNIT` edges for **every** sales record (linked or pending), and
writes `BOUGHT_UNIT`/`OWNS_UNIT` only when `person_id` is known. So a pending
sale's `Order` may already share a `MachineUnit` with an existing, resolved
Person via that person's own purchase/ownership history — that overlap is the
new signal.

## Trigger & state machine

New step in `main.py`'s end-of-run sequence, immediately after
`drain_pending_customer_sales` (same place `materialize_knows_from_chat_*`
already runs — no new scheduling infrastructure):

```python
drained = drain_pending_customer_sales(client, exclusion_context=exclusion_context)
proposed = propose_machine_unit_matches_for_pending_sales(client)
```

`link_status` transitions on the sales `SourceRecord`:

| From | To | When |
|---|---|---|
| `pending_customer` | `pending_customer` | No machine-unit candidate found this run (retried next run) |
| `pending_customer` | `pending_review` | ≥1 candidate found → `MatchDecision` + `ReviewCase` created (reuses the existing `pending_review` value already used for provisional-review identity records) |
| `pending_review` | `linked` | Reviewer approves (`merge` action) |
| `pending_review` | `unresolved` | Reviewer rejects (`reject` / `manual_no_match`) — terminal in v1, no automatic re-proposal |

`pending_review` and `unresolved` sales records are excluded from
`FIND_PENDING_CUSTOMER_SALES` (already filters on `link_status:
'pending_customer'`) and from `propose_machine_unit_matches_for_pending_sales`
(add the same filter), so each pending sale gets at most one open proposal.

## Candidate generation

For each `SourceRecord {record_type: 'sales', link_status: 'pending_customer'}`,
find the `Order`/`MachineUnit`s it touches via the `INVOLVES_UNIT` edges that
`_write_machine_unit_observations` already wrote for it (keyed by
`source_record_pk`), then traverse to existing owners/buyers:

```cypher
MATCH (sr:SourceRecord {source_record_pk: $sales_source_record_pk, link_status: 'pending_customer'})
MATCH (o:Order)-[unit_rel:INVOLVES_UNIT {source_record_pk: $sales_source_record_pk}]->(u:MachineUnit)
MATCH (u)<-[rel:BOUGHT_UNIT|OWNS_UNIT]-(p:Person {status: 'active'})
RETURN p.person_id AS person_id, u.machine_unit_id AS machine_unit_id,
       type(rel) AS rel_type, rel.is_active AS is_active,
       u.conflict_flag AS conflict_flag, rel.last_confirmed_at AS last_confirmed_at
```

**Candidate selection** — to keep this at one `MatchDecision`/`ReviewCase` per
pending sale (avoid review-queue fanout when an order touches multiple units
or a unit has multiple owners), rank and take the single best:

1. `OWNS_UNIT` with `is_active = true` and `conflict_flag = false` beats
   `BOUGHT_UNIT`-only or conflict-flagged units.
2. Tie-break by most-recent `last_confirmed_at`.
3. Final tie-break by `person_id` (deterministic).

If no candidates exist, leave `link_status='pending_customer'` (cheap retry
next run — new `BOUGHT_UNIT`/`OWNS_UNIT` evidence can only appear via another
ingestion run anyway).

## Scoring & MatchDecision

New module `services/ingestion/src/matching/machine_unit_heuristic.py`
(parallel to `matching/heuristic.py`, but with its own entry point — sales
records carry no identifiers/attributes for `evaluate_heuristic` to score):

```python
MACHINE_UNIT_OWNS_CONFIDENCE = 0.65   # OWNS_UNIT, active, no conflict
MACHINE_UNIT_BOUGHT_CONFIDENCE = 0.60 # BOUGHT_UNIT-only, or conflict-flagged unit
```

Both constants sit inside the existing `[CONFIDENCE_REVIEW, CONFIDENCE_AUTO_MERGE)`
= `[0.60, 0.90)` band from `matching/heuristic.py`, and — being fixed
constants with no additive signals — can never reach `CONFIDENCE_AUTO_MERGE`.
This satisfies the prior spec's "must stay below auto-merge" constraint by
construction, with no shared scoring code needed.

`reasons` (using the prior spec's vocabulary):

- `"same_machine_unit_owner_claim (OWNS_UNIT, person <id>, unit <id>)"` → confidence `0.65`
- `"same_machine_unit_purchase (BOUGHT_UNIT, person <id>, unit <id>)"` → confidence `0.60`
- append `"; unit has conflicting ownership claims"` when `conflict_flag = true`
  (informational only — doesn't change the confidence value, since both
  values are already at/near the review floor)

`feature_snapshot` fields: `candidate_person_id`, `machine_unit_id`,
`rel_type`, `conflict_flag`, and `"signal_source": "machine_unit"` (lets
reviewers/monitoring distinguish these from identity-matching decisions
without a new `EngineType`).

`MatchResult`: `engine_type=EngineType.HEURISTIC`, `decision=MatchDecision.REVIEW`
always (this path only ever produces REVIEW or nothing), `matched_person_id`
= the selected candidate.

## MatchDecision + ReviewCase creation

Reuse existing infrastructure as-is — no new relationship types:

- `CREATE_MATCH_DECISION` (existing)
- `LINK_MATCH_DECISION_LEFT_SOURCE_RECORD` — left = the pending sales `SourceRecord`
- `LINK_MATCH_DECISION_RIGHT_PERSON` — right = the selected candidate `Person`
- `CREATE_REVIEW_CASE` (existing, `priority=100`, 7-day SLA — same defaults as
  `create_review_case_if_needed`)
- Set `sr.link_status = 'pending_review'`

This produces exactly the `left_kind='source_record'` / `right_kind='person'`
shape `GET_REVIEW_CASE` already returns generically.

## Review resolution side-effects

`services/api/src/repositories/neo4j/review.py`'s `_action_tx` currently
handles `merge` only when **both** `ABOUT_LEFT` and `ABOUT_RIGHT` resolve to
`Person` (`GET_PERSONS_FOR_REVIEW_MERGE`); otherwise it returns
`ActionResult(merge_not_applicable=True)`. Add a sales-specific branch:

**New query** `GET_SALES_LINK_FOR_REVIEW` —
`MATCH (md)-[:ABOUT_LEFT]->(sr:SourceRecord {record_type:'sales'})`,
`MATCH (md)-[:ABOUT_RIGHT]->(p:Person)` → returns `source_record_pk`,
`person_id`, and the `Order`/`MachineUnit`(s) via the same
`INVOLVES_UNIT {source_record_pk: ...}` traversal used for candidate
generation.

**On `merge`**: `_action_tx` currently calls `GET_PERSONS_FOR_REVIEW_MERGE`
(requires `ABOUT_LEFT`/`ABOUT_RIGHT` both `Person`) and returns
`ActionResult(merge_not_applicable=True)` when it finds no row. Add a fallback:
when `GET_PERSONS_FOR_REVIEW_MERGE` returns nothing, try
`GET_SALES_LINK_FOR_REVIEW`; if *that* matches, run the sales-link branch
instead of `merge_not_applicable`:

- `MERGE (p)-[:PURCHASED {source_system_key, source_order_id}]->(o)` — same
  shape as `LINK_PERSON_PURCHASED_ORDER`
- For each `MachineUnit` found via `INVOLVES_UNIT`: `MERGE (p)-[:BOUGHT_UNIT
  {source_system_key, source_order_id}]->(u)` — same shape as
  `LINK_PERSON_BOUGHT_UNIT` (purchase evidence only; never `OWNS_UNIT` — this
  path never asserts ownership, matching the "purchase ≠ ownership" rule from
  the prior spec)
- `SET sr.link_status = 'linked'`
- No golden-profile recompute — sales records carry no identity facts, so
  `recompute_golden_profile_tx` is skipped for this branch (it only fires
  today when `survivor_person_id` is set, which this branch leaves `None`)

**On `reject` / `manual_no_match`**: the generic `build_review_action_cypher`
state transition already runs (sets `queue_state`, `resolution`, `actions`);
additionally `SET sr.link_status = 'unresolved'`.
`CREATE_NO_MATCH_LOCK_FROM_REVIEW` is skipped for this branch (it requires
both sides `Person`, which won't be the case here) — a `NO_MATCH_LOCK` between
a SourceRecord and a Person doesn't fit the existing person-to-person lock
model, and isn't needed since `unresolved` is already terminal.

**On `defer` / `escalate`**: no sales-specific writes — `link_status` stays
`pending_review`.

## API / frontend surfacing

`GET_REVIEW_CASE` already returns `left_kind='source_record'` with
`left_entity { .source_record_pk, .source_record_id, .normalized_payload,
.observed_at }`. For sales records `normalized_payload` is always `{}`
(`pipeline_sales.py` never populates it), so the review UI would show an empty
left card today.

Add a small, sales-specific enrichment: when `left_entity.record_type ==
'sales'`, the API additionally resolves the `Order`/`MachineUnit`(s) via
`INVOLVES_UNIT {source_record_pk}` (the same traversal as above) and returns a
summary — order no., total amount, and per-unit `machine_product` /
`normalized_lta_tag` / `normalized_serial_number` / `conflict_flag` — as a new
optional field on the left entity (`sales_summary`), following the existing
v2-presentation-model pattern (`SourceRecordView`). The right-hand `Person`
card needs no changes (existing person summary already covers it). Frontend:
`services/frontend2`'s review case detail view renders `sales_summary` when
present, alongside the existing reason list (`same_machine_unit_owner_claim`
/ `same_machine_unit_purchase`).

## Testing

- `propose_machine_unit_matches_for_pending_sales`: candidate ranking
  (`OWNS_UNIT` > `BOUGHT_UNIT`, tie-breaks), no-candidate no-op, multi-unit
  orders, conflict-flagged units, idempotency (`pending_review` records are
  skipped on subsequent runs).
- Scoring constants: confidence values stay within `[0.60, 0.90)`; reasons and
  `feature_snapshot` shape.
- `MatchDecision`/`ReviewCase` creation: correct `ABOUT_LEFT`/`ABOUT_RIGHT`
  wiring, `link_status` → `pending_review`.
- Review action side-effects: `merge` creates `PURCHASED`/`BOUGHT_UNIT` and
  sets `link_status='linked'`, no golden-profile recompute triggered;
  `reject`/`manual_no_match` sets `link_status='unresolved'` without touching
  `NO_MATCH_LOCK`; `defer`/`escalate` leave `link_status` unchanged.
- API: `GET_REVIEW_CASE` returns `sales_summary` for sales-typed left entities
  and omits it otherwise.
- Run ingestion tests, API tests, lint, and strict type checks for changed
  Python files (per project standards).
