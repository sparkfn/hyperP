# SG Gov Removal, Match-Only SG Ingestion, and Threshold Reduction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the `SG Gov` `Entity` and detach `sgbankruptcy`/`sgrentalflats` from
any entity; make `sgbankruptcy` ingestion match-only (link to existing persons only,
never create one, drop unmatched records); remove the bankruptcy NRIC name-conflict
gate; and lower the global person-match confidence thresholds to 0.40 (auto-merge)
/ 0.20 (review).

**Architecture:** Four independent, sequential changes to the ingestion service
(`services/ingestion/src`): (1) bootstrap seed data + a new entity-less
`SourceSystem` upsert query, (2) a read-before-write reordering in
`IngestPipeline._execute_ingest` gated by a new SG-source-family check, (3) deletion
of one branch in the deterministic layer, (4) two constant changes plus doc/test
upkeep. No new services, no schema migration, no API changes (existing
`OPTIONAL MATCH` queries already tolerate an entity-less `SourceSystem`).

**Tech Stack:** Python 3.12, pydantic v2, Neo4j Python driver, pytest, mypy --strict,
ruff. This repo's CI policy (`CLAUDE.md`) forbids running `pytest`/`mypy`/`ruff` on
the host — every task's verification step is "push to the PR branch and read the
Woodpecker verdict via `wpci home`", not a local command.

## Global Constraints

- Strict typing throughout: every variable/parameter/attribute has an explicit
  concrete type, no `Any`, no bare `dict`/`list`. Every function has a return type
  annotation.
- Must pass `mypy --strict` and `ruff check` (see CLAUDE.md CI lint findings section
  for known gotchas: trailing newline W292, line length E501 ≤100, closure-in-loop
  B023, `ruff format` drift).
- **Do not run `uv run pytest` / `mypy` / `ruff` on the host.** Push to a PR branch
  and read the verdict via `wpci home pipeline last sparkfn/hyperP --branch <branch>`
  then `wpci home pipeline show sparkfn/hyperP <n>`.
- Never commit without explicit user confirmation for this session (already
  established) — each task's commit step still stages the work, but actually running
  `git commit` requires the user's go-ahead at execution time.
- Follow existing test conventions exactly: fake `ManagedTransaction`/`Result`
  classes with a `run(query, **params)` method that pattern-matches on Cypher
  fragments (see `test_bankruptcy_name_gate.py`, `test_rental_flats_address_pipeline.py`,
  and the shared `_RecordingTx` in `services/ingestion/tests/_txmock.py`).
- Module docstrings and CLAUDE.md prose that state the old 0.90/0.60 bands must be
  updated in the same task that changes the constants — stale docs are lint-clean but
  are a plan defect.

---

## File Structure

| File | Change |
|---|---|
| `services/ingestion/src/graph/queries/entities.py` | Add `UPSERT_SOURCE_SYSTEM` (entity-less variant) |
| `services/ingestion/src/graph/queries/__init__.py` | Export `UPSERT_SOURCE_SYSTEM` |
| `services/ingestion/src/graph/bootstrap.py` | Drop `sggov` entity; make SG source `entity_key` optional; branch the upsert loop |
| `services/ingestion/src/main.py` | Import `_is_sg_match_only_source` from `src.pipeline` alongside `IngestPipeline` |
| `services/ingestion/src/models.py` | Add `IngestResult.dropped: bool = False` |
| `services/ingestion/src/pipeline.py` | Add `_SG_MATCH_ONLY_SOURCES` + `_is_sg_match_only_source`; reorder `_execute_ingest` so SG match-only sources probe candidates/match before any write; drop when unmatched |
| `services/ingestion/src/matching/deterministic.py` | Remove the bankruptcy name-conflict branch in `_check_government_id`; drop now-unused imports |
| `services/ingestion/src/matching/heuristic.py` | `CONFIDENCE_AUTO_MERGE = 0.40`, `CONFIDENCE_REVIEW = 0.20`; update module docstring |
| `CLAUDE.md` | Update the "Confidence bands" line under Key Design Decisions |
| `services/ingestion/tests/test_bankruptcy_name_gate.py` | Delete (the gate it tests no longer exists) |
| `services/ingestion/tests/test_match_engine_system_family.py` | Update the one assertion that pins bankruptcy/identity name-gate divergence |
| `services/ingestion/tests/test_relationship_promotion.py` | Fix the boundary flip on `test_identity_not_promoted_same_inputs` |
| `services/ingestion/tests/test_sg_match_only_pipeline.py` | New — pipeline-level match-only/drop behavior |
| `services/ingestion/tests/test_bootstrap_entities.py` | New — SG sources are entity-less; other sources unaffected |

---

### Task 1: Entity-less `SourceSystem` upsert query

