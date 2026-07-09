# SG Gov Entity Removal, Match-Only SG Ingestion, and Threshold Reduction

## Scope

Two independent changes:

1. Remove the `SG Gov` `Entity` node entirely. The two SG government source systems
   (`sgbankruptcy`, `sgrentalflats`) no longer belong to any `Entity`. `sgbankruptcy`
   ingestion becomes match-only: it can only link to existing persons (never create
   one) and drops records whose NRIC matches nobody. The bankruptcy-specific
   name-conflict gate on the deterministic NRIC merge is removed, so a valid NRIC
   match auto-merges regardless of name similarity.
2. Reduce the global person-match confidence thresholds: auto-merge from 0.90 to
   0.40, review from 0.60 to 0.20. This applies to all sources (fundbox, eko,
   speedzone, onediver, sgbankruptcy) — it is a global default, not SG-specific.
   The separate vehicle-matching threshold (`VEHICLE_MATCH_AUTO = 0.90` in
   `matching/vehicle_heuristic.py`) is a distinct pipeline (vehicle-to-order
   linking, not person identity) and is explicitly out of scope.

There is no existing graph data to migrate — this is a code-only change plus the
next ingestion bootstrap run.

## Background

`sggov` is currently seeded as an `Entity` (`entity_type: government`) in
`services/ingestion/src/graph/bootstrap.py`, with `sgbankruptcy` and
`sgrentalflats` bound to it via `entity_key` and the `OPERATED_BY` edge. SG Gov
is not a company/customer entity like Fundbox/SpeedZone/Eko/OneDiver, so it
should not appear in the entities list or accrue entity-scoped persons.

`sgrentalflats` already routes through `ingest_address_record` (`main.py` via
`_ADDRESS_ONLY_SOURCES`), carries no identifiers, and never touches the match
engine or creates a `Person` — it already satisfies "match-only, never create a
person" trivially and needs no pipeline changes, only the entity-less bootstrap
change.

`sgbankruptcy` carries one `nric` identifier per record and runs through the
full `IngestPipeline.ingest` flow, which today **always** creates a new `Person`
when no match is found (`_resolve_person` in `pipeline.py`). It also has a
bankruptcy-specific guard in the deterministic layer
(`matching/deterministic.py:_check_government_id`) that suppresses the exact-NRIC
hard-merge when both sides have a name and Jaro-Winkler similarity is below 0.50
— this is the "blocking condition" being removed for SG.

The auto-merge/review confidence bands (`CONFIDENCE_AUTO_MERGE = 0.90`,
`CONFIDENCE_REVIEW = 0.60` in `matching/heuristic.py`) are hardcoded constants
used by the heuristic layer (Layer 2) for every source. They are being lowered
globally per explicit instruction, accepting the precision trade-off.

## 1. SG Gov entity removal

### Bootstrap seed data (`services/ingestion/src/graph/bootstrap.py`)

- Remove the `sggov` entry from `_ENTITIES`.
- Change the `sgbankruptcy` and `sgrentalflats` entries in `_SOURCE_SYSTEMS` to
  omit `entity_key` (or set it to `None` if the `_SourceSystemSeed` TypedDict
  requires the key to be present — the field becomes `entity_key: str | None`).
- `SOURCE_KEY_TO_ENTITY` (derived dict comprehension) naturally drops the two SG
  keys once their `entity_key` is `None` — the comprehension should skip entries
  whose `entity_key` is `None` rather than mapping them to the string `"None"`.
- Split `bootstrap_entities_and_sources`'s write loop: source systems with a
  non-`None` `entity_key` use the existing `UPSERT_SOURCE_SYSTEM_WITH_ENTITY`;
  source systems with `entity_key is None` use a new `UPSERT_SOURCE_SYSTEM` query
  (no entity match, no `OPERATED_BY` edge).

### New query (`services/ingestion/src/graph/queries/entities.py`)

Add `UPSERT_SOURCE_SYSTEM`: identical to `UPSERT_SOURCE_SYSTEM_WITH_ENTITY` minus
the `MATCH (e:Entity {entity_key: $entity_key})` line and the
`MERGE (ss)-[:OPERATED_BY]->(e)` line. Export it from `queries/__init__.py`.

### Read-side (API) — no changes required

`services/api/src/graph/queries/entities.py` (`LIST_ENTITIES`,
`LIST_FILTER_SOURCE_SYSTEMS`) and `services/api/src/graph/queries/admin.py`
already use `MATCH (ss:SourceSystem) OPTIONAL MATCH (ss)-[:OPERATED_BY]->(e:Entity)`
— an entity-less `SourceSystem` renders with no entity and `SG Gov` simply no
longer appears in the entities list once its `Entity` node is gone. Confirmed no
other API code path assumes every source has an `entity_key`.

### No migration

No existing `sggov` `Entity` node or SG-sourced `Person`/`SourceRecord` data
exists in the current environment, so no cleanup migration is needed. If a
future environment already has the `sggov` `Entity` node from a prior deploy,
that is out of scope here — the bootstrap simply stops re-asserting it; a
stale node would need a manual one-off cleanup at that time.

## 2. SG bankruptcy ingestion becomes match-only

### Source family flag (`services/ingestion/src/main.py`)

Add `_SG_MATCH_ONLY_SOURCES = frozenset({"sgbankruptcy"})` alongside the
existing `_ADDRESS_ONLY_SOURCES`, with a parallel `_is_sg_match_only_source`
helper.

### Pipeline change (`services/ingestion/src/pipeline.py`)

`_execute_ingest` currently runs, in order: `upsert_nodes` (writes Identifier /
Address nodes) → `find_candidates` → `match_engine.evaluate` → `_resolve_person`
(creates a `Person` when unmatched). For match-only sources this must not write
anything when no match is found, so the order changes to a read-only probe
before any writes:

