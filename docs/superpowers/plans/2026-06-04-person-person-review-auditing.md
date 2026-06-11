# Person↔Person Review Case Auditing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-open pairwise person↔person review cases during ingestion when a shared identifier links 2+ active persons, and keep those cases (and existing record↔person cases) consistent across merge and unmerge.

**Architecture:** Detection runs synchronously inside the existing ingest write transaction (a new `pipeline_person_pairs.py` module), reusing the fanout cap and shared-identifier traversal. Merge side-effects (close other person-pair cases for the absorbed person; redirect record-person cases absorbed→survivor) are centralized in one API helper called from both merge call-sites; unmerge reverts the stamped side-effects when a human hasn't acted since the merge.

**Tech Stack:** Python 3 / FastAPI / Neo4j (Cypher) / pytest. Ingestion service (`services/ingestion`), API service (`services/api`). Unit tests use scripted fake-transaction objects (no live Neo4j), matching the existing test style in `test_match_engine_multi_match.py` and `test_review_repository_merge.py`.

**Design doc:** `docs/superpowers/specs/2026-06-04-person-person-review-auditing-design.md`

---

## File Structure

**Ingestion (`services/ingestion`)**
- Create `src/graph/queries/person_pairs.py` — Cypher constants for person-pair detection.
- Modify `src/graph/queries/__init__.py` — re-export the new constants.
- Create `src/pipeline_person_pairs.py` — `audit_person_pairs()` orchestration.
- Modify `src/pipeline.py` — call `audit_person_pairs()` in `_execute_ingest`.
- Create `tests/test_person_pair_auditing.py` — detection unit tests.

**API (`services/api`)**
- Modify `src/graph/queries/merge.py` — add 4 side-effect Cypher constants.
- Modify `src/graph/queries/__init__.py` — re-export them.
- Create `src/repositories/neo4j/_merge_side_effects.py` — `apply_…` / `revert_…` helpers.
- Modify `src/repositories/neo4j/review.py` — call `apply_…` in `_action_tx` after merge.
- Modify `src/repositories/neo4j/merge.py` — call `apply_…` in `_manual_merge_tx`, `revert_…` in `_unmerge_tx`.
- Modify `tests/test_review_repository_merge.py` — update merge-call assertions for the new trailing calls.
- Create `tests/test_merge_review_side_effects.py` — side-effect helper unit tests.

---

## Phase 1 — Ingestion detection

### Task 1: Person-pair Cypher constants

**Files:**
- Create: `services/ingestion/src/graph/queries/person_pairs.py`
- Modify: `services/ingestion/src/graph/queries/__init__.py`
- Test: `services/ingestion/tests/test_person_pair_auditing.py`

- [ ] **Step 1: Write the failing test** (constants exist and are importable)

Create `services/ingestion/tests/test_person_pair_auditing.py`:

```python
"""Person↔person review-case detection during ingestion."""

from __future__ import annotations

from src.graph import queries


def test_person_pair_query_constants_exist() -> None:
    assert "ABOUT_LEFT" in queries.CREATE_PERSON_PAIR_REVIEW_CASE
    assert "ABOUT_RIGHT" in queries.CREATE_PERSON_PAIR_REVIEW_CASE
    assert "pair_audit" in queries.CREATE_PERSON_PAIR_REVIEW_CASE
    assert "queue_state IN ['open', 'assigned', 'deferred']" in queries.CHECK_OPEN_PERSON_PAIR_CASE
    assert "IDENTIFIED_BY" in queries.FIND_PERSONS_SHARING_IDENTIFIER
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_person_pair_auditing.py::test_person_pair_query_constants_exist -v`
Expected: FAIL with `AttributeError: module 'src.graph.queries' has no attribute 'CREATE_PERSON_PAIR_REVIEW_CASE'`

- [ ] **Step 3: Create the query module**

Create `services/ingestion/src/graph/queries/person_pairs.py`:

```python
"""Cypher constants for person↔person review-case detection (shared-identifier bridges)."""

from __future__ import annotations

FIND_PERSONS_SHARING_IDENTIFIER = """
MATCH (id:Identifier {identifier_type: $identifier_type, normalized_value: $normalized_value})
      <-[rel:IDENTIFIED_BY]-(p:Person {status: 'active'})
WHERE rel.is_active = true
  AND rel.quality_flag IN ['valid', 'partial_parse']
RETURN collect(DISTINCT p.person_id) AS person_ids
"""

CHECK_OPEN_PERSON_PAIR_CASE = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT {entity_type: 'person'}]->(a:Person)
MATCH (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(b:Person)
WHERE rc.queue_state IN ['open', 'assigned', 'deferred']
  AND ((a.person_id = $left_person_id AND b.person_id = $right_person_id)
    OR (a.person_id = $right_person_id AND b.person_id = $left_person_id))
RETURN rc.review_case_id AS review_case_id
LIMIT 1
"""

CREATE_PERSON_PAIR_REVIEW_CASE = """
MATCH (a:Person {person_id: $left_person_id})
MATCH (b:Person {person_id: $right_person_id})
CREATE (md:MatchDecision {
    match_decision_id: randomUUID(),
    engine_type: 'pair_audit',
    engine_version: $engine_version,
    decision: 'review',
    confidence: 0.0,
    reasons: $reasons,
    blocking_conflicts: [],
    feature_snapshot: $feature_snapshot,
    policy_version: $policy_version,
    created_at: datetime(),
    retention_expires_at: null
})
CREATE (md)-[:ABOUT_LEFT {entity_type: 'person'}]->(a)
CREATE (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(b)
CREATE (rc:ReviewCase {
    review_case_id: randomUUID(),
    priority: $priority,
    queue_state: 'open',
    assigned_to: null,
    follow_up_at: null,
    sla_due_at: datetime($sla_due_at),
    resolution: null,
    resolved_at: null,
    actions: '[]',
    created_at: datetime(),
    updated_at: datetime()
})-[:FOR_DECISION]->(md)
RETURN rc.review_case_id AS review_case_id
"""
```

- [ ] **Step 4: Re-export the constants**

In `services/ingestion/src/graph/queries/__init__.py`, add an import block (alongside the other `from src.graph.queries.X import (...)` blocks) and extend `__all__` if the file defines one:

```python
from src.graph.queries.person_pairs import (
    CHECK_OPEN_PERSON_PAIR_CASE,
    CREATE_PERSON_PAIR_REVIEW_CASE,
    FIND_PERSONS_SHARING_IDENTIFIER,
)
```

(If `__init__.py` has an `__all__` list, append the three names to it. If it does not, no further change is needed — the imports make them attributes of the module.)

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest services/ingestion/tests/test_person_pair_auditing.py::test_person_pair_query_constants_exist -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add services/ingestion/src/graph/queries/person_pairs.py services/ingestion/src/graph/queries/__init__.py services/ingestion/tests/test_person_pair_auditing.py
git commit -m "feat(matching): add person-pair detection Cypher constants"
```

---

### Task 2: `audit_person_pairs` orchestration

**Files:**
- Create: `services/ingestion/src/pipeline_person_pairs.py`
- Test: `services/ingestion/tests/test_person_pair_auditing.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `services/ingestion/tests/test_person_pair_auditing.py`:

```python
from collections.abc import Iterator

from src.models import NormalizedIdentifier, QualityFlag
from src.pipeline_person_pairs import audit_person_pairs


class _Result:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self._records = records

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._records)

    def single(self) -> dict[str, object] | None:
        return self._records[0] if self._records else None


class _ScriptedTx:
    """Dispatches results by query content; records the create calls."""

    def __init__(
        self,
        *,
        fanout: int,
        person_ids: list[str],
        is_locked: bool = False,
        existing_case: str | None = None,
    ) -> None:
        self.fanout = fanout
        self.person_ids = person_ids
        self.is_locked = is_locked
        self.existing_case = existing_case
        self.create_calls: list[dict[str, object]] = []
        self._created = 0

    def run(self, query: str, **params: object) -> _Result:
        if "RETURN count(DISTINCT p) AS fanout" in query:
            return _Result([{"fanout": self.fanout}])
        if "collect(DISTINCT p.person_id) AS person_ids" in query:
            return _Result([{"person_ids": list(self.person_ids)}])
        if "RETURN count(lock) > 0 AS is_locked" in query:
            return _Result([{"is_locked": self.is_locked}])
        if "queue_state IN ['open', 'assigned', 'deferred']" in query and "review_case_id" in query and "CREATE" not in query:
            return _Result([{"review_case_id": self.existing_case}] if self.existing_case else [])
        if "CREATE (rc:ReviewCase" in query:
            self.create_calls.append(dict(params))
            self._created += 1
            return _Result([{"review_case_id": f"rc-{self._created}"}])
        return _Result([])


def _nric() -> list[NormalizedIdentifier]:
    return [
        NormalizedIdentifier(
            identifier_type="nric",
            normalized_value="hash-shared",
            quality_flag=QualityFlag.VALID,
        )
    ]


def test_two_active_persons_open_one_ordered_pair_case() -> None:
    tx = _ScriptedTx(fanout=2, person_ids=["person-b", "person-a"])
    created = audit_person_pairs(tx, _nric())  # type: ignore[arg-type]
    assert created == ["rc-1"]
    assert len(tx.create_calls) == 1
    call = tx.create_calls[0]
    # Canonical ordering: left < right.
    assert call["left_person_id"] == "person-a"
    assert call["right_person_id"] == "person-b"
    assert "nric" in str(call["feature_snapshot"])


def test_existing_open_case_suppresses_duplicate() -> None:
    tx = _ScriptedTx(fanout=2, person_ids=["person-a", "person-b"], existing_case="rc-old")
    created = audit_person_pairs(tx, _nric())  # type: ignore[arg-type]
    assert created == []
    assert tx.create_calls == []


def test_active_no_match_lock_suppresses_case() -> None:
    tx = _ScriptedTx(fanout=2, person_ids=["person-a", "person-b"], is_locked=True)
    created = audit_person_pairs(tx, _nric())  # type: ignore[arg-type]
    assert created == []
    assert tx.create_calls == []


def test_fanout_over_cap_skips_identifier() -> None:
    # nric cap is 5; fanout 6 exceeds it.
    tx = _ScriptedTx(fanout=6, person_ids=["a", "b", "c", "d", "e", "f"])
    created = audit_person_pairs(tx, _nric())  # type: ignore[arg-type]
    assert created == []
    assert tx.create_calls == []


def test_three_persons_produce_three_pairwise_cases() -> None:
    tx = _ScriptedTx(fanout=3, person_ids=["person-c", "person-a", "person-b"])
    created = audit_person_pairs(tx, _nric())  # type: ignore[arg-type]
    assert created == ["rc-1", "rc-2", "rc-3"]
    pairs = {(c["left_person_id"], c["right_person_id"]) for c in tx.create_calls}
    assert pairs == {
        ("person-a", "person-b"),
        ("person-a", "person-c"),
        ("person-b", "person-c"),
    }


def test_unusable_identifier_skipped() -> None:
    bad = [
        NormalizedIdentifier(
            identifier_type="nric",
            normalized_value="hash-shared",
            quality_flag=QualityFlag.INVALID_FORMAT,
        )
    ]
    tx = _ScriptedTx(fanout=2, person_ids=["person-a", "person-b"])
    created = audit_person_pairs(tx, bad)  # type: ignore[arg-type]
    assert created == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest services/ingestion/tests/test_person_pair_auditing.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.pipeline_person_pairs'`