**Files:**
- Modify: `services/ingestion/src/graph/queries/entities.py`
- Modify: `services/ingestion/src/graph/queries/__init__.py`
- Test: `services/ingestion/tests/test_bootstrap_entities.py` (new file, also used by Task 2)

**Interfaces:**
- Produces: `queries.UPSERT_SOURCE_SYSTEM: str` — a Cypher constant with the same
  `MERGE (ss:SourceSystem {source_key: $source_key}) ... RETURN ss.source_system_id AS source_system_id`
  shape as `UPSERT_SOURCE_SYSTEM_WITH_ENTITY`, but with no `Entity` match and no
  `OPERATED_BY` edge.

- [ ] **Step 1: Write the failing test**

Create `services/ingestion/tests/test_bootstrap_entities.py`:

```python
"""Entity/source-system bootstrap seed data — SG sources carry no entity_key."""

from __future__ import annotations

from src.graph import queries
from src.graph.bootstrap import _ENTITIES, _SOURCE_SYSTEMS, SOURCE_KEY_TO_ENTITY


def test_sggov_entity_is_not_seeded() -> None:
    entity_keys = {entity["entity_key"] for entity in _ENTITIES}
    assert "sggov" not in entity_keys


def test_sg_source_systems_have_no_entity_key() -> None:
    by_key = {source["source_key"]: source for source in _SOURCE_SYSTEMS}
    assert by_key["sgbankruptcy"]["entity_key"] is None
    assert by_key["sgrentalflats"]["entity_key"] is None


def test_non_sg_source_systems_still_have_an_entity_key() -> None:
    by_key = {source["source_key"]: source for source in _SOURCE_SYSTEMS}
    assert by_key["fundbox_consumer_backend"]["entity_key"] == "fundbox"
    assert by_key["onediver"]["entity_key"] == "onediver"


def test_source_key_to_entity_omits_entity_less_sg_sources() -> None:
    assert "sgbankruptcy" not in SOURCE_KEY_TO_ENTITY
    assert "sgrentalflats" not in SOURCE_KEY_TO_ENTITY
    assert SOURCE_KEY_TO_ENTITY["fundbox_consumer_backend"] == "fundbox"


def test_upsert_source_system_query_has_no_entity_match() -> None:
    assert "MATCH (e:Entity" not in queries.UPSERT_SOURCE_SYSTEM
    assert "OPERATED_BY" not in queries.UPSERT_SOURCE_SYSTEM
    assert "MERGE (ss:SourceSystem {source_key: $source_key})" in queries.UPSERT_SOURCE_SYSTEM
```

- [ ] **Step 2: Run test to verify it fails**

This will fail at collection/import time (`_ENTITIES`/`_SOURCE_SYSTEMS` still seed
`sggov` with a required `entity_key`, and `queries.UPSERT_SOURCE_SYSTEM` doesn't
exist yet) — expected, since Task 1 only adds the query and Task 2 changes the
bootstrap data. Do not run this locally per the Global Constraints — push to the PR
branch after Step 3 below and read the combined result from `wpci home`.

- [ ] **Step 3: Add the `UPSERT_SOURCE_SYSTEM` query**

In `services/ingestion/src/graph/queries/entities.py`, add below
`UPSERT_SOURCE_SYSTEM_WITH_ENTITY`:

```python
#: Idempotent create of a SourceSystem node with no owning Entity. Used for
#: source systems that do not belong to any real-world organisation in this
#: platform's ownership model (e.g. government registers).
UPSERT_SOURCE_SYSTEM = """
MERGE (ss:SourceSystem {source_key: $source_key})
ON CREATE SET
    ss.source_system_id = randomUUID(),
    ss.created_at       = datetime()
SET
    ss.display_name = $display_name,
    ss.system_type  = $system_type,
    ss.is_active    = true,
    ss.field_trust  = $field_trust,
    ss.updated_at   = datetime()
RETURN ss.source_system_id AS source_system_id
"""
```

In `services/ingestion/src/graph/queries/__init__.py`:
- Add `UPSERT_SOURCE_SYSTEM` to the import from `src.graph.queries.entities` (line 13-16):

```python
from src.graph.queries.entities import (
    UPSERT_ENTITY,
    UPSERT_SOURCE_SYSTEM,
    UPSERT_SOURCE_SYSTEM_WITH_ENTITY,
)
```

- Add `"UPSERT_SOURCE_SYSTEM",` to `__all__` (alphabetically, immediately before
  `"UPSERT_SOURCE_SYSTEM_WITH_ENTITY",` at line 193).

- [ ] **Step 4: Commit**

```bash
git add services/ingestion/src/graph/queries/entities.py services/ingestion/src/graph/queries/__init__.py services/ingestion/tests/test_bootstrap_entities.py
git commit -m "feat(ingestion): add entity-less SourceSystem upsert query"
```

