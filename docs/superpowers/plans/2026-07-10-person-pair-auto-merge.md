# Person-Pair Auto-Merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the person-pair auditor (`services/ingestion/src/pipeline_person_pairs.py`) scores a shared-identifier bridge between two existing persons at ≥ 0.60 confidence, auto-merge them instead of opening a human review case.

**Architecture:** A new `PERSON_PAIR_AUTO_MERGE = 0.60` threshold gates a branch in the existing detection loop. The merge itself executes locally inside the ingestion worker's Neo4j transaction (no cross-service call), reusing Cypher rewire queries already stubbed — but never wired up — in `graph/queries/merge.py`, `knows.py`, and `sales.py`, plus new redirect/close queries ported from the API's merge repository so other open review cases referencing the absorbed person get redirected. Survivor selection is deterministic (completeness score → created_at → person_id), since no human is choosing sides.

**Tech Stack:** Python 3.12, Neo4j driver (`ManagedTransaction`), pytest, mypy --strict, ruff.

## Global Constraints

- Every Python file ends with a trailing newline (W292) — the Write/Edit tools do not add one automatically.
- Lines ≤ 100 chars (E501); `ruff format` wraps code but not comments/docstrings.
- Strict typing everywhere: no `Any`, no untyped `dict`/`list` — use `dataclass`, explicit `str`/`float`/`bool` types, and `isinstance` narrowing (the pattern already used in `matching/pair_score.py`'s `_snapshot_identifiers`), not bare `.get()` with implicit `Any`.
- Every function/method has an explicit return type annotation.
- **Do not run `ruff`/`mypy`/`pytest` on the host to verify changes.** Per this repo's CI policy, validation happens by pushing to the PR branch and reading the Woodpecker verdict (`wpci home ...`). Steps below say "write the test" / "write the implementation" without a local run-and-check step for this reason — the final task pushes everything and validates via CI in one pass.
- **Never commit without explicit user confirmation**, even though steps below say "Commit" — treat those as reminders of what the commit will contain, not authorization to run `git commit` unattended.
- Branch: continue on the current branch `feat/sg-gov-removal-thresholds` (per user decision) — do not create a new branch or worktree.

---

## File Structure

| File | Responsibility |
|---|---|
| `services/ingestion/src/matching/pair_score.py` | *Modify.* Add the `PERSON_PAIR_AUTO_MERGE` threshold constant. |
| `services/ingestion/src/graph/queries/person_merge.py` | *Create.* New Cypher: fetch both persons' merge-relevant attributes (status/completeness/created_at), and the four review-case redirect/close queries ported from the API. |
| `services/ingestion/src/graph/queries/__init__.py` | *Modify.* Export the new `person_merge.py` constants. |
| `services/ingestion/src/pipeline_person_merge.py` | *Create.* `PairPersonAttrs` dataclass, `select_survivor()` (pure), `merge_person_pair()` (executes the full merge in one transaction). |
| `services/ingestion/src/pipeline_person_pairs.py` | *Modify.* Branch to auto-merge when the score clears the threshold instead of always opening a review case. |
| `services/ingestion/tests/test_pipeline_person_merge.py` | *Create.* Unit tests for `select_survivor()` and `merge_person_pair()`. |
| `services/ingestion/tests/test_person_pair_auditing.py` | *Modify.* Add auto-merge branch tests; update the existing `_ScriptedTx` fake to answer the new attributes query. |
| `docs/profile-unifier-matching-spec.md` | *Modify.* Update the "Person-Pair Auditing" section to describe the new auto-merge behavior. |

---

### Task 1: Auto-merge threshold constant

**Files:**
- Modify: `services/ingestion/src/matching/pair_score.py`
- Test: `services/ingestion/tests/test_pipeline_person_merge.py` (new file — created in this task, extended in Task 3)

**Interfaces:**
- Produces: `PERSON_PAIR_AUTO_MERGE: float` (module-level constant in `src.matching.pair_score`), consumed by Task 4's `pipeline_person_pairs.py` change.

- [ ] **Step 1: Write the failing test**

Create `services/ingestion/tests/test_pipeline_person_merge.py`:

```python
"""Person-pair auto-merge: threshold, survivor selection, merge execution."""

from __future__ import annotations

from src.matching.pair_score import PERSON_PAIR_AUTO_MERGE


def test_person_pair_auto_merge_threshold_is_060() -> None:
    assert PERSON_PAIR_AUTO_MERGE == 0.60
```

- [ ] **Step 2: Add the constant**

In `services/ingestion/src/matching/pair_score.py`, add near the top of the file (after the module docstring, before `def score_person_pair`):

```python
#: A person-pair bridge scoring at or above this confidence auto-merges the
#: two persons instead of opening a human review case. Distinct from
#: ``matching.heuristic.CONFIDENCE_AUTO_MERGE`` (0.90), which gates
#: source-record-to-person matching — person-pair merges use a higher bar
#: since no human confirms the merge.
PERSON_PAIR_AUTO_MERGE: float = 0.60
```

- [ ] **Step 3: Commit**

```bash
git add services/ingestion/src/matching/pair_score.py services/ingestion/tests/test_pipeline_person_merge.py
git commit -m "feat(ingestion): add person-pair auto-merge threshold constant"
```

---

### Task 2: Ported Cypher — pair attributes + review-case redirects

**Files:**
- Create: `services/ingestion/src/graph/queries/person_merge.py`
- Modify: `services/ingestion/src/graph/queries/__init__.py`
- Test: `services/ingestion/tests/test_pipeline_person_merge.py`

**Interfaces:**
- Produces: `FETCH_PAIR_MERGE_ATTRS`, `CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED`, `REDIRECT_PERSON_PAIR_CASES_ABSORBED_LEFT`, `REDIRECT_PERSON_PAIR_CASES_ABSORBED_RIGHT`, `REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED` — all re-exported from `src.graph.queries`, consumed by Task 3's `pipeline_person_merge.py` and Task 4's `pipeline_person_pairs.py`.

- [ ] **Step 1: Write the failing test**

Add `from src.graph import queries` to the top import block of
`services/ingestion/tests/test_pipeline_person_merge.py` (alongside the
existing `from src.matching.pair_score import PERSON_PAIR_AUTO_MERGE`), then
append this test function to the bottom of the file:

```python
def test_person_merge_query_constants_exist() -> None:
    assert "left_status" in queries.FETCH_PAIR_MERGE_ATTRS
    assert "right_status" in queries.FETCH_PAIR_MERGE_ATTRS
    assert "cancelled_superseded" in queries.CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED
    assert "ABOUT_LEFT" in queries.REDIRECT_PERSON_PAIR_CASES_ABSORBED_LEFT
    assert "ABOUT_RIGHT" in queries.REDIRECT_PERSON_PAIR_CASES_ABSORBED_RIGHT
    assert "source_record" in queries.REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED
```

- [ ] **Step 2: Run test to verify it fails**

This cannot be run locally per Global Constraints — the import of `queries.FETCH_PAIR_MERGE_ATTRS` (etc.) will fail with `AttributeError` once the module is created and step 3 is skipped; proceed to the implementation.

- [ ] **Step 3: Create the new query module**

Create `services/ingestion/src/graph/queries/person_merge.py`:

```python
"""Cypher for person-pair auto-merge: pair attribute lookup and review-case
redirects on the absorbed person, ported from the API's merge repository
(``services/api/src/graph/queries/merge.py``) so an ingestion-driven merge
keeps the review queue consistent the same way a human-triggered merge does.
"""

from __future__ import annotations

FETCH_PAIR_MERGE_ATTRS = """
MATCH (a:Person {person_id: $left_person_id})
MATCH (b:Person {person_id: $right_person_id})
RETURN a.person_id AS left_person_id,
       a.status AS left_status,
       a.profile_completeness_score AS left_completeness,
       toString(a.created_at) AS left_created_at,
       b.person_id AS right_person_id,
       b.status AS right_status,
       b.profile_completeness_score AS right_completeness,
       toString(b.created_at) AS right_created_at
"""

CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT {entity_type: 'person'}]->(a:Person)
MATCH (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(b:Person)
WHERE rc.queue_state IN ['open', 'assigned', 'deferred']
  AND (
    (a.person_id = $absorbed_id AND b.person_id = $survivor_id)
    OR (b.person_id = $absorbed_id AND a.person_id = $survivor_id)
  )
SET rc.queue_state = 'cancelled',
    rc.resolution = 'cancelled_superseded',
    rc.resolved_at = datetime(),
    rc.closed_by_merge_event_id = $merge_event_id,
    rc.updated_at = datetime()
"""

REDIRECT_PERSON_PAIR_CASES_ABSORBED_LEFT = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[old_left:ABOUT_LEFT {entity_type: 'person'}]->(:Person {person_id: $absorbed_id})
MATCH (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(other:Person)
WHERE rc.queue_state IN ['open', 'assigned', 'deferred']
  AND other.person_id <> $survivor_id
MATCH (survivor:Person {person_id: $survivor_id})
CREATE (md)-[:ABOUT_LEFT {entity_type: 'person'}]->(survivor)
DELETE old_left
SET rc.redirected_pair_by_merge_event_id = $merge_event_id,
    rc.redirected_pair_from_person_id = $absorbed_id,
    rc.redirected_pair_side = 'left',
    rc.updated_at = datetime()
RETURN rc.review_case_id AS review_case_id
"""

REDIRECT_PERSON_PAIR_CASES_ABSORBED_RIGHT = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT {entity_type: 'person'}]->(other:Person)
MATCH (md)-[old_right:ABOUT_RIGHT {entity_type: 'person'}]->(:Person {person_id: $absorbed_id})
WHERE rc.queue_state IN ['open', 'assigned', 'deferred']
  AND other.person_id <> $survivor_id
MATCH (survivor:Person {person_id: $survivor_id})
CREATE (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(survivor)
DELETE old_right
SET rc.redirected_pair_by_merge_event_id = $merge_event_id,
    rc.redirected_pair_from_person_id = $absorbed_id,
    rc.redirected_pair_side = 'right',
    rc.updated_at = datetime()
RETURN rc.review_case_id AS review_case_id
"""

REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
WHERE rc.queue_state IN ['open', 'assigned', 'deferred']
MATCH (md)-[old_right:ABOUT_RIGHT {entity_type: 'person'}]->(absorbed:Person {person_id: $absorbed_id})
MATCH (md)-[:ABOUT_LEFT {entity_type: 'source_record'}]->(:SourceRecord)
MATCH (survivor:Person {person_id: $survivor_id})
CREATE (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(survivor)
DELETE old_right
SET rc.redirected_by_merge_event_id = $merge_event_id,
    rc.redirected_from_person_id = $absorbed_id,
    rc.updated_at = datetime()
"""
```

- [ ] **Step 4: Export the new constants**

In `services/ingestion/src/graph/queries/__init__.py`, add an import block (alphabetically after the `pair_audit_recalc` import block, before `person_pairs`):

```python
from src.graph.queries.person_merge import (
    CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED,
    FETCH_PAIR_MERGE_ATTRS,
    REDIRECT_PERSON_PAIR_CASES_ABSORBED_LEFT,
    REDIRECT_PERSON_PAIR_CASES_ABSORBED_RIGHT,
    REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
)
```

And add to `__all__` (alphabetically):

```python
    "CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED",
    "FETCH_PAIR_MERGE_ATTRS",
    "REDIRECT_PERSON_PAIR_CASES_ABSORBED_LEFT",
    "REDIRECT_PERSON_PAIR_CASES_ABSORBED_RIGHT",
    "REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED",
```

- [ ] **Step 5: Commit**

```bash
git add services/ingestion/src/graph/queries/person_merge.py services/ingestion/src/graph/queries/__init__.py services/ingestion/tests/test_pipeline_person_merge.py
git commit -m "feat(ingestion): port person-pair merge review-case redirect queries"
```

---

### Task 3: Survivor selection and merge execution

**Files:**
- Create: `services/ingestion/src/pipeline_person_merge.py`
- Test: `services/ingestion/tests/test_pipeline_person_merge.py`

**Interfaces:**
- Consumes: `queries.FETCH_PAIR_MERGE_ATTRS`, `queries.CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED`, `queries.REDIRECT_PERSON_PAIR_CASES_ABSORBED_LEFT`, `queries.REDIRECT_PERSON_PAIR_CASES_ABSORBED_RIGHT`, `queries.REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED` (Task 2); `queries.CREATE_MERGE_EVENT_AUTO_MERGE`, `queries.LINK_MERGE_EVENT_TRIGGERED_BY`, `queries.REWIRE_LINKED_TO`, `queries.REWIRE_IDENTIFIED_BY`, `queries.REWIRE_LIVES_AT`, `queries.REWIRE_HAS_FACT`, `queries.MARK_PERSON_MERGED`, `queries.CREATE_MERGED_INTO`, `queries.PATH_COMPRESS_MERGED_INTO` (all pre-existing in `graph/queries/merge.py`); `queries.REWIRE_KNOWS_OUT`, `queries.REWIRE_KNOWS_IN` (pre-existing in `graph/queries/knows.py`); `queries.REWIRE_PURCHASED` (pre-existing in `graph/queries/sales.py`); `compute_golden_profile(tx, person_id) -> dict[str, Any]` from `src.golden_profile` (pre-existing).
- Produces:
  - `PairPersonAttrs` (frozen dataclass: `person_id: str`, `status: str`, `profile_completeness_score: float`, `created_at: str`)
  - `fetch_pair_attrs(tx: ManagedTransaction, left_person_id: str, right_person_id: str) -> tuple[PairPersonAttrs, PairPersonAttrs] | None` — returns `None` if either person is missing from the query result (defensive; should not happen inside the same transaction that found them).
  - `select_survivor(left: PairPersonAttrs, right: PairPersonAttrs) -> tuple[str, str]` — returns `(survivor_id, absorbed_id)`.
  - `merge_person_pair(tx: ManagedTransaction, *, absorbed_id: str, survivor_id: str, match_decision_id: str, reason: str) -> str` — returns `merge_event_id`.
  - Consumed by Task 4's `pipeline_person_pairs.py`.

- [ ] **Step 1: Write the failing tests**

Add these two imports to the top import block of
`services/ingestion/tests/test_pipeline_person_merge.py`:

```python
import pytest
from _txmock import _RecordingTx
from src.pipeline_person_merge import PairPersonAttrs, merge_person_pair, select_survivor
```

Then append the following to the bottom of the file:

```python
def test_select_survivor_higher_completeness_wins() -> None:
    left = PairPersonAttrs("p-a", "active", 0.80, "2026-01-01T00:00:00Z")
    right = PairPersonAttrs("p-b", "active", 0.40, "2026-01-01T00:00:00Z")
    assert select_survivor(left, right) == ("p-a", "p-b")


def test_select_survivor_ties_break_on_earlier_created_at() -> None:
    left = PairPersonAttrs("p-a", "active", 0.50, "2026-02-01T00:00:00Z")
    right = PairPersonAttrs("p-b", "active", 0.50, "2026-01-01T00:00:00Z")
    assert select_survivor(left, right) == ("p-b", "p-a")


def test_select_survivor_final_tiebreak_on_person_id() -> None:
    left = PairPersonAttrs("p-b", "active", 0.50, "2026-01-01T00:00:00Z")
    right = PairPersonAttrs("p-a", "active", 0.50, "2026-01-01T00:00:00Z")
    assert select_survivor(left, right) == ("p-a", "p-b")


class _MergeResult:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self._records = records

    def single(self) -> dict[str, object] | None:
        return self._records[0] if self._records else None


class _MergeScriptedTx(_RecordingTx):
    """Records every query run against it; returns synthetic ids for creates."""

    def __init__(self) -> None:
        super().__init__()
        self._merge_event_counter = 0

    def run(self, query: str, **params: object) -> _MergeResult:
        self._record(query, params)
        if "RETURN me.merge_event_id AS merge_event_id" in query:
            self._merge_event_counter += 1
            return _MergeResult([{"merge_event_id": f"me-{self._merge_event_counter}"}])
        return _MergeResult([])


def test_merge_person_pair_runs_all_rewires_and_recomputes_golden_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recomputed: list[str] = []
    monkeypatch.setattr(
        "src.pipeline_person_merge.compute_golden_profile",
        lambda tx, person_id: recomputed.append(person_id),
    )
    tx = _MergeScriptedTx()
    merge_event_id = merge_person_pair(
        tx,  # type: ignore[arg-type]
        absorbed_id="p-absorbed",
        survivor_id="p-survivor",
        match_decision_id="md-1",
        reason="test merge",
    )
    assert merge_event_id == "me-1"
    queries_run_text = "\n---\n".join(q for q, _ in tx.calls)
    assert "CREATE (me:MergeEvent" in queries_run_text
    assert "event_type: 'auto_merge'" in queries_run_text
    assert "TRIGGERED_BY" in queries_run_text
    assert "MATCH (sr:SourceRecord)-[old:LINKED_TO]->(absorbed:Person" in queries_run_text
    assert "OPTIONAL MATCH (absorbed)-[old_id:IDENTIFIED_BY]->" in queries_run_text
    assert "OPTIONAL MATCH (absorbed)-[old_addr:LIVES_AT]->" in queries_run_text
    assert "MATCH (absorbed:Person" in queries_run_text and "HAS_FACT" in queries_run_text
    assert "SET absorbed.status = 'merged'" in queries_run_text
    assert "CREATE (absorbed)-[:MERGED_INTO" in queries_run_text
    assert "MATCH (prev:Person)-[old:MERGED_INTO]->(absorbed:Person" in queries_run_text
    assert "KNOWS]->(other:Person)" in queries_run_text
    assert "(other:Person)-[old:KNOWS]->(absorbed:Person" in queries_run_text
    assert "PURCHASED]->(o:Order)" in queries_run_text
    assert "cancelled_superseded" in queries_run_text
    assert "redirected_pair_side = 'left'" in queries_run_text
    assert "redirected_pair_side = 'right'" in queries_run_text
    assert "source_record" in queries_run_text
    assert recomputed == ["p-survivor"]
```

- [ ] **Step 2: Run test to verify it fails**

Per Global Constraints, this is not run locally — proceed to the implementation.

- [ ] **Step 3: Write the implementation**

Create `services/ingestion/src/pipeline_person_merge.py`:

```python
"""Person-pair auto-merge execution.

Executes a full person-to-person merge locally inside the ingestion worker's
Neo4j transaction: rewires every relationship type a person can carry
(LINKED_TO, IDENTIFIED_BY, LIVES_AT, HAS_FACT, KNOWS, PURCHASED) from the
absorbed person to the survivor, marks the absorbed person merged, records
lineage, recomputes the survivor's golden profile, and redirects any other
open review case that referenced the absorbed person.

This mirrors the merge machinery in ``services/api/src/repositories/neo4j/merge.py``
(human-gated, review-case-triggered) but runs without a human in the loop —
used only when :mod:`src.pipeline_person_pairs` finds a person-pair bridge
scoring at or above ``matching.pair_score.PERSON_PAIR_AUTO_MERGE``.
"""

from __future__ import annotations

from dataclasses import dataclass

from neo4j import ManagedTransaction

from src.golden_profile import compute_golden_profile
from src.graph import queries


@dataclass(frozen=True)
class PairPersonAttrs:
    """Attributes of one side of a person-pair, used for survivor selection."""

    person_id: str
    status: str
    profile_completeness_score: float
    created_at: str


def fetch_pair_attrs(
    tx: ManagedTransaction,
    left_person_id: str,
    right_person_id: str,
) -> tuple[PairPersonAttrs, PairPersonAttrs] | None:
    """Fetch both persons' merge-relevant attributes in one round trip."""
    record = tx.run(
        queries.FETCH_PAIR_MERGE_ATTRS,
        left_person_id=left_person_id,
        right_person_id=right_person_id,
    ).single()
    if record is None:
        return None
    left_id = record.get("left_person_id")
    right_id = record.get("right_person_id")
    left_status = record.get("left_status")
    right_status = record.get("right_status")
    left_completeness = record.get("left_completeness")
    right_completeness = record.get("right_completeness")
    left_created_at = record.get("left_created_at")
    right_created_at = record.get("right_created_at")
    if (
        not isinstance(left_id, str)
        or not isinstance(right_id, str)
        or not isinstance(left_status, str)
        or not isinstance(right_status, str)
        or not isinstance(left_completeness, float)
        or not isinstance(right_completeness, float)
        or not isinstance(left_created_at, str)
        or not isinstance(right_created_at, str)
    ):
        return None
    return (
        PairPersonAttrs(left_id, left_status, left_completeness, left_created_at),
        PairPersonAttrs(right_id, right_status, right_completeness, right_created_at),
    )


def select_survivor(left: PairPersonAttrs, right: PairPersonAttrs) -> tuple[str, str]:
    """Deterministically pick which person survives a pair-merge.

    Higher ``profile_completeness_score`` wins; ties break on earlier
    ``created_at``; final tie-break is the lower ``person_id`` (consistent
    with the canonical pair-ordering convention used elsewhere, e.g.
    ``NO_MATCH_LOCK``). Returns ``(survivor_id, absorbed_id)``.
    """
    if left.profile_completeness_score != right.profile_completeness_score:
        winner = (
            left if left.profile_completeness_score > right.profile_completeness_score else right
        )
    elif left.created_at != right.created_at:
        winner = left if left.created_at < right.created_at else right
    else:
        winner = left if left.person_id < right.person_id else right
    loser = right if winner is left else left
    return winner.person_id, loser.person_id


def merge_person_pair(
    tx: ManagedTransaction,
    *,
    absorbed_id: str,
    survivor_id: str,
    match_decision_id: str,
    reason: str,
) -> str:
    """Execute a full person-to-person merge; returns the ``merge_event_id``."""
    me_record = tx.run(
        queries.CREATE_MERGE_EVENT_AUTO_MERGE,
        from_person_id=absorbed_id,
        to_person_id=survivor_id,
        reason=reason,
    ).single()
    assert me_record is not None, "CREATE_MERGE_EVENT_AUTO_MERGE must return a row"
    merge_event_id = me_record["merge_event_id"]
    assert isinstance(merge_event_id, str)

    tx.run(
        queries.LINK_MERGE_EVENT_TRIGGERED_BY,
        merge_event_id=merge_event_id,
        match_decision_id=match_decision_id,
    )

    tx.run(queries.REWIRE_LINKED_TO, absorbed_id=absorbed_id, survivor_id=survivor_id)
    tx.run(queries.REWIRE_IDENTIFIED_BY, absorbed_id=absorbed_id, survivor_id=survivor_id)
    tx.run(queries.REWIRE_LIVES_AT, absorbed_id=absorbed_id, survivor_id=survivor_id)
    tx.run(queries.REWIRE_HAS_FACT, absorbed_id=absorbed_id, survivor_id=survivor_id)
    tx.run(queries.REWIRE_KNOWS_OUT, absorbed_id=absorbed_id, survivor_id=survivor_id)
    tx.run(queries.REWIRE_KNOWS_IN, absorbed_id=absorbed_id, survivor_id=survivor_id)
    tx.run(queries.REWIRE_PURCHASED, absorbed_id=absorbed_id, survivor_id=survivor_id)

    tx.run(queries.MARK_PERSON_MERGED, absorbed_id=absorbed_id)
    tx.run(
        queries.CREATE_MERGED_INTO,
        absorbed_id=absorbed_id,
        survivor_id=survivor_id,
        merge_event_id=merge_event_id,
    )
    tx.run(
        queries.PATH_COMPRESS_MERGED_INTO,
        absorbed_id=absorbed_id,
        survivor_id=survivor_id,
    )

    tx.run(
        queries.CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED,
        absorbed_id=absorbed_id,
        survivor_id=survivor_id,
        merge_event_id=merge_event_id,
    )
    tx.run(
        queries.REDIRECT_PERSON_PAIR_CASES_ABSORBED_LEFT,
        absorbed_id=absorbed_id,
        survivor_id=survivor_id,
        merge_event_id=merge_event_id,
    )
    tx.run(
        queries.REDIRECT_PERSON_PAIR_CASES_ABSORBED_RIGHT,
        absorbed_id=absorbed_id,
        survivor_id=survivor_id,
        merge_event_id=merge_event_id,
    )
    tx.run(
        queries.REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
        absorbed_id=absorbed_id,
        survivor_id=survivor_id,
        merge_event_id=merge_event_id,
    )

    compute_golden_profile(tx, survivor_id)
    return merge_event_id
```

Note: `record.get(...)` above returns `object` from the driver's `Record` type in this codebase's usage elsewhere (see `pair_score.py`'s `_snapshot_identifiers`, which does the same `isinstance` narrowing rather than casting) — this keeps the function mypy-strict clean without `Any`.

- [ ] **Step 4: Commit**

```bash
git add services/ingestion/src/pipeline_person_merge.py services/ingestion/tests/test_pipeline_person_merge.py
git commit -m "feat(ingestion): add person-pair merge execution and survivor selection"
```

---

### Task 4: Wire auto-merge into the person-pair auditor

**Files:**
- Modify: `services/ingestion/src/pipeline_person_pairs.py`
- Test: `services/ingestion/tests/test_person_pair_auditing.py`

**Interfaces:**
- Consumes: `PERSON_PAIR_AUTO_MERGE` (Task 1); `fetch_pair_attrs`, `select_survivor`, `merge_person_pair` (Task 3); pre-existing `queries.CREATE_MATCH_DECISION`, `queries.LINK_MATCH_DECISION_LEFT_PERSON`, `queries.LINK_MATCH_DECISION_RIGHT_PERSON`.
- Produces: unchanged public signature `audit_person_pairs(tx, identifiers) -> list[str]` (still returns opened *review-case* ids only, for logging — merges are logged separately and are not part of this return value, so existing callers/tests are unaffected).

- [ ] **Step 1: Write the failing tests**

In `services/ingestion/tests/test_person_pair_auditing.py`, the existing `_ScriptedTx.run` must answer the new `FETCH_PAIR_MERGE_ATTRS` query for every existing test to keep passing (today none of them exercise the new branch, but the guard runs unconditionally). Modify `_ScriptedTx.__init__` to accept the new fields and its `run` to answer them:

```python
class _ScriptedTx:
    """Dispatches results by query content; records the create calls."""

    def __init__(
        self,
        *,
        fanout: int,
        person_ids: list[str],
        is_locked: bool = False,
        existing_case: str | None = None,
        idents: list[dict[str, object]] | None = None,
        facts: list[dict[str, object]] | None = None,
        addrs: list[dict[str, object]] | None = None,
        pair_attrs: dict[str, object] | None = None,
    ) -> None:
        self.fanout = fanout
        self.person_ids = person_ids
        self.is_locked = is_locked
        self.existing_case = existing_case
        self.idents = idents or []
        self.facts = facts or []
        self.addrs = addrs or []
        # Defaults both persons to active/no-completeness so the new guard
        # passes for every pre-existing test scenario unchanged.
        self.pair_attrs = pair_attrs or {
            "left_person_id": "unset-left",
            "left_status": "active",
            "left_completeness": 0.0,
            "left_created_at": "2026-01-01T00:00:00Z",
            "right_person_id": "unset-right",
            "right_status": "active",
            "right_completeness": 0.0,
            "right_created_at": "2026-01-01T00:00:00Z",
        }
        self.create_calls: list[dict[str, object]] = []
        self.match_decision_calls: list[dict[str, object]] = []
        self._created = 0

    def run(self, query: str, **params: object) -> _Result:
        if "RETURN count(DISTINCT p) AS fanout" in query:
            return _Result([{"fanout": self.fanout}])
        if "RETURN id.identifier_type AS identifier_type" in query:
            return _Result(list(self.idents))
        if "RETURN f.attribute_name AS attribute_name" in query:
            return _Result(list(self.facts))
        if "addr.address_id AS address_id" in query:
            return _Result(list(self.addrs))
        if "collect(DISTINCT p.person_id) AS person_ids" in query:
            return _Result([{"person_ids": list(self.person_ids)}])
        if "RETURN count(lock) > 0 AS is_locked" in query:
            return _Result([{"is_locked": self.is_locked}])
        if "left_status" in query and "right_status" in query:
            attrs = dict(self.pair_attrs)
            attrs["left_person_id"] = params["left_person_id"]
            attrs["right_person_id"] = params["right_person_id"]
            return _Result([attrs])
        if (
            "queue_state IN ['open', 'assigned', 'deferred']" in query
            and "review_case_id" in query
            and "CREATE" not in query
        ):
            return _Result(
                [{"review_case_id": self.existing_case}] if self.existing_case else []
            )
        if "CREATE (rc:ReviewCase" in query:
            self.create_calls.append(dict(params))
            self._created += 1
            return _Result([{"review_case_id": f"rc-{self._created}"}])
        if "RETURN md.match_decision_id AS match_decision_id" in query:
            self.match_decision_calls.append(dict(params))
            return _Result([{"match_decision_id": "md-1"}])
        return _Result([])
```

Add `import pytest` to the top import block of `test_person_pair_auditing.py`.
Then append new tests exercising the auto-merge branch (using `monkeypatch` to
isolate `merge_person_pair`, per Task 3 already covering its internals — this
avoids re-simulating the full rewire Cypher inside this file, per the repo's
DRY-check convention):

```python
def test_high_confidence_pair_auto_merges_instead_of_review_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.pipeline_person_pairs as pair_pairs_module

    merge_calls: list[dict[str, object]] = []

    def _fake_merge_person_pair(
        tx: object, *, absorbed_id: str, survivor_id: str, match_decision_id: str, reason: str
    ) -> str:
        merge_calls.append(
            {
                "absorbed_id": absorbed_id,
                "survivor_id": survivor_id,
                "match_decision_id": match_decision_id,
            }
        )
        return "me-1"

    monkeypatch.setattr(pair_pairs_module, "merge_person_pair", _fake_merge_person_pair)

    # Verified phone (+0.35) + verified email (+0.35) = 0.70 >= 0.60 threshold.
    tx = _ScriptedTx(
        fanout=2,
        person_ids=["person-a", "person-b"],
        idents=[
            {"identifier_type": "phone", "normalized_value": "+6580000000", "is_verified": True},
            {"identifier_type": "email", "normalized_value": "a@example.com", "is_verified": True},
        ],
        pair_attrs={
            "left_person_id": "person-a",
            "left_status": "active",
            "left_completeness": 0.75,
            "left_created_at": "2026-01-01T00:00:00Z",
            "right_person_id": "person-b",
            "right_status": "active",
            "right_completeness": 0.25,
            "right_created_at": "2026-01-01T00:00:00Z",
        },
    )
    created = audit_person_pairs(tx, _nric())  # type: ignore[arg-type]
    assert created == []  # no ReviewCase opened
    assert tx.create_calls == []
    assert len(merge_calls) == 1
    # person-a has higher completeness (0.75 > 0.25) -> survives.
    assert merge_calls[0]["survivor_id"] == "person-a"
    assert merge_calls[0]["absorbed_id"] == "person-b"
    assert merge_calls[0]["match_decision_id"] == "md-1"


def _fail_if_merge_called(merge_calls: list[str]) -> object:
    """Build a ``merge_person_pair`` stub that records unexpected calls."""

    def _fake(
        tx: object, *, absorbed_id: str, survivor_id: str, match_decision_id: str, reason: str
    ) -> str:
        merge_calls.append(absorbed_id)
        return "me-x"

    return _fake


def test_below_threshold_pair_still_opens_review_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.pipeline_person_pairs as pair_pairs_module

    merge_calls: list[str] = []
    monkeypatch.setattr(pair_pairs_module, "merge_person_pair", _fail_if_merge_called(merge_calls))
    # Same fixture as test_pair_case_carries_heuristic_confidence: confidence 0.55 < 0.60.
    tx = _ScriptedTx(
        fanout=2,
        person_ids=["person-a", "person-b"],
        idents=[
            {"identifier_type": "phone", "normalized_value": "+6580000000", "is_verified": True}
        ],
        facts=[{"attribute_name": "full_name", "attribute_value": "Alice Tan"}],
    )
    created = audit_person_pairs(tx, _nric())  # type: ignore[arg-type]
    assert created == ["rc-1"]
    assert merge_calls == []


def test_inactive_person_skips_pair_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.pipeline_person_pairs as pair_pairs_module

    merge_calls: list[str] = []
    monkeypatch.setattr(pair_pairs_module, "merge_person_pair", _fail_if_merge_called(merge_calls))
    tx = _ScriptedTx(
        fanout=2,
        person_ids=["person-a", "person-b"],
        idents=[
            {"identifier_type": "phone", "normalized_value": "+6580000000", "is_verified": True},
            {"identifier_type": "email", "normalized_value": "a@example.com", "is_verified": True},
        ],
        pair_attrs={
            "left_person_id": "person-a",
            "left_status": "merged",  # already absorbed earlier in this same batch
            "left_completeness": 0.75,
            "left_created_at": "2026-01-01T00:00:00Z",
            "right_person_id": "person-b",
            "right_status": "active",
            "right_completeness": 0.25,
            "right_created_at": "2026-01-01T00:00:00Z",
        },
    )
    created = audit_person_pairs(tx, _nric())  # type: ignore[arg-type]
    assert created == []
    assert tx.create_calls == []
    assert merge_calls == []
```

- [ ] **Step 2: Run tests to verify they fail**

Per Global Constraints, not run locally — proceed to the implementation.

- [ ] **Step 3: Write the implementation**

Replace `services/ingestion/src/pipeline_person_pairs.py` in full:

```python
"""Person↔person review-case detection during ingestion.

After a record is linked, any usable identifier it carries may now connect two
or more *active* persons. That shared-identifier bridge is the signal that the
persons might be duplicates. Below ``PERSON_PAIR_AUTO_MERGE`` confidence this
module opens a pairwise person↔person ReviewCase so a human can adjudicate; at
or above it, the pair is merged automatically (see
:mod:`src.pipeline_person_merge`) since no further human judgment is expected
to change the outcome.

Detection reuses the existing fanout cap (high-fanout identifiers are
non-discriminating) and the canonical pair ordering (left.person_id < right).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from neo4j import ManagedTransaction

from src.graph import queries
from src.matching.pair_score import PERSON_PAIR_AUTO_MERGE, score_person_pair
from src.models import JsonValue, NormalizedIdentifier
from src.pipeline_normalization import is_usable
from src.pipeline_person_merge import (
    PairPersonAttrs,
    fetch_pair_attrs,
    merge_person_pair,
    select_survivor,
)
from src.pipeline_writes import exceeds_fanout_cap

logger = logging.getLogger(__name__)

_ENGINE_VERSION = "v0.1.0"
_POLICY_VERSION = "v0.1.0"
_PRIORITY = 100
_SLA_DAYS = 7


@dataclass(frozen=True)
class _PairOutcome:
    kind: Literal["review_case", "merged"]
    id: str


def audit_person_pairs(
    tx: ManagedTransaction,
    identifiers: list[NormalizedIdentifier],
) -> list[str]:
    """Open person↔person review cases for shared-identifier bridges.

    Returns the list of created ``review_case_id``s (may be empty). Pairs
    that auto-merge are logged but not included in this list — callers that
    want a merge count should read the log, since none currently need the ids.
    """
    opened: list[str] = []
    merged: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()

    for ident in identifiers:
        if not is_usable(ident.quality_flag):
            continue
        if exceeds_fanout_cap(tx, ident):
            continue
        record = tx.run(
            queries.FIND_PERSONS_SHARING_IDENTIFIER,
            identifier_type=ident.identifier_type,
            normalized_value=ident.normalized_value,
        ).single()
        if record is None:
            continue
        person_ids = sorted({str(pid) for pid in record["person_ids"]})
        for i in range(len(person_ids)):
            for j in range(i + 1, len(person_ids)):
                pair = (person_ids[i], person_ids[j])  # left < right by sort
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                outcome = _process_pair(tx, pair[0], pair[1], ident)
                if outcome is None:
                    continue
                if outcome.kind == "review_case":
                    opened.append(outcome.id)
                else:
                    merged.append(outcome.id)

    if opened:
        logger.info("Opened %d person-pair review case(s): %s", len(opened), opened)
    if merged:
        logger.info(
            "Auto-merged %d person-pair(s) at >= %.2f confidence: %s",
            len(merged),
            PERSON_PAIR_AUTO_MERGE,
            merged,
        )
    return opened


def _process_pair(
    tx: ManagedTransaction,
    left_person_id: str,
    right_person_id: str,
    ident: NormalizedIdentifier,
) -> _PairOutcome | None:
    lock = tx.run(
        queries.CHECK_NO_MATCH_LOCK,
        left_person_id=left_person_id,
        right_person_id=right_person_id,
    ).single()
    if lock is not None and bool(lock["is_locked"]):
        return None

    existing = tx.run(
        queries.CHECK_OPEN_PERSON_PAIR_CASE,
        left_person_id=left_person_id,
        right_person_id=right_person_id,
    ).single()
    if existing is not None and existing.get("review_case_id") is not None:
        return None

    # Guards against a pair going stale within the same batch: if an earlier
    # pair sharing one of these two persons already auto-merged it this
    # ingest, that person is no longer 'active' and this bridge is moot.
    attrs = fetch_pair_attrs(tx, left_person_id, right_person_id)
    if attrs is None:
        return None
    left_attrs, right_attrs = attrs
    if left_attrs.status != "active" or right_attrs.status != "active":
        return None

    score = score_person_pair(tx, left_person_id, right_person_id)
    reasons = [
        f"Shared {ident.identifier_type} links 2 active persons "
        f"({left_person_id}, {right_person_id})",
        *score.reasons,
    ]
    snapshot: dict[str, JsonValue] = {
        "bridging_identifier_type": ident.identifier_type,
        "bridging_identifier_value": ident.normalized_value,
        "heuristic_band": score.decision.value,
        **score.feature_snapshot,
    }
    feature_snapshot = json.dumps(snapshot)

    if score.confidence >= PERSON_PAIR_AUTO_MERGE:
        return _auto_merge_pair(
            tx,
            left_person_id=left_person_id,
            right_person_id=right_person_id,
            left_attrs=left_attrs,
            right_attrs=right_attrs,
            confidence=score.confidence,
            reasons=reasons,
            feature_snapshot=feature_snapshot,
        )
    return _open_review_case(
        tx,
        left_person_id=left_person_id,
        right_person_id=right_person_id,
        confidence=score.confidence,
        reasons=reasons,
        feature_snapshot=feature_snapshot,
    )


def _auto_merge_pair(
    tx: ManagedTransaction,
    *,
    left_person_id: str,
    right_person_id: str,
    left_attrs: PairPersonAttrs,
    right_attrs: PairPersonAttrs,
    confidence: float,
    reasons: list[str],
    feature_snapshot: str,
) -> _PairOutcome:
    md_record = tx.run(
        queries.CREATE_MATCH_DECISION,
        engine_type="pair_audit",
        engine_version=_ENGINE_VERSION,
        decision="merge",
        confidence=confidence,
        reasons=reasons,
        blocking_conflicts=[],
        feature_snapshot=feature_snapshot,
        policy_version=_POLICY_VERSION,
    ).single()
    assert md_record is not None, "CREATE_MATCH_DECISION must return a row"
    match_decision_id = md_record["match_decision_id"]
    assert isinstance(match_decision_id, str)

    tx.run(
        queries.LINK_MATCH_DECISION_LEFT_PERSON,
        match_decision_id=match_decision_id,
        person_id=left_person_id,
    )
    tx.run(
        queries.LINK_MATCH_DECISION_RIGHT_PERSON,
        match_decision_id=match_decision_id,
        person_id=right_person_id,
    )

    survivor_id, absorbed_id = select_survivor(left_attrs, right_attrs)
    merge_event_id = merge_person_pair(
        tx,
        absorbed_id=absorbed_id,
        survivor_id=survivor_id,
        match_decision_id=match_decision_id,
        reason="; ".join(reasons),
    )
    return _PairOutcome(kind="merged", id=merge_event_id)


def _open_review_case(
    tx: ManagedTransaction,
    *,
    left_person_id: str,
    right_person_id: str,
    confidence: float,
    reasons: list[str],
    feature_snapshot: str,
) -> _PairOutcome | None:
    sla_due_at = (datetime.now(UTC) + timedelta(days=_SLA_DAYS)).isoformat()
    record = tx.run(
        queries.CREATE_PERSON_PAIR_REVIEW_CASE,
        left_person_id=left_person_id,
        right_person_id=right_person_id,
        priority=_PRIORITY,
        sla_due_at=sla_due_at,
        engine_version=_ENGINE_VERSION,
        policy_version=_POLICY_VERSION,
        confidence=confidence,
        reasons=reasons,
        feature_snapshot=feature_snapshot,
    ).single()
    if record is None:
        return None
    return _PairOutcome(kind="review_case", id=str(record["review_case_id"]))
```

- [ ] **Step 4: Commit**

```bash
git add services/ingestion/src/pipeline_person_pairs.py services/ingestion/tests/test_person_pair_auditing.py
git commit -m "feat(ingestion): auto-merge person-pairs at >= 0.60 confidence"
```

---

### Task 5: Documentation update

**Files:**
- Modify: `docs/profile-unifier-matching-spec.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: Update the "Person-Pair Auditing" section**

In `docs/profile-unifier-matching-spec.md`, find the paragraph beginning "The score is **strictly advisory**: `decision` stays `review` regardless of the band — a shared-identifier bridge never auto-merges persons..." and replace it with:

```markdown
- **Confidence (advisory triage, with an auto-merge exception)** — the case
  carries a real `confidence`, not a placeholder. It reuses the **Layer 2
  heuristic scorer** unchanged: the left person is treated as the "incoming
  record" (its golden identifiers, facts, and address) and scored against the
  right person's candidate snapshot. Below `PERSON_PAIR_AUTO_MERGE` (0.60),
  the score is advisory only — the band it would fall in (`heuristic_band`)
  and the heuristic signals/reasons are merged into the case's
  `feature_snapshot` so a reviewer can tell a same-name/same-phone duplicate
  from two distinct people who merely share a phone, but `decision` stays
  `review` regardless of the band. At or above `PERSON_PAIR_AUTO_MERGE`, the
  pair is merged automatically instead of opening a `ReviewCase`: a
  `MatchDecision` (`engine_type='pair_audit'`, `decision='merge'`) is created,
  survivor selection is deterministic (higher `profile_completeness_score` →
  earlier `created_at` → lower `person_id`), and the merge applies uniformly
  across bridging identifier types (phone, email, NRIC) — the heuristic score
  itself, not the identifier type, is the safety net against a coincidental
  shared phone. Conversation-promotion does **not** apply to pair audits (the
  scorer is called with the default `record_type`).
```

Update the earlier sentence "The decision *not* to auto-merge persons that share an identifier must not mean the possibility goes unexamined" (in the section's opening paragraph) to:

```markdown
Below a high-confidence threshold, the engine deliberately does not auto-merge
persons that merely share an identifier — that possibility must not go
unexamined, though. After a record is ingested and linked, the engine audits
whether any identifier the record carries now connects **two or more distinct
active persons**, and opens a **person↔person review case** for each bridged
pair so a human can adjudicate — unless the pair's score clears
`PERSON_PAIR_AUTO_MERGE`, in which case it merges automatically (see below).
```

- [ ] **Step 2: Commit**

```bash
git add docs/profile-unifier-matching-spec.md
git commit -m "docs(matching-spec): document person-pair auto-merge threshold"
```

---

### Task 6: Push and validate via Woodpecker

**Files:** none (CI validation only).

- [ ] **Step 1: Push the branch**

```bash
git push origin feat/sg-gov-removal-thresholds
```

(Confirm with the user before pushing — this is a shared/remote action per this repo's risk-confirmation rule, separate from the commit-confirmation rule already noted in Global Constraints.)

- [ ] **Step 2: Read the PR pipeline verdict**

```bash
wpci home pipeline last sparkfn/hyperP --branch feat/sg-gov-removal-thresholds
```

If any step fails (ruff, ruff format --check, mypy --strict, or pytest on `services/ingestion/src`/`services/ingestion/tests`), inspect it with:

```bash
wpci home pipeline log show sparkfn/hyperP <pipeline-number> <step-name>
```

Common failure classes to expect and fix per `CLAUDE.md`'s "CI lint findings" section: trailing newline (W292) on the new files, `ruff format --check` drift (run `ruff format` on just the changed files as a one-shot generate step, not a verify step, then re-push), and any `mypy --strict` narrowing issue in `fetch_pair_attrs`'s `isinstance` chain.

- [ ] **Step 3: Report status**

Do not report this work complete without pipeline evidence: repo, branch, commit SHA, pipeline number, status, and step names, per this repo's "Agent rules (PR + DEV)" policy.