- [ ] **Step 3: Implement the module**

Create `services/ingestion/src/pipeline_person_pairs.py`:

```python
"""Person↔person review-case detection.

After a record is linked, any usable identifier it carries may now connect two
or more *active* persons. That shared-identifier bridge is the signal that the
persons might be duplicates — but the match engine deliberately does not merge
persons that merely share an identifier. This module opens a pairwise
person↔person ReviewCase for each newly-bridged pair so a human can adjudicate.

Detection reuses the existing fanout cap (high-fanout identifiers are
non-discriminating) and the canonical pair ordering (left.person_id < right).
It only creates audit cases; it never merges or links persons.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from neo4j import ManagedTransaction

from src.graph import queries
from src.models import NormalizedIdentifier
from src.pipeline_normalization import is_usable
from src.pipeline_writes import exceeds_fanout_cap

logger = logging.getLogger(__name__)

_ENGINE_VERSION = "v0.1.0"
_POLICY_VERSION = "v0.1.0"
_PRIORITY = 100
_SLA_DAYS = 7


def audit_person_pairs(
    tx: ManagedTransaction,
    identifiers: list[NormalizedIdentifier],
) -> list[str]:
    """Open person↔person review cases for shared-identifier bridges.

    Returns the list of created ``review_case_id``s (may be empty).
    """
    created: list[str] = []
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
                case_id = _create_pair_case_if_needed(tx, pair[0], pair[1], ident)
                if case_id is not None:
                    created.append(case_id)

    if created:
        logger.info("Opened %d person-pair review case(s): %s", len(created), created)
    return created


def _create_pair_case_if_needed(
    tx: ManagedTransaction,
    left_person_id: str,
    right_person_id: str,
    ident: NormalizedIdentifier,
) -> str | None:
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

    sla_due_at = (datetime.now(UTC) + timedelta(days=_SLA_DAYS)).isoformat()
    feature_snapshot = json.dumps(
        {
            "bridging_identifier_type": ident.identifier_type,
            "bridging_identifier_value": ident.normalized_value,
        }
    )
    reasons = [
        f"Shared {ident.identifier_type} links 2 active persons "
        f"({left_person_id}, {right_person_id})"
    ]
    record = tx.run(
        queries.CREATE_PERSON_PAIR_REVIEW_CASE,
        left_person_id=left_person_id,
        right_person_id=right_person_id,
        priority=_PRIORITY,
        sla_due_at=sla_due_at,
        engine_version=_ENGINE_VERSION,
        policy_version=_POLICY_VERSION,
        reasons=reasons,
        feature_snapshot=feature_snapshot,
    ).single()
    if record is None:
        return None
    return str(record["review_case_id"])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest services/ingestion/tests/test_person_pair_auditing.py -v`
Expected: PASS (all 7 tests)

- [ ] **Step 5: Lint & type-check**