(This task's test won't fully pass until Task 2 lands — that's expected; Task 2's
commit is what turns the suite green. Do not push/verify CI until Task 2 is also
committed.)

---

### Task 2: Remove `sggov` Entity; make SG source systems entity-less

**Files:**
- Modify: `services/ingestion/src/graph/bootstrap.py`
- Test: `services/ingestion/tests/test_bootstrap_entities.py` (from Task 1 — now fully green)

**Interfaces:**
- Consumes: `queries.UPSERT_SOURCE_SYSTEM` (Task 1).
- Produces: `_SourceSystemSeed.entity_key: str | None`; `_SOURCE_SYSTEMS` entries for
  `sgbankruptcy`/`sgrentalflats` with `entity_key=None`; `SOURCE_KEY_TO_ENTITY: dict[str, str]`
  no longer containing `"sgbankruptcy"`/`"sgrentalflats"` as keys;
  `bootstrap_entities_and_sources(client: Neo4jClient) -> None` unchanged signature.

- [ ] **Step 1: Confirm the test file from Task 1 still fails for the bootstrap-data
      assertions**

(No new test needed — reuse `test_bootstrap_entities.py` from Task 1. Its
`test_sggov_entity_is_not_seeded`, `test_sg_source_systems_have_no_entity_key`, and
`test_source_key_to_entity_omits_entity_less_sg_sources` cases are the ones this
task turns green.)

- [ ] **Step 2: Update `_EntitySeed`/`_SourceSystemSeed` TypedDicts**

In `services/ingestion/src/graph/bootstrap.py`, change the `_SourceSystemSeed`
TypedDict (lines 29-34) so `entity_key` is optional:

```python
class _SourceSystemSeed(TypedDict):
    source_key: str
    display_name: str
    system_type: str
    entity_key: str | None
    field_trust: dict[str, str]
```

- [ ] **Step 3: Remove the `sggov` entity**

Delete the `sggov` entry from `_ENTITIES` (currently lines 56-61):

```python
    {
        "entity_key": "sggov",
        "display_name": "SG Gov",
        "entity_type": "government",
        "country_code": "SG",
    },
```

`_ENTITIES` now has four entries: `fundbox`, `speedzone`, `eko`, `onediver`.

- [ ] **Step 4: Make the two SG source-system entries entity-less**

Change the `sgbankruptcy` and `sgrentalflats` entries in `_SOURCE_SYSTEMS`
(currently lines 197-210) to set `entity_key` to `None`:

```python
    {
        "source_key": "sgbankruptcy",
        "display_name": "SG Bankruptcy Register",
        "system_type": "government_registry",
        "entity_key": None,
        "field_trust": _GOVERNMENT_REGISTRY_TRUST,
    },
    {
        "source_key": "sgrentalflats",
        "display_name": "SG Rental Flats",
        "system_type": "government_registry",
        "entity_key": None,
        "field_trust": _GOVERNMENT_REGISTRY_TRUST,
    },
```

- [ ] **Step 5: Fix `SOURCE_KEY_TO_ENTITY` to skip entity-less sources**

Replace the dict comprehension (currently lines 231-233):

```python
SOURCE_KEY_TO_ENTITY: dict[str, str] = {
    source["source_key"]: source["entity_key"] for source in _SOURCE_SYSTEMS
}
```

with a comprehension that filters out `None` entity keys:

```python
SOURCE_KEY_TO_ENTITY: dict[str, str] = {
    source["source_key"]: source["entity_key"]
    for source in _SOURCE_SYSTEMS
    if source["entity_key"] is not None
}
```

- [ ] **Step 6: Branch the bootstrap write loop on `entity_key`**

Replace the `for source in _SOURCE_SYSTEMS:` loop body inside
`bootstrap_entities_and_sources`'s `_work` (currently lines 248-256):

```python
        for source in _SOURCE_SYSTEMS:
            tx.run(
                queries.UPSERT_SOURCE_SYSTEM_WITH_ENTITY,
                entity_key=source["entity_key"],
                source_key=source["source_key"],
                display_name=source["display_name"],
                system_type=source["system_type"],
                field_trust=json.dumps(source["field_trust"]),
            )
```

with:

```python
        for source in _SOURCE_SYSTEMS:
            if source["entity_key"] is None:
                tx.run(
                    queries.UPSERT_SOURCE_SYSTEM,
                    source_key=source["source_key"],
                    display_name=source["display_name"],
                    system_type=source["system_type"],
                    field_trust=json.dumps(source["field_trust"]),
                )
            else:
                tx.run(
                    queries.UPSERT_SOURCE_SYSTEM_WITH_ENTITY,
                    entity_key=source["entity_key"],
                    source_key=source["source_key"],
                    display_name=source["display_name"],
                    system_type=source["system_type"],
                    field_trust=json.dumps(source["field_trust"]),
                )
```