1. `candidates = find_candidates(tx, identifiers, addresses)` — this only reads
   existing `Identifier`/`Person` edges; it does not require the incoming
   identifier's `Identifier` node to already exist as a graph node (candidate
   generation matches on `identifier_type` + `normalized_value` against
   existing nodes).
2. `match_result = self._match_engine.evaluate(tx, candidates, ...)` — read-only.
3. If the envelope's source is in `_SG_MATCH_ONLY_SOURCES` and the match result
   is not a usable match — defined as: `decision == NO_MATCH`, **or**
   `decision == REVIEW` with no `matched_person_id`/no candidates — return a new
   dropped disposition: `IngestResult(source_record_id=..., dropped=True,
   skipped_duplicate=False, person_id=None, ...)`. Nothing is written: no
   `upsert_nodes`, no `SourceRecord`, no `Person`, no `BankruptcyCase`, no
   `MatchDecision`.
4. Otherwise (MERGE against an existing person, or REVIEW with a candidate
   person), proceed with the existing flow unchanged: `upsert_nodes` →
   `_resolve_person` → persist source record → link to graph → materialize
   bankruptcy case → recompute golden profile → multi-match fan-out →
   `audit_person_pairs`.

For match-only sources, `_resolve_person` must never reach a `create_person`
call. Since the gate in step 3 gets there first, the two `create_person`
branches in `_resolve_person` (REVIEW-no-candidate, hard NO_MATCH-no-person) are
unreachable for `sgbankruptcy` — no change to `_resolve_person`'s signature is
required as long as the gate in `_execute_ingest` fully covers both cases before
`_resolve_person` is called.

### `IngestResult` / run accounting (`services/ingestion/src/models.py`, `main.py`)

Add a `dropped: bool = False` field to `IngestResult` (or reuse an existing
disposition field if one fits better — confirm during planning) so a dropped SG
record is distinguishable from a normal ingest, a duplicate-skip, and a
rejection. The ingest run's summary counters must count a dropped record as
neither a failure nor a normal ingested record, so the run completes cleanly.
The exact counter field(s) to update are an implementation-plan detail (likely
a new `dropped_count` alongside `record_count`/`rejected_count`).

### `sgrentalflats` — unchanged

Continues to route through `ingest_address_record`, which never creates a
`Person` and never invokes the match engine. It already fulfills "SG sources
should only match to existing persons" / "never create new persons" (there is
no person concept to match or create) and "unmatched records ignored" is
vacuous since it only ever links a source record to an `Address` node.

## 3. Remove the bankruptcy NRIC name-conflict gate

### Change (`services/ingestion/src/matching/deterministic.py`)

In `_check_government_id`, delete the `record_type == RecordType.BANKRUPTCY`
branch (currently lines ~188-197) that calls `is_partial_name_match(...)` and
returns `None` (suppressing the merge) on a name conflict. After removal, an
exact valid-NRIC match against a candidate with a valid-quality NRIC edge
always returns a deterministic `MatchResult(decision=MERGE, confidence=1.0,
reasons=["Exact government ID hash match"])` — regardless of name.

### What stays unchanged

- The conflicting-NRIC hard `NO_MATCH` branch (different valid NRIC value on
  the candidate) — this is a correctness guard against merging two people who
  provably hold different government IDs, not a "blocking condition" on a
  positive match.
- `NO_MATCH_LOCK` checks (`_check_no_match_lock`) — explicit human-recorded
  locks between two persons still block a match.
- Fanout caps (`exceeds_fanout_cap` in `pipeline_writes.py`) — unchanged; NRIC
  is low-cardinality so this rarely fires in practice.
- The `quality_flag = 'valid'` requirement on both the incoming and candidate
  NRIC edge — this is the "minimal requirement on the NRIC" itself, not a
  blocking condition, and stays as the floor for what counts as a usable NRIC
  match.

### Consequence

Since `record_type == BANKRUPTCY` records are produced exclusively by the
`sgbankruptcy` connector, this change is scoped to SG bankruptcy ingestion in
practice without needing an explicit source-key check in the deterministic
layer.

## 4. Global confidence threshold reduction

### Change (`services/ingestion/src/matching/heuristic.py`)

```python
CONFIDENCE_AUTO_MERGE = 0.40
CONFIDENCE_REVIEW = 0.20
```

This is a global change affecting the Layer-2 heuristic `_band()` decision for
every source. `PROMOTED_CONFIDENCE = 0.91` (record-type promotion for
conversation/relationship pairs) stays unchanged and remains comfortably above
the new auto-merge floor.

### Explicitly out of scope

`VEHICLE_MATCH_AUTO = 0.90` and its associated review threshold in
`services/ingestion/src/matching/vehicle_heuristic.py` are a separate
vehicle-to-order matching pipeline, not person-identity matching, and are not
touched by this change.

### Follow-on updates required

- Docstrings/comments stating the old bands: `heuristic.py` module docstring
  (≥0.90 MERGE / 0.60–0.89 REVIEW / <0.60 NO_MATCH), and the CLAUDE.md
  "Confidence bands" line under Key Design Decisions.
- Existing tests that assert specific confidence values or MERGE/REVIEW/NO_MATCH
  outcomes near the old boundaries (0.60, 0.90) must be re-examined against the
  new boundaries (0.20, 0.40) — enumerated during planning, not resolved here.

## Out of scope

- No changes to the vehicle-matching pipeline or its thresholds.
- No changes to `sgrentalflats` ingestion behavior (already match-only /
  person-less by construction).
- No graph data migration (no existing SG-sourced persons/entities in the
  current environment).
- No changes to LLM adjudication (still a stubbed pass-through).
- No changes to `NO_MATCH_LOCK` or fanout-cap behavior.