Run: `uv run --package profile-unifier-ingestion ruff check services/ingestion/src/pipeline_person_pairs.py && uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src/pipeline_person_pairs.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add services/ingestion/src/pipeline_person_pairs.py services/ingestion/tests/test_person_pair_auditing.py
git commit -m "feat(matching): detect shared-identifier person bridges and open pair review cases"
```

---

### Task 3: Wire detection into the ingest transaction

**Files:**
- Modify: `services/ingestion/src/pipeline.py` (imports near line 52; call site near line 244, after the multi-match loop)

- [ ] **Step 1: Write the failing test**

Append to `services/ingestion/tests/test_person_pair_auditing.py`:

```python
def test_pipeline_imports_audit_person_pairs() -> None:
    import src.pipeline as pipeline_module

    assert hasattr(pipeline_module, "audit_person_pairs")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_person_pair_auditing.py::test_pipeline_imports_audit_person_pairs -v`
Expected: FAIL with `AssertionError`

- [ ] **Step 3: Add the import**

In `services/ingestion/src/pipeline.py`, after the existing `from src.pipeline_normalization import (...)` block (around line 47-51), add:

```python
from src.pipeline_person_pairs import audit_person_pairs
```

- [ ] **Step 4: Add the call site**

In `services/ingestion/src/pipeline.py`, in `_execute_ingest`, immediately after the multi-match `for other_person_id in match_result.additional_linked_person_ids:` loop completes (after line 244, before the `if match_result.decision == MatchDecision.MERGE and not is_new_person:` block at line 245), insert:

```python
        # Person↔person audit: any usable identifier this record carries that now
        # links 2+ active persons opens a pairwise review case (deduped, fanout-
        # capped). Audit-only — never merges or links persons.
        audit_person_pairs(tx, identifiers)
```

- [ ] **Step 5: Run test + full ingestion suite to verify pass + no regression**

Run: `uv run pytest services/ingestion/tests/test_person_pair_auditing.py services/ingestion/tests/test_review_provisional_attach.py services/ingestion/tests/test_match_engine_multi_match.py -v`
Expected: PASS

- [ ] **Step 6: Lint & type-check the pipeline**

Run: `uv run --package profile-unifier-ingestion ruff check services/ingestion/src/pipeline.py && uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src/pipeline.py`
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add services/ingestion/src/pipeline.py services/ingestion/tests/test_person_pair_auditing.py
git commit -m "feat(matching): run person-pair audit inside the ingest transaction"
```

---

## Phase 2 — API merge / unmerge side-effects

### Task 4: Side-effect Cypher constants

**Files:**
- Modify: `services/api/src/graph/queries/merge.py` (append constants)
- Modify: `services/api/src/graph/queries/__init__.py` (re-export)
- Test: `services/api/tests/test_merge_review_side_effects.py`

- [ ] **Step 1: Write the failing test**

Create `services/api/tests/test_merge_review_side_effects.py`:

```python
from __future__ import annotations

from src.graph.queries import (
    CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED,
    REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
    REVERT_PERSON_PAIR_CASE_CLOSURES,
    REVERT_RECORD_PERSON_CASE_REDIRECTS,
)


def test_close_query_targets_open_person_pair_cases() -> None:
    assert "cancelled_superseded" in CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED
    assert "closed_by_merge_event_id" in CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED
    assert "queue_state IN ['open', 'assigned', 'deferred']" in CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED


def test_redirect_query_rewires_about_right_for_record_cases() -> None:
    assert "ABOUT_RIGHT" in REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED
    assert "entity_type: 'source_record'" in REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED
    assert "redirected_by_merge_event_id" in REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED


def test_revert_queries_are_event_scoped_and_state_guarded() -> None:
    assert "redirected_by_merge_event_id = $merge_event_id" in REVERT_RECORD_PERSON_CASE_REDIRECTS
    assert "queue_state IN ['open', 'assigned', 'deferred']" in REVERT_RECORD_PERSON_CASE_REDIRECTS
    assert "closed_by_merge_event_id = $merge_event_id" in REVERT_PERSON_PAIR_CASE_CLOSURES
    assert "rc.queue_state = 'cancelled'" in REVERT_PERSON_PAIR_CASE_CLOSURES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/api/tests/test_merge_review_side_effects.py::test_close_query_targets_open_person_pair_cases -v`
Expected: FAIL with `ImportError: cannot import name 'CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED'`

- [ ] **Step 3: Append the constants**

Append to `services/api/src/graph/queries/merge.py`:

```python
CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT {entity_type: 'person'}]->(a:Person)
MATCH (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(b:Person)
WHERE rc.queue_state IN ['open', 'assigned', 'deferred']
  AND (a.person_id = $absorbed_id OR b.person_id = $absorbed_id)
SET rc.queue_state = 'cancelled',
    rc.resolution = 'cancelled_superseded',
    rc.resolved_at = datetime(),
    rc.closed_by_merge_event_id = $merge_event_id,
    rc.updated_at = datetime()
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

REVERT_RECORD_PERSON_CASE_REDIRECTS = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
WHERE rc.redirected_by_merge_event_id = $merge_event_id
  AND rc.queue_state IN ['open', 'assigned', 'deferred']
MATCH (md)-[cur_right:ABOUT_RIGHT {entity_type: 'person'}]->(:Person)
MATCH (absorbed:Person {person_id: rc.redirected_from_person_id})
CREATE (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(absorbed)
DELETE cur_right
SET rc.redirected_by_merge_event_id = null,
    rc.redirected_from_person_id = null,
    rc.updated_at = datetime()
"""

REVERT_PERSON_PAIR_CASE_CLOSURES = """
MATCH (rc:ReviewCase)
WHERE rc.closed_by_merge_event_id = $merge_event_id
  AND rc.queue_state = 'cancelled'
  AND rc.resolution = 'cancelled_superseded'
SET rc.queue_state = 'open',
    rc.resolution = null,
    rc.resolved_at = null,
    rc.closed_by_merge_event_id = null,
    rc.updated_at = datetime()
"""
```

- [ ] **Step 4: Re-export from `__init__.py`**

In `services/api/src/graph/queries/__init__.py`, find the existing `from src.graph.queries.merge import (...)` block and add the four names to it (keep alphabetical/existing order), and add them to `__all__` if present:

```python
    CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED,
    REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
    REVERT_PERSON_PAIR_CASE_CLOSURES,
    REVERT_RECORD_PERSON_CASE_REDIRECTS,
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest services/api/tests/test_merge_review_side_effects.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add services/api/src/graph/queries/merge.py services/api/src/graph/queries/__init__.py services/api/tests/test_merge_review_side_effects.py
git commit -m "feat(review): add merge/unmerge review-case side-effect Cypher"
```

---

### Task 5: Side-effect helper module

**Files:**
- Create: `services/api/src/repositories/neo4j/_merge_side_effects.py`
- Test: `services/api/tests/test_merge_review_side_effects.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `services/api/tests/test_merge_review_side_effects.py`:

```python
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

import pytest
from neo4j import AsyncManagedTransaction
from src.repositories.neo4j._merge_side_effects import (
    apply_merge_review_side_effects,
    revert_merge_review_side_effects,
)

type ParamValue = str | None
type Params = Mapping[str, ParamValue]


@dataclass(frozen=True)
class _Call:
    query: str
    params: Params


class _AsyncResult:
    async def single(self) -> None:
        return None


class _RecordingTx:
    def __init__(self) -> None:
        self.calls: list[_Call] = []

    async def run(self, query: str, **params: ParamValue) -> _AsyncResult:
        self.calls.append(_Call(query=query, params=params))
        return _AsyncResult()


@pytest.mark.asyncio
async def test_apply_closes_then_redirects_with_event_stamp() -> None:
    tx = _RecordingTx()
    await apply_merge_review_side_effects(
        cast(AsyncManagedTransaction, tx), "merge-1", "person-a", "person-b"
    )
    assert [c.query for c in tx.calls] == [
        CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED,
        REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
    ]
    assert tx.calls[0].params == {"absorbed_id": "person-a", "merge_event_id": "merge-1"}
    assert tx.calls[1].params == {
        "absorbed_id": "person-a",
        "survivor_id": "person-b",
        "merge_event_id": "merge-1",
    }


@pytest.mark.asyncio
async def test_revert_reverts_redirects_then_closures() -> None:
    tx = _RecordingTx()
    await revert_merge_review_side_effects(cast(AsyncManagedTransaction, tx), "merge-1")
    assert [c.query for c in tx.calls] == [
        REVERT_RECORD_PERSON_CASE_REDIRECTS,
        REVERT_PERSON_PAIR_CASE_CLOSURES,
    ]
    assert all(c.params == {"merge_event_id": "merge-1"} for c in tx.calls)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest services/api/tests/test_merge_review_side_effects.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.repositories.neo4j._merge_side_effects'`

- [ ] **Step 3: Implement the helper**

Create `services/api/src/repositories/neo4j/_merge_side_effects.py`:

```python
"""Review-case side-effects of a person merge, and their unmerge reversal.

A person merge must keep the review queue consistent:
- other open person↔person cases referencing the absorbed person are closed
  (the absorbed person no longer exists as a distinct reviewable person);
- open record↔person cases pointing at the absorbed person are redirected to
  the survivor.

Both mutations are stamped with the ``merge_event_id`` so an unmerge can revert
exactly the cases this merge changed — but only where a human has not acted on
the case since the merge (the revert queries are state-guarded).

Centralized here because two paths execute merges: the review-merge action
(``review.py:_action_tx``) and the direct admin merge (``merge.py:_manual_merge_tx``).
"""

from __future__ import annotations

from neo4j import AsyncManagedTransaction

from src.graph.queries import (
    CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED,
    REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
    REVERT_PERSON_PAIR_CASE_CLOSURES,
    REVERT_RECORD_PERSON_CASE_REDIRECTS,
)


async def apply_merge_review_side_effects(
    tx: AsyncManagedTransaction,
    merge_event_id: str,
    absorbed_id: str,
    survivor_id: str,
) -> None:
    """Close/redirect review cases affected by a merge; runs in the merge tx."""
    await tx.run(
        CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED,
        absorbed_id=absorbed_id,
        merge_event_id=merge_event_id,
    )
    await tx.run(
        REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
        absorbed_id=absorbed_id,
        survivor_id=survivor_id,
        merge_event_id=merge_event_id,
    )


async def revert_merge_review_side_effects(
    tx: AsyncManagedTransaction,
    merge_event_id: str,
) -> None:
    """Revert merge side-effects on unmerge for cases untouched since the merge."""
    await tx.run(REVERT_RECORD_PERSON_CASE_REDIRECTS, merge_event_id=merge_event_id)
    await tx.run(REVERT_PERSON_PAIR_CASE_CLOSURES, merge_event_id=merge_event_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest services/api/tests/test_merge_review_side_effects.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Lint & type-check**

Run: `uv run --package profile-unifier-api ruff check services/api/src/repositories/neo4j/_merge_side_effects.py && uv run --package profile-unifier-api mypy --strict services/api/src/repositories/neo4j/_merge_side_effects.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add services/api/src/repositories/neo4j/_merge_side_effects.py services/api/tests/test_merge_review_side_effects.py
git commit -m "feat(review): add merge side-effect apply/revert helpers"
```

---

### Task 6: Wire `apply_…` into both merge paths

**Files:**
- Modify: `services/api/src/repositories/neo4j/review.py` (`_action_tx`, merge branch near line 202-209)
- Modify: `services/api/src/repositories/neo4j/merge.py` (`_manual_merge_tx`, near line 100-110)
- Modify: `services/api/tests/test_review_repository_merge.py` (update trailing-call assertions)

- [ ] **Step 1: Update the existing review-merge test expectations (will fail)**

In `services/api/tests/test_review_repository_merge.py`:

In `test_review_merge_uses_requested_survivor_person`, replace the final two assertions:

```python
    assert tx.calls[-1].query == EXECUTE_MANUAL_MERGE
    assert tx.calls[-1].params == {
        "from_id": "person-a",
        "to_id": "person-b",
        "reason": "same person",
        "actor_id": "reviewer@example.com",
    }
```

with:

```python
    merge_call = next(c for c in tx.calls if c.query == EXECUTE_MANUAL_MERGE)
    assert merge_call.params == {
        "from_id": "person-a",
        "to_id": "person-b",
        "reason": "same person",
        "actor_id": "reviewer@example.com",
    }
    # Merge side-effects run after the merge, scoped to the merge event.
    assert [c.query for c in tx.calls[-2:]] == [
        CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED,
        REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
    ]
    assert tx.calls[-1].params == {
        "absorbed_id": "person-a",
        "survivor_id": "person-b",
        "merge_event_id": "merge-1",
    }
```

Add the imports at the top of the test file (extend the existing `from src.graph.queries import (...)` block):

```python
    CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED,
    REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest services/api/tests/test_review_repository_merge.py::test_review_merge_uses_requested_survivor_person -v`
Expected: FAIL — side-effect calls not yet emitted (`tx.calls[-2:]` are not the side-effect queries)

- [ ] **Step 3: Wire into `_action_tx`**

In `services/api/src/repositories/neo4j/review.py`:

Add to the imports from `src.repositories.neo4j` (near the existing `from src.repositories.neo4j.merge import (...)` block, around line 22):

```python
from src.repositories.neo4j._merge_side_effects import apply_merge_review_side_effects
```

Replace the merge branch (lines 202-211, the `elif action_type == ApiReviewActionType.MERGE.value and absorbed_id and survivor_id:` block):

```python
    elif action_type == ApiReviewActionType.MERGE.value and absorbed_id and survivor_id:
        merge_result = await tx.run(
            EXECUTE_MANUAL_MERGE,
            from_id=absorbed_id,
            to_id=survivor_id,
            reason=notes or "Review merge",
            actor_id=actor_id,
        )
        merge_record = await merge_result.single()
        merge_event_id = to_str(merge_record["merge_event_id"]) if merge_record else ""
        if merge_event_id:
            await apply_merge_review_side_effects(
                tx, merge_event_id, absorbed_id, survivor_id
            )
        out["survivor_person_id"] = survivor_id
        out["golden_profile_selections"] = golden_profile_selections
```

- [ ] **Step 4: Wire into `_manual_merge_tx`**

In `services/api/src/repositories/neo4j/merge.py`:

Add the import near the top (with the other `from src.repositories...` / local imports):

```python
from src.repositories.neo4j._merge_side_effects import apply_merge_review_side_effects
```

In `_manual_merge_tx`, replace the tail of the function (lines 107-110) — currently:

```python
    record = await merge_result.single()
    if record is None:
        return MergeOutcome(not_found=True)
    return MergeOutcome(merge_event_id=to_str(record["merge_event_id"]))
```

with:

```python
    record = await merge_result.single()
    if record is None:
        return MergeOutcome(not_found=True)
    merge_event_id = to_str(record["merge_event_id"])
    await apply_merge_review_side_effects(tx, merge_event_id, from_id, to_id)
    return MergeOutcome(merge_event_id=merge_event_id)
```

(In `_manual_merge_tx`, `from_id` is the absorbed person and `to_id` the survivor — matching `EXECUTE_MANUAL_MERGE`'s `absorbed={from_id}`, `survivor={to_id}`.)

- [ ] **Step 5: Run the API merge tests to verify pass + no regression**

Run: `uv run pytest services/api/tests/test_review_repository_merge.py services/api/tests/test_merge_review_side_effects.py -v`
Expected: PASS

> Note: the existing `test_review_merge_uses_requested_survivor_person` scripts a 5th record `{"merge_event_id": "merge-1"}` returned by `EXECUTE_MANUAL_MERGE`; the new `await merge_result.single()` consumes it, and the two side-effect calls return `None` (the script has no more records, which `_Tx.run` already handles by returning `_AsyncResult(None)`). No additional scripted records are required.

- [ ] **Step 6: Lint & type-check**

Run: `uv run --package profile-unifier-api ruff check services/api/src/repositories/neo4j/review.py services/api/src/repositories/neo4j/merge.py && uv run --package profile-unifier-api mypy --strict services/api/src/repositories/neo4j/review.py services/api/src/repositories/neo4j/merge.py`
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add services/api/src/repositories/neo4j/review.py services/api/src/repositories/neo4j/merge.py services/api/tests/test_review_repository_merge.py
git commit -m "feat(review): apply review-case side-effects on review and admin merges"
```

---

### Task 7: Wire `revert_…` into unmerge

**Files:**
- Modify: `services/api/src/repositories/neo4j/merge.py` (`_unmerge_tx`, near line 195)
- Test: `services/api/tests/test_merge_review_side_effects.py` (extend with an unmerge-wiring test)

- [ ] **Step 1: Write the failing test**

Append to `services/api/tests/test_merge_review_side_effects.py`:

```python
from src.graph.queries import (
    CREATE_UNMERGE_AUDIT,
    FLAG_AFFECTED_RECORDS_FOR_REVIEW,
    GET_UNMERGE_TARGET,
    REVERT_MERGE,
)
from src.repositories.neo4j.merge import _unmerge_tx


@pytest.mark.asyncio
async def test_unmerge_reverts_review_side_effects() -> None:
    class _ScriptedResult:
        def __init__(self, record: Mapping[str, object] | None) -> None:
            self._record = record

        async def single(self) -> Mapping[str, object] | None:
            return self._record

    class _ScriptedTx:
        def __init__(self, records: Sequence[Mapping[str, object] | None]) -> None:
            self._records = list(records)
            self.queries: list[str] = []

        async def run(self, query: str, **params: object) -> _ScriptedResult:
            self.queries.append(query)
            record = self._records.pop(0) if self._records else None
            return _ScriptedResult(record)

    tx = _ScriptedTx(
        [
            {"absorbed_id": "person-a", "survivor_id": "person-b"},  # GET_UNMERGE_TARGET
            {"removed_count": 1, "current_survivor_id": "person-b"},  # REVERT_MERGE
            None,  # CREATE_UNMERGE_AUDIT
            None,  # FLAG_AFFECTED_RECORDS_FOR_REVIEW
            None,  # REVERT_RECORD_PERSON_CASE_REDIRECTS
            None,  # REVERT_PERSON_PAIR_CASE_CLOSURES
        ]
    )

    result = await _unmerge_tx(
        cast(AsyncManagedTransaction, tx), "merge-1", "oops", "admin@example.com"
    )

    assert result == ("person-a", "person-b")
    assert REVERT_RECORD_PERSON_CASE_REDIRECTS in tx.queries
    assert REVERT_PERSON_PAIR_CASE_CLOSURES in tx.queries
    # Revert runs after the graph unmerge is reverted.
    assert tx.queries.index(REVERT_MERGE) < tx.queries.index(REVERT_RECORD_PERSON_CASE_REDIRECTS)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest services/api/tests/test_merge_review_side_effects.py::test_unmerge_reverts_review_side_effects -v`
Expected: FAIL — `REVERT_RECORD_PERSON_CASE_REDIRECTS` not in `tx.queries`

- [ ] **Step 3: Wire into `_unmerge_tx`**

In `services/api/src/repositories/neo4j/merge.py`, add the import (alongside the `apply_…` import added in Task 6):

```python
from src.repositories.neo4j._merge_side_effects import (
    apply_merge_review_side_effects,
    revert_merge_review_side_effects,
)
```

(Replace the single-name import line from Task 6 with this two-name form.)

In `_unmerge_tx`, after the existing `FLAG_AFFECTED_RECORDS_FOR_REVIEW` call (line 195) and before `return absorbed_id, current_survivor_id` (line 196), insert:

```python
    await revert_merge_review_side_effects(tx, merge_event_id)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest services/api/tests/test_merge_review_side_effects.py -v`
Expected: PASS

- [ ] **Step 5: Lint & type-check**

Run: `uv run --package profile-unifier-api ruff check services/api/src/repositories/neo4j/merge.py && uv run --package profile-unifier-api mypy --strict services/api/src/repositories/neo4j/merge.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add services/api/src/repositories/neo4j/merge.py services/api/tests/test_merge_review_side_effects.py
git commit -m "feat(review): revert review-case side-effects on unmerge"
```

---

### Task 8: Full regression + manual smoke

**Files:** none (verification only)

- [ ] **Step 1: Run both service test suites**

Run: `uv run pytest services/ingestion/tests services/api/tests -q`
Expected: PASS (no regressions)

- [ ] **Step 2: Lint & type-check both services (changed source)**

Run:
```bash
uv run --package profile-unifier-ingestion ruff check services/ingestion/src && uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src
uv run --package profile-unifier-api ruff check services/api/src && uv run --package profile-unifier-api mypy --strict services/api/src
```
Expected: no new errors (pre-existing `types_sales.py` / `types_requests.py` `Any` findings are known and unrelated).

- [ ] **Step 3: Manual smoke (optional, requires Docker + Neo4j)**

```bash
docker compose build --no-cache api worker
docker compose up -d api worker neo4j redis
```
Re-ingest two source records that share one identifier (e.g. the same NRIC hash) but resolve to two distinct persons (e.g. conflicting names that block auto-merge). In the Neo4j browser (`:7474`), confirm a `ReviewCase` exists with `engine_type:'pair_audit'` linking the two persons:

```cypher
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision {engine_type:'pair_audit'})
MATCH (md)-[:ABOUT_LEFT {entity_type:'person'}]->(a:Person)
MATCH (md)-[:ABOUT_RIGHT {entity_type:'person'}]->(b:Person)
RETURN rc.review_case_id, a.person_id, b.person_id, md.reasons
```

- [ ] **Step 4: Commit (if any smoke fixups were needed)**

```bash
git add -A
git commit -m "test: person-pair auditing regression pass"
```

---

## Self-Review (completed during planning)

**Spec coverage:**
- D1 trigger → Task 2 (`audit_person_pairs` loop, fanout + usable guards).
- D2 pairwise → Task 2 (`C(n,2)` pair loop) + Task 1 (`CREATE_PERSON_PAIR_REVIEW_CASE`).
- D3 canonical ordering → Task 2 (`sorted(...)`, left<right) verified in `test_two_active_persons_open_one_ordered_pair_case`.
- D4 dedup on open case → Task 1 (`CHECK_OPEN_PERSON_PAIR_CASE`) + Task 2 test.
- D5 active/self-pair guard → `Person {status:'active'}` in `FIND_PERSONS_SHARING_IDENTIFIER`; self-pairs impossible from a set of distinct ids.
- D6 NO_MATCH_LOCK guard → Task 2 (`CHECK_NO_MATCH_LOCK`) + test.
- D7 fanout cap → Task 2 (`exceeds_fanout_cap`) + test.
- D8 in-transaction → Task 3.
- D9 both merge paths → Task 6.
- D10 close + redirect → Tasks 4-6.
- D11 unmerge revert, state-guarded → Tasks 4, 5, 7.

**Placeholder scan:** none — every step has concrete code/commands.

**Type/name consistency:** query constant names, helper signatures (`audit_person_pairs(tx, identifiers)`, `apply_merge_review_side_effects(tx, merge_event_id, absorbed_id, survivor_id)`, `revert_merge_review_side_effects(tx, merge_event_id)`), and param names (`left_person_id`/`right_person_id`, `absorbed_id`/`survivor_id`/`merge_event_id`) are consistent across tasks and match existing call-site conventions (`CHECK_NO_MATCH_LOCK` uses `$left_person_id`/`$right_person_id`).

**Known edge cases (deferred, documented in spec):** redirect collision producing duplicate record→survivor cases; priority/SLA tuning; optional `IngestResult.person_pair_cases_created` telemetry.