- [ ] **Step 7: Commit**

```bash
git add services/ingestion/src/graph/bootstrap.py
git commit -m "feat(ingestion): drop SG Gov entity, make SG sources entity-less"
```

- [ ] **Step 8: Push and verify via CI**

Push the branch and read the Woodpecker verdict per this repo's CI policy:

```bash
git push -u origin <branch-name>
wpci home pipeline last sparkfn/hyperP --branch <branch-name>
```

Expected: PR pipeline green, including `test_bootstrap_entities.py`'s five
assertions and `ruff check`/`mypy --strict` on `bootstrap.py`.

---

### Task 3: SG bankruptcy match-only ingestion (no new persons; drop unmatched)

**Files:**
- Modify: `services/ingestion/src/main.py`
- Modify: `services/ingestion/src/models.py`
- Modify: `services/ingestion/src/pipeline.py`
- Test: `services/ingestion/tests/test_sg_match_only_pipeline.py` (new)

**Interfaces:**
- Consumes: `IngestPipeline._resolve_person` (unchanged signature), `find_candidates`,
  `MatchEngine.evaluate` (all pre-existing, from `src.pipeline_writes` /
  `src.matching.engine`).
- Produces: `pipeline._SG_MATCH_ONLY_SOURCES: frozenset[str]` and
  `pipeline._is_sg_match_only_source(source_key: str) -> bool`, both defined in
  `src.pipeline` and re-exposed at `src.main` module scope via
  `from src.pipeline import IngestPipeline, _is_sg_match_only_source` (so
  `from src.main import _is_sg_match_only_source` still works for callers/tests
  that import it from `main`); `IngestResult.dropped: bool = False` (new field);
  `IngestPipeline.ingest` now returns an `IngestResult` with `dropped=True` and no
  graph writes for an SG match-only source with no usable match.

- [ ] **Step 1: Write the failing test**

Create `services/ingestion/tests/test_sg_match_only_pipeline.py`:

```python
"""SG bankruptcy ingestion is match-only: no new persons, unmatched dropped."""

from __future__ import annotations

from typing import cast

from _txmock import _RecordingTx
from neo4j import ManagedTransaction
from src.main import _is_sg_match_only_source
from src.models import QualityFlag, SourceRecordEnvelope
from src.pipeline import IngestPipeline


class _Result:
    def __init__(self, row: dict[str, object] | None = None, rows: list[dict[str, object]] | None = None) -> None:
        self._row = row
        self._rows = rows or ([] if row is None else [row])

    def single(self) -> dict[str, object] | None:
        return self._row

    def __iter__(self) -> object:
        return iter(self._rows)


class _Tx(_RecordingTx):
    """No candidate ever owns the incoming NRIC — nothing matches."""

    def run(self, query: str, **kwargs: object) -> _Result:
        self._record(query, kwargs)
        return _Result()


class _MatchedTx(_RecordingTx):
    """The incoming NRIC is owned by an existing person-1 with a VALID edge."""

    def run(self, query: str, **kwargs: object) -> _Result:
        self._record(query, kwargs)
        if "person_id AS person_id" in query and "rel.quality_flag = 'valid'" in query:
            return _Result({"person_id": "person-1"})
        if "candidate:Person" in query:
            return _Result(rows=[{"person_id": "person-1"}])
        if "RETURN sr.source_record_pk AS source_record_pk" in query:
            return _Result({"source_record_pk": "sr-1"})
        if "match_decision_id" in query:
            return _Result({"match_decision_id": "md-1"})
        if "merge_event_id" in query:
            return _Result({"merge_event_id": "me-1"})
        return _Result()


class _Session:
    def __init__(self, tx: _RecordingTx) -> None:
        self.tx = tx

    def __enter__(self) -> "_Session":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def execute_write(self, callback: object) -> object:
        return callback(cast(ManagedTransaction, self.tx))  # type: ignore[operator]


class _Client:
    def __init__(self, tx: _RecordingTx) -> None:
        self._tx = tx

    def session(self) -> _Session:
        return _Session(self._tx)

    def execute_read(self, callback: object) -> object:
        return callback(cast(ManagedTransaction, self._tx))  # type: ignore[operator]


def _bankruptcy_envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="sgbankruptcy",
        source_record_id="bankruptcy_case:1",
        record_type="bankruptcy",
        observed_at="2026-05-08T09:47:25+00:00",
        record_hash="sha256:abc",
        identifiers=[{"type": "nric", "value": "S1234567A", "is_verified": True}],
        attributes={"full_name": "Ada Lovelace"},
        raw_payload={"case": {"id": "1"}},
    )


def test_sgbankruptcy_is_match_only_source() -> None:
    assert _is_sg_match_only_source("sgbankruptcy") is True
    assert _is_sg_match_only_source("sgrentalflats") is False
    assert _is_sg_match_only_source("fundbox_consumer_backend") is False


def test_unmatched_bankruptcy_record_is_dropped_with_no_writes() -> None:
    tx = _Tx()
    client = _Client(tx)
    pipeline = IngestPipeline(cast(object, client))

    result = pipeline.ingest(_bankruptcy_envelope(), ingest_run_id="run-1")

    assert result.dropped is True
    assert result.person_id is None
    write_queries = [q for q, _ in tx.calls]
    assert not any("CREATE (p:Person" in q or "MERGE (p:Person" in q for q in write_queries)
    assert not any("CREATE_SOURCE_RECORD" in q or "sr:SourceRecord" in q for q in write_queries)


def test_matched_bankruptcy_record_links_to_existing_person() -> None:
    tx = _MatchedTx()
    client = _Client(tx)
    pipeline = IngestPipeline(cast(object, client))

    result = pipeline.ingest(_bankruptcy_envelope(), ingest_run_id="run-1")

    assert result.dropped is False
    assert result.person_id == "person-1"
    assert result.is_new_person is False
```

- [ ] **Step 2: Run test to verify it fails**

Expected failures: `_is_sg_match_only_source` doesn't exist yet (`ImportError`);
`IngestResult` has no `dropped` field; the unmatched-record test would currently
create a new `Person` instead of dropping. Do not run locally — this is validated
via the PR pipeline after Step 7.

- [ ] **Step 3: Add the SG match-only source set to `pipeline.py`**

`main.py` already imports `IngestPipeline` from `src.pipeline` (line 41 —
`from src.pipeline import IngestPipeline`), so defining the new source-family
check in `main.py` and importing it back into `pipeline.py` would create a
circular import. Define it in `services/ingestion/src/pipeline.py` instead, near
the top of the module, right after the existing imports and before the
`logger = logging.getLogger(__name__)` line:

```python
_SG_MATCH_ONLY_SOURCES = frozenset({"sgbankruptcy"})


def _is_sg_match_only_source(source_key: str) -> bool:
    return source_key in _SG_MATCH_ONLY_SOURCES


logger = logging.getLogger(__name__)
```

Then in `services/ingestion/src/main.py`, change the existing import (line 41)
so `main.py` re-exposes the same name at module scope (this is why the test in
Step 1 can still do `from src.main import _is_sg_match_only_source`):

```python
from src.pipeline import IngestPipeline, _is_sg_match_only_source
```

- [ ] **Step 4: Add `dropped` to `IngestResult`**

In `services/ingestion/src/models.py`, add a field to `IngestResult` (currently
ends at line 267):

```python
class IngestResult(BaseModel):
    """Summary returned after processing a single source record."""

    source_record_id: str
    source_record_pk: str | None = None
    person_id: str | None = None
    is_new_person: bool = False
    candidate_count: int = 0
    match_decision: MatchDecision | None = None
    ingest_run_id: str | None = None
    match_decision_id: str | None = None
    review_case_id: str | None = None
    errors: list[str] = Field(default_factory=list)
    skipped_duplicate: bool = False
    dropped: bool = False
```

- [ ] **Step 5: Reorder `_execute_ingest` for match-only sources**

In `services/ingestion/src/pipeline.py`, `_execute_ingest` (currently lines
151-225) currently starts:

```python
        upsert_nodes(tx, identifiers, addresses)
        candidates = find_candidates(tx, identifiers, addresses)
        match_result = self._match_engine.evaluate(
            tx,
            candidates,
            identifiers,
            addresses[0] if addresses else None,
            attributes,
            record_type=envelope.record_type,
        )
        person_id, is_new_person = self._resolve_person(tx, match_result, candidates)
```

Replace with a match-only probe that runs `find_candidates` + `evaluate` *before*
`upsert_nodes`, and returns a dropped result when the source is match-only and no
usable match exists:

```python
        candidates = find_candidates(tx, identifiers, addresses)
        match_result = self._match_engine.evaluate(
            tx,
            candidates,
            identifiers,
            addresses[0] if addresses else None,
            attributes,
            record_type=envelope.record_type,
        )
        if _is_sg_match_only_source(envelope.source_system) and not self._has_usable_match(
            match_result, candidates
        ):
            logger.info(
                "Dropping unmatched match-only record %s (source=%s, decision=%s)",
                envelope.source_record_id,
                envelope.source_system,
                match_result.decision.value,
            )
            return IngestResult(
                source_record_id=envelope.source_record_id,
                ingest_run_id=ingest_run_id,
                dropped=True,
            )
        upsert_nodes(tx, identifiers, addresses)
        person_id, is_new_person = self._resolve_person(tx, match_result, candidates)
```

`_is_sg_match_only_source` is already defined in `pipeline.py` itself (Step 3), so
no new import is needed for that call — it's a plain module-level name.

Add the `_has_usable_match` static helper method to `IngestPipeline`, right above
`_resolve_person` (currently at line 323):

```python
    @staticmethod
    def _has_usable_match(
        match_result: MatchResult,
        candidates: list[CandidateResult],
    ) -> bool:
        """True when the match result resolves to an existing person.

        MERGE always resolves to an existing person. REVIEW resolves to an
        existing person only when the engine or the top candidate provides one
        — a REVIEW with no ``matched_person_id`` and no candidates has nothing
        to attach to.
        """
        if match_result.decision == MatchDecision.MERGE:
            return True
        if match_result.decision == MatchDecision.REVIEW:
            return match_result.matched_person_id is not None or bool(candidates)
        return False
```

- [ ] **Step 6: Commit**

```bash
git add services/ingestion/src/main.py services/ingestion/src/models.py services/ingestion/src/pipeline.py services/ingestion/tests/test_sg_match_only_pipeline.py
git commit -m "feat(ingestion): sgbankruptcy ingestion is match-only, drops unmatched records"
```

- [ ] **Step 7: Push and verify via CI**

```bash
git push -u origin <branch-name>
wpci home pipeline last sparkfn/hyperP --branch <branch-name>
```

Expected: PR pipeline green, including the new pipeline test and
`mypy --strict`/`ruff check` on `pipeline.py`/`main.py`/`models.py`.

---

### Task 4: Remove the bankruptcy NRIC name-conflict gate

**Files:**
- Modify: `services/ingestion/src/matching/deterministic.py`
- Modify: `services/ingestion/tests/test_match_engine_system_family.py`
- Delete: `services/ingestion/tests/test_bankruptcy_name_gate.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `evaluate_deterministic(tx, candidate_person_id, identifiers, attributes,
  record_type) -> MatchResult | None` — unchanged signature; for
  `record_type == RecordType.BANKRUPTCY` now behaves identically to `IDENTITY`
  (name is never consulted for the govt-ID hard-merge check).

- [ ] **Step 1: Delete the obsolete test file**

The gate this file tests is being removed entirely, so its assertions
(`test_bankruptcy_blocks_nric_merge_on_name_conflict`,
`test_bankruptcy_merges_on_nric_when_no_incoming_name`, etc.) no longer describe
real behavior. Delete `services/ingestion/tests/test_bankruptcy_name_gate.py`.

- [ ] **Step 2: Update the system-family regression test**

`services/ingestion/tests/test_match_engine_system_family.py` currently documents
(in its module docstring, lines 1-9) that bankruptcy diverges from identity via the
name gate, and its `test_deterministic_nric_merge_identical_across_system_family`
test (lines 86-96) already asserts all three `SYSTEM_FAMILY` types produce an
*identical* `MatchResult` when there's no incoming name — this still passes
unchanged. Update only the module docstring to remove the now-false claim about
bankruptcy's name gate:

```python
"""Regression: system-family matching, with the per-record-type divergences.

`identity`, `bankruptcy`, and `relationship` make up the system family (they
replaced the former single `system` record type). They share the same
deterministic NRIC merge regardless of name. The one deliberate divergence
(Spec 2) is pinned here: `relationship` adds a Layer-2 phone + partial-name
auto-merge promotion. `identity` and `bankruptcy` keep the plain additive
behaviour and are identical in both the deterministic and heuristic layers.
"""
```

Also strengthen `test_deterministic_nric_merge_identical_across_system_family`
(lines 86-96) to prove bankruptcy no longer diverges even *with* a conflicting
name — add a new test case:

```python
def test_bankruptcy_merges_on_nric_regardless_of_name_conflict() -> None:
    # Previously blocked by a bankruptcy-specific name-conflict gate; that gate
    # has been removed, so a conflicting name no longer prevents the merge.
    tx = _NricTx()
    res = evaluate_deterministic(
        tx,  # type: ignore[arg-type]
        "person-1",
        _nric(),
        [
            NormalizedAttribute(
                attribute_name="full_name",
                attribute_value="Kok Pin",
                quality_flag=QualityFlag.VALID,
            )
        ],
        RecordType.BANKRUPTCY,
    )
    assert res is not None
    assert res.decision == MatchDecision.MERGE
```

This requires adding `NormalizedAttribute` to the existing import from
`src.models` at the top of the file (currently lines 18-27):

```python
from src.models import (
    SYSTEM_FAMILY,
    CandidateResult,
    MatchDecision,
    MatchResult,
    NormalizedAttribute,
    NormalizedIdentifier,
    QualityFlag,
    RecordType,
)
```

- [ ] **Step 3: Remove the bankruptcy branch from `_check_government_id`**

In `services/ingestion/src/matching/deterministic.py`, `_check_government_id`
(currently lines 163-226) currently has:

```python
    govt_ids = [
        i
        for i in identifiers
        if i.identifier_type == "nric" and i.quality_flag == QualityFlag.VALID
    ]
    for govt_id in govt_ids:
        if tx.run(
            _PERSON_HAS_VALID_GOVT_ID,
            person_id=candidate_person_id,
            normalized_value=govt_id.normalized_value,
        ).single():
            if record_type == RecordType.BANKRUPTCY:
                verdict = is_partial_name_match(
                    attributes, fetch_candidate_snapshot(tx, candidate_person_id).names()
                )
                if verdict is False:
                    logger.info(
                        "Bankruptcy NRIC match for candidate %s blocked: name conflict",
                        candidate_person_id,
                    )
                    return None  # fall through to heuristic (→ no-match / pair review)
            logger.info(
                "Deterministic hard merge: candidate %s shares govt ID hash",
                candidate_person_id,
            )
            return MatchResult(
                decision=MatchDecision.MERGE,
                confidence=1.0,
                reasons=["Exact government ID hash match"],
                engine_type=EngineType.DETERMINISTIC,
                matched_person_id=candidate_person_id,
            )
```

Replace with (name-conflict branch removed):

```python
    govt_ids = [
        i
        for i in identifiers
        if i.identifier_type == "nric" and i.quality_flag == QualityFlag.VALID
    ]
    for govt_id in govt_ids:
        if tx.run(
            _PERSON_HAS_VALID_GOVT_ID,
            person_id=candidate_person_id,
            normalized_value=govt_id.normalized_value,
        ).single():
            logger.info(
                "Deterministic hard merge: candidate %s shares govt ID hash",
                candidate_person_id,
            )
            return MatchResult(
                decision=MatchDecision.MERGE,
                confidence=1.0,
                reasons=["Exact government ID hash match"],
                engine_type=EngineType.DETERMINISTIC,
                matched_person_id=candidate_person_id,
            )
```

Update the function's docstring (currently lines 170-176):

```python
def _check_government_id(
    tx: ManagedTransaction,
    candidate_person_id: str,
    identifiers: list[NormalizedIdentifier],
    attributes: list[NormalizedAttribute],
    record_type: RecordType,
) -> MatchResult | None:
    """Government ID hash: exact match → hard MERGE; conflict → hard NO_MATCH.

    A matching valid-quality NRIC always hard-merges — name is never consulted
    for this check, for any record type.
    """
```

Note `attributes` and `record_type` are now unused inside `_check_government_id`'s
body but must stay in the signature — `evaluate_deterministic` (the caller) still
passes them positionally and other callers/tests rely on the existing signature.
Since they're unused now, check whether `ruff` flags them (`ARG001` is not in this
repo's `select` list per `pyproject.toml`'s `[tool.ruff.lint]` — only
`["E", "F", "W", "I", "N", "UP", "B", "ANN", "RET"]` — so unused-argument warnings
are not enabled and no `# noqa` is needed).

- [ ] **Step 4: Remove now-unused imports**

`is_partial_name_match` (from `src.matching.names`) and `fetch_candidate_snapshot`
(from `src.matching.snapshot`) were only used inside the deleted branch. Remove
their imports at the top of `deterministic.py` (currently lines 17-18):

```python
from src.matching.names import is_partial_name_match
from src.matching.snapshot import fetch_candidate_snapshot
```

Confirm no other line in `deterministic.py` references `is_partial_name_match` or
`fetch_candidate_snapshot` before deleting — Step 3 above is the only call site.

- [ ] **Step 5: Commit**

```bash
git add services/ingestion/src/matching/deterministic.py services/ingestion/tests/test_match_engine_system_family.py
git rm services/ingestion/tests/test_bankruptcy_name_gate.py
git commit -m "feat(ingestion): remove bankruptcy NRIC name-conflict gate"
```

- [ ] **Step 6: Push and verify via CI**

```bash
git push -u origin <branch-name>
wpci home pipeline last sparkfn/hyperP --branch <branch-name>
```

Expected: PR pipeline green. `ruff check` must not flag unused imports (F401) since
Step 4 removed them; `mypy --strict` must not flag unused-import errors either.

---

### Task 5: Lower confidence thresholds to 0.40 / 0.20 and fix boundary-affected tests

**Files:**
- Modify: `services/ingestion/src/matching/heuristic.py`
- Modify: `services/ingestion/tests/test_relationship_promotion.py`
- Modify: `CLAUDE.md`

**Interfaces:**
- Produces: `CONFIDENCE_AUTO_MERGE = 0.40`, `CONFIDENCE_REVIEW = 0.20` (module-level
  constants in `src.matching.heuristic`, unchanged names/types, only values change).
  `PROMOTED_CONFIDENCE` (0.91) and `VEHICLE_MATCH_AUTO`/`VEHICLE_MATCH_REVIEW`
  (`src.matching.vehicle_heuristic`, 0.90/0.70) are untouched.

- [ ] **Step 1: Identify and pre-fix the known boundary-flip test**

`test_relationship_promotion.py::test_identity_not_promoted_same_inputs` (currently
lines 121-123) evaluates an `IDENTITY` record with an unverified phone match
(`PHONE_UNVERIFIED_WEIGHT = 0.20`) plus a high name-similarity match
(`NAME_HIGH_WEIGHT = 0.20`) against candidate "Ada Lovelace", for a raw score of
0.40. Under the old bands (`CONFIDENCE_AUTO_MERGE = 0.90`) this landed in NO_MATCH/
REVIEW territory and the test asserts `res.decision != MatchDecision.MERGE`. Under
the new `CONFIDENCE_AUTO_MERGE = 0.40`, a raw score of exactly 0.40 now meets the
`>= 0.40` auto-merge threshold, flipping the decision to `MERGE` and breaking the
assertion.

Update the test to keep testing its actual intent — that plain `IDENTITY` records
do **not** get the `relationship`-specific phone+name promotion path (as opposed to
merging for an unrelated reason) — by asserting on the *reason*, not just the
decision:

```python
def test_identity_not_promoted_same_inputs() -> None:
    res = _evaluate(_Tx(), record_type=RecordType.IDENTITY)
    # No record-type promotion fires for IDENTITY (that's relationship-only);
    # any MERGE here would come from plain additive heuristic scoring, not a
    # promotion — confirm the promotion path specifically did not fire.
    assert not any("promot" in r.lower() for r in res.reasons)
```

This is still a correctness-preserving change: the test's purpose (per its own
comment, "identity keeps the plain additive behaviour" from
`test_match_engine_system_family.py`'s docstring) is to confirm the
`relationship`-only promotion doesn't leak into `identity`, not to pin an exact
confidence band. Asserting on reasons instead of the raw decision decouples the
test from the exact threshold value.

- [ ] **Step 2: Change the threshold constants**

In `services/ingestion/src/matching/heuristic.py`, update the module docstring
(currently lines 1-7):

```python
"""Layer 2 heuristic scoring — conditional weights across phone/email/DOB/name/address.

Confidence bands:
    ≥ 0.40 → MERGE  (auto-merge)
    0.20–0.39 → REVIEW
    < 0.20 → NO_MATCH (explicit, so the orchestrator can drop it)
"""
```

And the constants (currently lines 66-67):

```python
CONFIDENCE_AUTO_MERGE = 0.40
CONFIDENCE_REVIEW = 0.20
```

- [ ] **Step 3: Update CLAUDE.md**

In `CLAUDE.md`, under "Key Design Decisions" (currently line 291):

```
- **Confidence bands**: ≥0.40 auto-merge, 0.20–0.39 human review, <0.20 no-match (lowered from the original 0.90/0.60 calibration).
```

- [ ] **Step 4: Commit**

```bash
git add services/ingestion/src/matching/heuristic.py services/ingestion/tests/test_relationship_promotion.py CLAUDE.md
git commit -m "feat(ingestion): lower auto-merge/review confidence thresholds to 0.40/0.20"
```

- [ ] **Step 5: Push and verify via CI**

```bash
git push -u origin <branch-name>
wpci home pipeline last sparkfn/hyperP --branch <branch-name>
```

Expected: full PR pipeline green across both services. If CI surfaces additional
boundary-affected test failures beyond the one identified in Step 1 (any heuristic
score between 0.20 and 0.60 that used to land differently), fix each by asserting
on `reasons`/`feature_snapshot` semantics rather than the raw decision where
possible, or by updating the expected `MatchDecision` where the test's intent is
genuinely about the raw score crossing a specific named band. Do not weaken a test
to merely "pass" — confirm each fix still asserts the behavior the test's docstring
describes.

---

## Task Order and Dependencies

Tasks 1→2 are sequential (Task 2 depends on the query Task 1 adds). Task 3 is
independent of 1/2 but touches `pipeline.py`/`main.py` — no file overlap with 1/2,
safe to run in parallel or after. Task 4 is fully independent (only touches
`deterministic.py` + its own tests). Task 5 is fully independent (only touches
`heuristic.py` + its own tests + CLAUDE.md). Recommended order: 1, 2, 3, 4, 5 —
sequential is simplest given the shared reviewer context, but 3/4/5 have no file
conflicts with each other if parallel execution is preferred.
