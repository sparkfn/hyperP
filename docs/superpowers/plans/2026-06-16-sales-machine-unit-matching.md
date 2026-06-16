# Sales ↔ Person Matching via Shared Machine Units — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `pending_customer` sales SourceRecords a review-only candidate-generation path via shared `MachineUnit` evidence (`BOUGHT_UNIT`/`OWNS_UNIT`), wiring a `MatchDecision` + `ReviewCase` for human resolution and handling the approve/reject side-effects in the API review repository.

**Architecture:** New ingestion module `matching/machine_unit_heuristic.py` scores and selects the best candidate; `pipeline_sales.py` orchestrates the end-of-run single-pass using `persist_match_decision`/`create_review_case_if_needed` already in `pipeline_writes.py`. On the API side, `_action_tx` in `repositories/neo4j/review.py` gains a fallback sales-link branch (called when `GET_PERSONS_FOR_REVIEW_MERGE` returns nothing), plus unconditional `MARK_REVIEW_SALES_RECORD_UNRESOLVED` for reject/manual_no_match. `GET_REVIEW_CASE` gains a WITH+collect enrichment that resolves the Order and MachineUnit(s) for sales-typed left entities and surfaces them as `sales_summary` on `PersonComparisonEntity`.

**Tech Stack:** Python 3.13, Neo4j 5.x (Cypher), FastAPI, mypy --strict, ruff, pytest + pytest-asyncio, no new dependencies.

> **DO NOT COMMIT** any changes made by this plan without an explicit instruction from the user.

---

## File Map

### New files
- `services/ingestion/src/matching/machine_unit_heuristic.py` — scoring module (constants, `MachineUnitCandidate`, `select_best_machine_unit_candidate`, `build_machine_unit_match_result`)
- `services/ingestion/tests/test_machine_unit_heuristic.py` — unit tests for the above
- `services/ingestion/tests/test_sales_machine_unit_matching.py` — integration tests for `_propose_one_pending_sale` + `propose_machine_unit_matches_for_pending_sales`
- `services/api/tests/test_review_mappers.py` — tests for `_map_sales_summary` and updated `map_review_case_detail`

### Modified files
- `services/ingestion/src/graph/queries/sales.py` — add `FIND_MACHINE_UNIT_CANDIDATES_FOR_SALES`, `MARK_SALES_RECORD_PENDING_REVIEW`
- `services/ingestion/src/graph/queries/__init__.py` — export the two new queries
- `services/ingestion/src/pipeline_sales.py` — add `_propose_one_pending_sale`, `propose_machine_unit_matches_for_pending_sales`
- `services/ingestion/src/main.py` — wire `propose_machine_unit_matches_for_pending_sales` into the end-of-run sequence
- `services/api/src/graph/queries/review.py` — add 4 new queries, enrich `GET_REVIEW_CASE` with WITH+collect for sales
- `services/api/src/graph/queries/__init__.py` — export the 4 new review queries
- `services/api/src/types.py` — add `SalesUnitSummary`, `SalesOrderSummary`, `sales_summary` field on `PersonComparisonEntity`
- `services/api/src/graph/mappers.py` — add `_map_sales_summary`, extend `_map_comparison_entity` + `_map_source_record_comparison` signatures, update `map_review_case_detail`
- `services/api/src/repositories/neo4j/review.py` — add `_sales_link_merge_tx`, patch `_action_tx` (merge fallback + reject/manual_no_match `MARK_REVIEW_SALES_RECORD_UNRESOLVED`)
- `services/api/tests/test_review_repository_merge.py` — update `test_manual_no_match_creates_review_lock_after_action`, add 3 new tests

---

## Task 1: Heuristic scoring module

**Files:**
- Create: `services/ingestion/src/matching/machine_unit_heuristic.py`
- Create: `services/ingestion/tests/test_machine_unit_heuristic.py`

- [ ] **Step 1: Write the failing test file**

```python
# services/ingestion/tests/test_machine_unit_heuristic.py
from __future__ import annotations

from src.matching.machine_unit_heuristic import (
    MACHINE_UNIT_BOUGHT_CONFIDENCE,
    MACHINE_UNIT_OWNS_CONFIDENCE,
    MachineUnitCandidate,
    build_machine_unit_match_result,
    select_best_machine_unit_candidate,
)
from src.models import EngineType, MatchDecision


def _c(**overrides: object) -> MachineUnitCandidate:
    base: dict[str, object] = {
        "person_id": "person-1",
        "machine_unit_id": "unit-1",
        "rel_type": "OWNS_UNIT",
        "is_active": True,
        "conflict_flag": False,
        "last_confirmed_at": "2026-06-01T00:00:00+00:00",
    }
    base.update(overrides)
    return MachineUnitCandidate(**base)  # type: ignore[arg-type]


def test_select_best_returns_none_for_empty_list() -> None:
    assert select_best_machine_unit_candidate([]) is None


def test_select_best_prefers_active_owns_no_conflict() -> None:
    owns = _c(person_id="p-owns", rel_type="OWNS_UNIT", is_active=True, conflict_flag=False)
    bought = _c(person_id="p-bought", rel_type="BOUGHT_UNIT", is_active=False, conflict_flag=False)
    assert select_best_machine_unit_candidate([bought, owns]) == owns


def test_select_best_conflicted_owns_treated_as_lower_tier() -> None:
    conflicted_owns = _c(
        person_id="p-conflict",
        rel_type="OWNS_UNIT",
        is_active=True,
        conflict_flag=True,
        last_confirmed_at="2026-06-10T00:00:00+00:00",
    )
    bought = _c(
        person_id="p-bought",
        rel_type="BOUGHT_UNIT",
        is_active=False,
        conflict_flag=False,
        last_confirmed_at="2026-06-01T00:00:00+00:00",
    )
    # Both are tier 0 (non-best-tier); most-recent last_confirmed_at wins.
    assert select_best_machine_unit_candidate([bought, conflicted_owns]) == conflicted_owns


def test_select_best_tie_breaks_by_most_recent_last_confirmed_at() -> None:
    older = _c(person_id="p-older", last_confirmed_at="2026-05-01T00:00:00+00:00")
    newer = _c(person_id="p-newer", last_confirmed_at="2026-06-10T00:00:00+00:00")
    assert select_best_machine_unit_candidate([older, newer]) == newer


def test_select_best_final_tie_break_by_person_id() -> None:
    a = _c(person_id="person-aaa", last_confirmed_at="2026-06-01T00:00:00+00:00")
    b = _c(person_id="person-bbb", last_confirmed_at="2026-06-01T00:00:00+00:00")
    assert select_best_machine_unit_candidate([b, a]) == a


def test_select_best_none_last_confirmed_at_sorts_last() -> None:
    has_date = _c(person_id="p-date", last_confirmed_at="2026-01-01T00:00:00+00:00")
    no_date = _c(person_id="p-none", last_confirmed_at=None)
    # Both tier-1; "" < any ISO date so no_date sorts before has_date in score.
    # has_date has higher score → wins.
    assert select_best_machine_unit_candidate([has_date, no_date]) == has_date


def test_build_match_result_owns_unit_active_no_conflict() -> None:
    candidate = _c(person_id="person-1", machine_unit_id="unit-1")

    result = build_machine_unit_match_result(candidate)

    assert result.decision == MatchDecision.REVIEW
    assert result.confidence == MACHINE_UNIT_OWNS_CONFIDENCE
    assert result.engine_type == EngineType.HEURISTIC
    assert result.matched_person_id == "person-1"
    assert result.reasons == [
        "same_machine_unit_owner_claim (OWNS_UNIT, person person-1, unit unit-1)"
    ]
    assert result.feature_snapshot == {
        "candidate_person_id": "person-1",
        "machine_unit_id": "unit-1",
        "rel_type": "OWNS_UNIT",
        "conflict_flag": False,
        "signal_source": "machine_unit",
    }


def test_build_match_result_bought_unit() -> None:
    candidate = _c(
        person_id="person-2",
        machine_unit_id="unit-2",
        rel_type="BOUGHT_UNIT",
        is_active=False,
    )
    result = build_machine_unit_match_result(candidate)
    assert result.confidence == MACHINE_UNIT_BOUGHT_CONFIDENCE
    assert result.reasons == [
        "same_machine_unit_purchase (BOUGHT_UNIT, person person-2, unit unit-2)"
    ]


def test_build_match_result_appends_conflict_note() -> None:
    candidate = _c(
        person_id="person-3",
        machine_unit_id="unit-3",
        rel_type="BOUGHT_UNIT",
        is_active=False,
        conflict_flag=True,
    )
    result = build_machine_unit_match_result(candidate)
    assert result.reasons == [
        "same_machine_unit_purchase (BOUGHT_UNIT, person person-3, unit unit-3)"
        "; unit has conflicting ownership claims"
    ]
    assert result.feature_snapshot["conflict_flag"] is True


def test_confidence_constants_inside_review_band() -> None:
    # Both constants must satisfy: CONFIDENCE_REVIEW (0.60) ≤ x < CONFIDENCE_AUTO_MERGE (0.90)
    assert 0.60 <= MACHINE_UNIT_BOUGHT_CONFIDENCE < MACHINE_UNIT_OWNS_CONFIDENCE < 0.90
```

- [ ] **Step 2: Run to confirm ImportError**

```bash
uv run pytest services/ingestion/tests/test_machine_unit_heuristic.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'src.matching.machine_unit_heuristic'`

- [ ] **Step 3: Implement the module**

```python
# services/ingestion/src/matching/machine_unit_heuristic.py
"""Heuristic scoring for sales-record↔person candidate matching via shared MachineUnit evidence.

Both confidence constants sit inside [CONFIDENCE_REVIEW, CONFIDENCE_AUTO_MERGE) = [0.60, 0.90),
so this engine can never produce an auto-merge decision.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.models import EngineType, JsonValue, MatchDecision, MatchResult

#: OWNS_UNIT, is_active=True, conflict_flag=False — strongest machine-unit signal.
MACHINE_UNIT_OWNS_CONFIDENCE: float = 0.65
#: BOUGHT_UNIT-only, or any conflict-flagged unit.
MACHINE_UNIT_BOUGHT_CONFIDENCE: float = 0.60


@dataclass(frozen=True)
class MachineUnitCandidate:
    """A Person who shares a MachineUnit with a pending-customer sales SourceRecord."""

    person_id: str
    machine_unit_id: str
    rel_type: str
    is_active: bool
    conflict_flag: bool
    last_confirmed_at: str | None


def _is_best_tier(c: MachineUnitCandidate) -> bool:
    return c.rel_type == "OWNS_UNIT" and c.is_active and not c.conflict_flag


def select_best_machine_unit_candidate(
    candidates: list[MachineUnitCandidate],
) -> MachineUnitCandidate | None:
    """Select the single best candidate per the ranking in the design spec.

    1. OWNS_UNIT (active, no conflict) beats everything else.
    2. Tie-break by most-recent last_confirmed_at (ISO string comparison).
    3. Final tie-break by smallest person_id (deterministic).
    """
    if not candidates:
        return None

    def _score(c: MachineUnitCandidate) -> tuple[int, str]:
        return (1 if _is_best_tier(c) else 0, c.last_confirmed_at or "")

    best_score = max(_score(c) for c in candidates)
    tied = [c for c in candidates if _score(c) == best_score]
    return min(tied, key=lambda c: c.person_id)


def build_machine_unit_match_result(candidate: MachineUnitCandidate) -> MatchResult:
    """Build a REVIEW-band MatchResult for the selected candidate."""
    if _is_best_tier(candidate):
        confidence = MACHINE_UNIT_OWNS_CONFIDENCE
        reason = (
            f"same_machine_unit_owner_claim (OWNS_UNIT, person {candidate.person_id},"
            f" unit {candidate.machine_unit_id})"
        )
    else:
        confidence = MACHINE_UNIT_BOUGHT_CONFIDENCE
        reason = (
            f"same_machine_unit_purchase ({candidate.rel_type},"
            f" person {candidate.person_id}, unit {candidate.machine_unit_id})"
        )
    if candidate.conflict_flag:
        reason += "; unit has conflicting ownership claims"

    feature_snapshot: dict[str, JsonValue] = {
        "candidate_person_id": candidate.person_id,
        "machine_unit_id": candidate.machine_unit_id,
        "rel_type": candidate.rel_type,
        "conflict_flag": candidate.conflict_flag,
        "signal_source": "machine_unit",
    }
    return MatchResult(
        decision=MatchDecision.REVIEW,
        confidence=confidence,
        reasons=[reason],
        engine_type=EngineType.HEURISTIC,
        matched_person_id=candidate.person_id,
        feature_snapshot=feature_snapshot,
    )
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
uv run pytest services/ingestion/tests/test_machine_unit_heuristic.py -v
```

Expected: all 10 tests PASS.

- [ ] **Step 5: Type-check and lint the new file**

```bash
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src/matching/machine_unit_heuristic.py
uv run --package profile-unifier-ingestion ruff check services/ingestion/src/matching/machine_unit_heuristic.py
```

Both must exit 0.

---

## Task 2: Ingestion Cypher queries

**Files:**
- Modify: `services/ingestion/src/graph/queries/sales.py`
- Modify: `services/ingestion/src/graph/queries/__init__.py`

- [ ] **Step 1: Add two new query constants to `sales.py`**

Append the following block at the end of `services/ingestion/src/graph/queries/sales.py` (after `REWIRE_PURCHASED`):

```python
#: Find active Person candidates who share a MachineUnit with a pending-customer
#: sales SourceRecord (via the INVOLVES_UNIT edges already written by
#: _write_machine_unit_observations). Used by propose_machine_unit_matches_for_pending_sales.
FIND_MACHINE_UNIT_CANDIDATES_FOR_SALES = """
MATCH (sr:SourceRecord {source_record_pk: $sales_source_record_pk, link_status: 'pending_customer'})
MATCH (o:Order)-[unit_rel:INVOLVES_UNIT {source_record_pk: $sales_source_record_pk}]->(u:MachineUnit)
MATCH (u)<-[rel:BOUGHT_UNIT|OWNS_UNIT]-(p:Person {status: 'active'})
RETURN p.person_id AS person_id, u.machine_unit_id AS machine_unit_id,
       type(rel) AS rel_type, rel.is_active AS is_active,
       u.conflict_flag AS conflict_flag, rel.last_confirmed_at AS last_confirmed_at
"""

#: Transition a pending-customer sales SourceRecord to pending_review once a
#: machine-unit-based MatchDecision + ReviewCase have been created for it.
MARK_SALES_RECORD_PENDING_REVIEW = """
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
SET sr.link_status = 'pending_review',
    sr.updated_at  = datetime()
"""
```

- [ ] **Step 2: Export the two new constants from `__init__.py`**

In `services/ingestion/src/graph/queries/__init__.py`, add `FIND_MACHINE_UNIT_CANDIDATES_FOR_SALES` and `MARK_SALES_RECORD_PENDING_REVIEW` to the `from src.graph.queries.sales import (...)` block and the `__all__` list.

In the import block (after `CLEAR_SUPERSEDED_SALES_LINKS`, before `FIND_PENDING_CUSTOMER_SALES`; and `MARK_SALES_RECORD_PENDING_REVIEW` after `MARK_SALES_RECORD_LINKED`):

```python
# In the sales import block — add two lines:
from src.graph.queries.sales import (
    CLEAR_SUPERSEDED_SALES_LINKS,
    FIND_MACHINE_UNIT_CANDIDATES_FOR_SALES,   # ← new
    FIND_PENDING_CUSTOMER_SALES,
    LINK_PERSON_PURCHASED_ORDER,
    LINK_PRODUCT_TO_ENTITY,
    LINK_SALES_TO_IDENTITY_RECORD,
    MARK_SALES_RECORD_LINKED,
    MARK_SALES_RECORD_PENDING_REVIEW,          # ← new
    MERGE_LINE_ITEM,
    MERGE_ORDER,
    MERGE_PRODUCT,
    RESOLVE_SALES_CUSTOMER,
    REWIRE_PURCHASED,
)
```

In `__all__`, add:
- `"FIND_MACHINE_UNIT_CANDIDATES_FOR_SALES"` — after `"FIND_PENDING_CUSTOMER_SALES"` (or near `"CLEAR_SUPERSEDED_SALES_LINKS"`)
- `"MARK_SALES_RECORD_PENDING_REVIEW"` — after `"MARK_SALES_RECORD_LINKED"`

- [ ] **Step 3: Verify with a quick import check**

```bash
uv run --package profile-unifier-ingestion python -c "from src.graph.queries import FIND_MACHINE_UNIT_CANDIDATES_FOR_SALES, MARK_SALES_RECORD_PENDING_REVIEW; print('OK')"
```

Expected: prints `OK`.

- [ ] **Step 4: Lint**

```bash
uv run --package profile-unifier-ingestion ruff check services/ingestion/src/graph/queries/sales.py services/ingestion/src/graph/queries/__init__.py
```

---

## Task 3: Pipeline orchestration + `main.py` wiring

**Files:**
- Modify: `services/ingestion/src/pipeline_sales.py`
- Modify: `services/ingestion/src/main.py`
- Create: `services/ingestion/tests/test_sales_machine_unit_matching.py`

- [ ] **Step 1: Write failing tests**

```python
# services/ingestion/tests/test_sales_machine_unit_matching.py
from __future__ import annotations

from collections.abc import Iterator
from typing import cast
from unittest.mock import MagicMock, call, patch

from neo4j import ManagedTransaction

from src.graph import queries
from src.matching.machine_unit_heuristic import (
    MACHINE_UNIT_BOUGHT_CONFIDENCE,
    MACHINE_UNIT_OWNS_CONFIDENCE,
)
from src.pipeline_sales import _propose_one_pending_sale, propose_machine_unit_matches_for_pending_sales


class _Result:
    def __init__(
        self,
        row: dict[str, object] | None = None,
        rows: list[dict[str, object]] | None = None,
    ) -> None:
        self._row = row
        self._rows = rows if rows is not None else ([] if row is None else [row])

    def single(self) -> dict[str, object] | None:
        return self._row

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._rows)


class _Tx:
    def __init__(self, candidates: list[dict[str, object]] | None = None) -> None:
        self._candidates: list[dict[str, object]] = candidates or []
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **kwargs: object) -> _Result:
        self.calls.append((query, dict(kwargs)))
        # FIND_MACHINE_UNIT_CANDIDATES_FOR_SALES: unique fragment
        if "INVOLVES_UNIT {source_record_pk: $sales_source_record_pk}" in query:
            return _Result(rows=self._candidates)
        # FIND_PENDING_CUSTOMER_SALES: has $limit, returns multiple rows
        if "LIMIT $limit" in query:
            return _Result(
                rows=[{"source_record_pk": "sr-pending", "source_system_key": "sys", "raw_payload": "{}"}]
            )
        return _Result()


class _Session:
    def __init__(self, tx: _Tx) -> None:
        self.tx = tx

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def execute_write(self, callback: object) -> object:
        return callback(cast(ManagedTransaction, self.tx))  # type: ignore[arg-type]


class _Client:
    """Fake Neo4jClient that hands out sessions in order."""

    def __init__(self, *txs: _Tx) -> None:
        self._sessions = [_Session(tx) for tx in txs]

    def session(self) -> _Session:
        return self._sessions.pop(0)


# ---------------------------------------------------------------------------
# _propose_one_pending_sale tests
# ---------------------------------------------------------------------------

def test_propose_no_candidates_returns_false() -> None:
    tx = _Tx(candidates=[])
    with (
        patch("src.pipeline_sales.persist_match_decision") as mock_persist,
        patch("src.pipeline_sales.create_review_case_if_needed") as mock_create,
    ):
        result = _propose_one_pending_sale(cast(ManagedTransaction, tx), "sr-1")

    assert result is False
    mock_persist.assert_not_called()
    mock_create.assert_not_called()
    assert not any("pending_review" in q for q, _ in tx.calls)


def test_propose_owns_unit_candidate_creates_review_case() -> None:
    candidate_row: dict[str, object] = {
        "person_id": "person-1",
        "machine_unit_id": "unit-1",
        "rel_type": "OWNS_UNIT",
        "is_active": True,
        "conflict_flag": False,
        "last_confirmed_at": "2026-06-01T00:00:00+00:00",
    }
    tx = _Tx(candidates=[candidate_row])

    with (
        patch("src.pipeline_sales.persist_match_decision", return_value="md-1") as mock_persist,
        patch("src.pipeline_sales.create_review_case_if_needed", return_value="rc-1") as mock_create,
    ):
        result = _propose_one_pending_sale(cast(ManagedTransaction, tx), "sr-1")

    assert result is True
    mock_persist.assert_called_once()
    match_result_arg = mock_persist.call_args[0][1]
    assert match_result_arg.confidence == MACHINE_UNIT_OWNS_CONFIDENCE
    assert match_result_arg.matched_person_id == "person-1"
    mock_create.assert_called_once()

    pending_review_calls = [(q, k) for q, k in tx.calls if "pending_review" in q]
    assert len(pending_review_calls) == 1
    assert pending_review_calls[0][1]["source_record_pk"] == "sr-1"


def test_propose_bought_unit_candidate_uses_lower_confidence() -> None:
    candidate_row: dict[str, object] = {
        "person_id": "person-2",
        "machine_unit_id": "unit-2",
        "rel_type": "BOUGHT_UNIT",
        "is_active": False,
        "conflict_flag": False,
        "last_confirmed_at": "2026-06-01T00:00:00+00:00",
    }
    tx = _Tx(candidates=[candidate_row])

    with (
        patch("src.pipeline_sales.persist_match_decision", return_value="md-2") as mock_persist,
        patch("src.pipeline_sales.create_review_case_if_needed", return_value="rc-2"),
    ):
        _propose_one_pending_sale(cast(ManagedTransaction, tx), "sr-2")

    match_result_arg = mock_persist.call_args[0][1]
    assert match_result_arg.confidence == MACHINE_UNIT_BOUGHT_CONFIDENCE


def test_propose_selects_best_among_multiple_candidates() -> None:
    """OWNS_UNIT (active, no conflict) beats BOUGHT_UNIT."""
    candidates: list[dict[str, object]] = [
        {
            "person_id": "person-bought",
            "machine_unit_id": "unit-1",
            "rel_type": "BOUGHT_UNIT",
            "is_active": False,
            "conflict_flag": False,
            "last_confirmed_at": "2026-06-10T00:00:00+00:00",  # more recent but lower tier
        },
        {
            "person_id": "person-owns",
            "machine_unit_id": "unit-1",
            "rel_type": "OWNS_UNIT",
            "is_active": True,
            "conflict_flag": False,
            "last_confirmed_at": "2026-05-01T00:00:00+00:00",
        },
    ]
    tx = _Tx(candidates=candidates)

    with (
        patch("src.pipeline_sales.persist_match_decision", return_value="md-x") as mock_persist,
        patch("src.pipeline_sales.create_review_case_if_needed", return_value="rc-x"),
    ):
        _propose_one_pending_sale(cast(ManagedTransaction, tx), "sr-x")

    match_result_arg = mock_persist.call_args[0][1]
    assert match_result_arg.matched_person_id == "person-owns"


# ---------------------------------------------------------------------------
# propose_machine_unit_matches_for_pending_sales tests
# ---------------------------------------------------------------------------

def test_propose_orchestration_returns_count_of_created_cases() -> None:
    """One pending sale, one candidate → one ReviewCase proposed → returns 1."""
    candidate_row: dict[str, object] = {
        "person_id": "person-1",
        "machine_unit_id": "unit-1",
        "rel_type": "OWNS_UNIT",
        "is_active": True,
        "conflict_flag": False,
        "last_confirmed_at": "2026-06-01T00:00:00+00:00",
    }
    # Two sessions: one for _get_pending_pks, one for _propose_one_pending_sale
    get_tx = _Tx()          # FIND_PENDING_CUSTOMER_SALES → ["sr-pending"]
    propose_tx = _Tx(candidates=[candidate_row])
    client = _Client(get_tx, propose_tx)

    with (
        patch("src.pipeline_sales.persist_match_decision", return_value="md-1"),
        patch("src.pipeline_sales.create_review_case_if_needed", return_value="rc-1"),
    ):
        count = propose_machine_unit_matches_for_pending_sales(client)

    assert count == 1


def test_propose_orchestration_no_pending_returns_zero() -> None:
    class _EmptyTx(_Tx):
        def run(self, query: str, **kwargs: object) -> _Result:
            self.calls.append((query, dict(kwargs)))
            return _Result(rows=[])

    client = _Client(_EmptyTx())
    count = propose_machine_unit_matches_for_pending_sales(client)
    assert count == 0
```

- [ ] **Step 2: Run to confirm ImportError on new functions**

```bash
uv run pytest services/ingestion/tests/test_sales_machine_unit_matching.py -v
```

Expected: FAIL with `ImportError: cannot import name '_propose_one_pending_sale'`

- [ ] **Step 3: Add imports and the two new functions to `pipeline_sales.py`**

Add to the imports section (after `from src.models import ...`):

```python
from src.matching.machine_unit_heuristic import (
    MachineUnitCandidate,
    build_machine_unit_match_result,
    select_best_machine_unit_candidate,
)
from src.pipeline_writes import create_review_case_if_needed, persist_match_decision
```

Add the two new functions at the end of `services/ingestion/src/pipeline_sales.py` (after `drain_pending_customer_sales`):

```python
def _propose_one_pending_sale(tx: ManagedTransaction, source_record_pk: str) -> bool:
    """Try to create a machine-unit ReviewCase for one pending-customer sales record.

    Returns True if a ReviewCase was created (link_status → pending_review),
    False if no active Person candidate shares a MachineUnit with the record.
    """
    result = tx.run(
        queries.FIND_MACHINE_UNIT_CANDIDATES_FOR_SALES,
        sales_source_record_pk=source_record_pk,
    )
    candidates: list[MachineUnitCandidate] = [
        MachineUnitCandidate(
            person_id=str(row["person_id"]),
            machine_unit_id=str(row["machine_unit_id"]),
            rel_type=str(row["rel_type"]),
            is_active=bool(row.get("is_active", False)),
            conflict_flag=bool(row.get("conflict_flag", False)),
            last_confirmed_at=(
                str(row["last_confirmed_at"]) if row["last_confirmed_at"] is not None else None
            ),
        )
        for row in result
    ]
    best = select_best_machine_unit_candidate(candidates)
    if best is None:
        return False
    match_result = build_machine_unit_match_result(best)
    decision_id = persist_match_decision(tx, match_result, source_record_pk)
    create_review_case_if_needed(tx, match_result, decision_id)
    tx.run(queries.MARK_SALES_RECORD_PENDING_REVIEW, source_record_pk=source_record_pk)
    return True


def propose_machine_unit_matches_for_pending_sales(
    client: Neo4jClient,
    *,
    batch_size: int = 200,
) -> int:
    """Create machine-unit ReviewCases for all pending-customer sales records.

    Single-pass — records with no candidates stay pending_customer and are
    retried on the next ingestion run. Returns the count of ReviewCases created.
    """

    def _get_pending_pks(tx: ManagedTransaction) -> list[str]:
        rows = list(tx.run(queries.FIND_PENDING_CUSTOMER_SALES, limit=batch_size))
        return [str(row["source_record_pk"]) for row in rows]

    with client.session() as session:
        pending_pks: list[str] = session.execute_write(_get_pending_pks)

    proposed = 0
    for pk in pending_pks:
        def _propose(tx: ManagedTransaction, _pk: str = pk) -> bool:
            return _propose_one_pending_sale(tx, _pk)

        with client.session() as session:
            if session.execute_write(_propose):
                proposed += 1
    return proposed
```

- [ ] **Step 4: Wire into `main.py`**

In `services/ingestion/src/main.py`, update line 51's import:

```python
# Before:
from src.pipeline_sales import drain_pending_customer_sales, ingest_sales_record

# After:
from src.pipeline_sales import (
    drain_pending_customer_sales,
    ingest_sales_record,
    propose_machine_unit_matches_for_pending_sales,
)
```

In `_run_ingestion` (around line 336), insert between `if drained:` and `chat_knows_linked =`:

```python
            if drained:
                logger.info("Drained %d pending sales records", drained)
            proposed = propose_machine_unit_matches_for_pending_sales(client)
            if proposed:
                logger.info("Proposed %d machine-unit review cases for pending sales", proposed)
            chat_knows_linked = materialize_knows_from_chat_relationships(client)
```

- [ ] **Step 5: Run tests — expect PASS**

```bash
uv run pytest services/ingestion/tests/test_sales_machine_unit_matching.py services/ingestion/tests/test_machine_unit_heuristic.py -v
```

Expected: all tests PASS.

- [ ] **Step 6: Type-check and lint changed files**

```bash
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src/pipeline_sales.py services/ingestion/src/main.py
uv run --package profile-unifier-ingestion ruff check services/ingestion/src/pipeline_sales.py services/ingestion/src/main.py
```

---

## Task 4: API Cypher queries

**Files:**
- Modify: `services/api/src/graph/queries/review.py`
- Modify: `services/api/src/graph/queries/__init__.py`

- [ ] **Step 1: Add four new query constants to `review.py`**

Add after `CREATE_NO_MATCH_LOCK_FROM_REVIEW` (line ~230), before `build_review_action_cypher`:

```python
#: Link the Person (ABOUT_RIGHT) to the Order associated with a pending-review
#: sales SourceRecord (ABOUT_LEFT) via PURCHASED. Idempotent (MERGE). No-op
#: when ABOUT_LEFT is not a sales SourceRecord.
LINK_REVIEW_SALES_PURCHASED_ORDER = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT]->(sr:SourceRecord {record_type: 'sales'})
MATCH (md)-[:ABOUT_RIGHT]->(p:Person)
MATCH (o:Order)-[r:INVOLVES_UNIT]->(u:MachineUnit) WHERE r.source_record_pk = sr.source_record_pk
WITH DISTINCT sr, p, o
MERGE (p)-[:PURCHASED {source_system_key: o.source_system_key, source_order_id: o.source_order_id}]->(o)
"""

#: Link the Person to every MachineUnit on the same Order as BOUGHT_UNIT.
#: Idempotent (MERGE). No-op when ABOUT_LEFT is not a sales SourceRecord.
LINK_REVIEW_SALES_BOUGHT_UNIT = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT]->(sr:SourceRecord {record_type: 'sales'})
MATCH (md)-[:ABOUT_RIGHT]->(p:Person)
MATCH (o:Order)-[r:INVOLVES_UNIT]->(u:MachineUnit) WHERE r.source_record_pk = sr.source_record_pk
MERGE (p)-[:BOUGHT_UNIT {source_system_key: o.source_system_key, source_order_id: o.source_order_id}]->(u)
"""

#: Mark the sales SourceRecord as linked on reviewer approval. Returns a row
#: only when ABOUT_LEFT is actually a sales SourceRecord (used as presence check).
MARK_REVIEW_SALES_RECORD_LINKED = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT]->(sr:SourceRecord {record_type: 'sales'})
SET sr.link_status = 'linked', sr.updated_at = datetime()
RETURN sr.source_record_pk AS source_record_pk
"""

#: Mark the sales SourceRecord as unresolved (terminal) on reviewer rejection.
#: No-op when ABOUT_LEFT is not a sales SourceRecord.
MARK_REVIEW_SALES_RECORD_UNRESOLVED = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT]->(sr:SourceRecord {record_type: 'sales'})
SET sr.link_status = 'unresolved', sr.updated_at = datetime()
"""
```

- [ ] **Step 2: Enrich `GET_REVIEW_CASE` to include sales Order/MachineUnit data**

Replace the existing `GET_REVIEW_CASE` constant (lines 155–188) with:

```python
GET_REVIEW_CASE = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
OPTIONAL MATCH (md)-[:ABOUT_LEFT]->(left)
OPTIONAL MATCH (md)-[:ABOUT_RIGHT]->(right)
OPTIONAL MATCH (left_addr:Address) WHERE left:Person AND left_addr.address_id = left.preferred_address_id
OPTIONAL MATCH (right_addr:Address) WHERE right:Person AND right_addr.address_id = right.preferred_address_id
OPTIONAL MATCH (sales_o:Order)-[sui:INVOLVES_UNIT]->(sales_u:MachineUnit)
  WHERE left:SourceRecord AND left.record_type = 'sales'
    AND sui.source_record_pk = left.source_record_pk
WITH rc, md, left, right, left_addr, right_addr,
     collect(DISTINCT sales_o {.order_id, .order_no, .total_amount, .currency, .ordered_at}) AS sales_orders,
     collect(DISTINCT sales_u {
       .machine_unit_id, .machine_product,
       .normalized_lta_tag, .normalized_serial_number, .conflict_flag
     }) AS sales_units
RETURN rc {
  .review_case_id, .queue_state, .priority, .assigned_to,
  .follow_up_at, .sla_due_at, .resolution, .resolved_at,
  .actions, .created_at, .updated_at
} AS review_case,
md {
  .match_decision_id, .engine_type, .engine_version, .policy_version,
  .decision, .confidence, .reasons, .blocking_conflicts, .created_at
} AS match_decision,
CASE WHEN left:Person THEN 'person'
     WHEN left:SourceRecord THEN 'source_record'
     ELSE null END AS left_kind,
CASE WHEN left:Person
     THEN left { .person_id, .status, .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob }
     WHEN left:SourceRecord
     THEN left { .source_record_pk, .source_record_id, .record_type, .normalized_payload, .observed_at }
     ELSE null END AS left_entity,
left_addr { .address_id, .unit_number, .street_number, .street_name, .city, .postal_code, .country_code, .normalized_full } AS left_address,
CASE WHEN right:Person THEN 'person'
     WHEN right:SourceRecord THEN 'source_record'
     ELSE null END AS right_kind,
CASE WHEN right:Person
     THEN right { .person_id, .status, .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob }
     WHEN right:SourceRecord
     THEN right { .source_record_pk, .source_record_id, .record_type, .normalized_payload, .observed_at }
     ELSE null END AS right_entity,
right_addr { .address_id, .unit_number, .street_number, .street_name, .city, .postal_code, .country_code, .normalized_full } AS right_address,
sales_orders[0] AS sales_order,
sales_units AS sales_units
"""
```

Note: `left_entity` now includes `.record_type` for SourceRecord projections. `result.single()` remains safe because the WITH+collect collapses any multi-unit OPTIONAL MATCH expansion back to exactly one row.

- [ ] **Step 3: Export the four new queries from `__init__.py`**

In `services/api/src/graph/queries/__init__.py`, update the review import block:

```python
from src.graph.queries.review import (
    ASSIGN_REVIEW_CASE,
    CREATE_NO_MATCH_LOCK_FROM_REVIEW,
    GET_PERSONS_FOR_REVIEW_MERGE,
    GET_REVIEW_CASE,
    LINK_REVIEW_SALES_BOUGHT_UNIT,       # ← new
    LINK_REVIEW_SALES_PURCHASED_ORDER,   # ← new
    MARK_REVIEW_SALES_RECORD_LINKED,     # ← new
    MARK_REVIEW_SALES_RECORD_UNRESOLVED, # ← new
    REVIEW_SORT_KEYS,
    build_count_review_cases_query,
    build_list_review_cases_query,
    build_review_action_cypher,
)
```

Add the four new names to `__all__` near the other LINK_*/MARK_* entries:
- `"LINK_REVIEW_SALES_BOUGHT_UNIT"` — near the other `LINK_*` entries
- `"LINK_REVIEW_SALES_PURCHASED_ORDER"` — near the other `LINK_*` entries
- `"MARK_REVIEW_SALES_RECORD_LINKED"` — near other `MARK_*` entries
- `"MARK_REVIEW_SALES_RECORD_UNRESOLVED"` — near other `MARK_*` entries

- [ ] **Step 4: Verify import**

```bash
uv run --package profile-unifier-api python -c "from src.graph.queries import LINK_REVIEW_SALES_PURCHASED_ORDER, LINK_REVIEW_SALES_BOUGHT_UNIT, MARK_REVIEW_SALES_RECORD_LINKED, MARK_REVIEW_SALES_RECORD_UNRESOLVED; print('OK')"
```

- [ ] **Step 5: Lint**

```bash
uv run --package profile-unifier-api ruff check services/api/src/graph/queries/review.py services/api/src/graph/queries/__init__.py
```

---

## Task 5: API types + mapper

**Files:**
- Modify: `services/api/src/types.py`
- Modify: `services/api/src/graph/mappers.py`
- Create: `services/api/tests/test_review_mappers.py`

- [ ] **Step 1: Write failing mapper tests**

```python
# services/api/tests/test_review_mappers.py
from __future__ import annotations

from src.graph.mappers import _map_sales_summary, map_review_case_detail  # type: ignore[attr-defined]
from src.types import SalesOrderSummary, SalesUnitSummary


# ---------------------------------------------------------------------------
# _map_sales_summary
# ---------------------------------------------------------------------------

def test_map_sales_summary_returns_none_for_none_order() -> None:
    assert _map_sales_summary(None, None) is None


def test_map_sales_summary_returns_none_for_empty_order() -> None:
    assert _map_sales_summary({}, []) is None


def test_map_sales_summary_maps_order_and_units() -> None:
    order: dict[str, object] = {
        "order_id": "o-1",
        "order_no": "ORD-001",
        "total_amount": 1500.0,
        "currency": "SGD",
        "ordered_at": "2026-04-01T10:00:00Z",
    }
    units: list[dict[str, object]] = [
        {
            "machine_unit_id": "mu-1",
            "machine_product": "Forklift X",
            "normalized_lta_tag": "LTA001",
            "normalized_serial_number": "SN001",
            "conflict_flag": False,
        },
        {
            "machine_unit_id": "mu-2",
            "machine_product": "Pallet Jack Y",
            "normalized_lta_tag": None,
            "normalized_serial_number": "SN002",
            "conflict_flag": True,
        },
    ]

    result = _map_sales_summary(order, units)

    assert isinstance(result, SalesOrderSummary)
    assert result.order_id == "o-1"
    assert result.order_no == "ORD-001"
    assert result.total_amount == 1500.0
    assert result.currency == "SGD"
    assert len(result.units) == 2

    u1 = result.units[0]
    assert isinstance(u1, SalesUnitSummary)
    assert u1.machine_unit_id == "mu-1"
    assert u1.normalized_lta_tag == "LTA001"
    assert u1.conflict_flag is False

    u2 = result.units[1]
    assert u2.machine_unit_id == "mu-2"
    assert u2.normalized_lta_tag is None
    assert u2.conflict_flag is True


def test_map_sales_summary_empty_units_list() -> None:
    order: dict[str, object] = {"order_id": "o-2", "order_no": None}
    result = _map_sales_summary(order, [])
    assert isinstance(result, SalesOrderSummary)
    assert result.units == []
    assert result.order_no is None


# ---------------------------------------------------------------------------
# map_review_case_detail with sales enrichment
# ---------------------------------------------------------------------------

def _minimal_review_record(
    *,
    left_kind: str = "person",
    left_entity: dict[str, object] | None = None,
    sales_order: dict[str, object] | None = None,
    sales_units: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "review_case": {
            "review_case_id": "rc-1",
            "queue_state": "open",
            "priority": 100,
            "assigned_to": None,
            "follow_up_at": None,
            "sla_due_at": None,
            "resolution": None,
            "resolved_at": None,
            "actions": [],
            "created_at": "2026-06-01T00:00:00Z",
            "updated_at": "2026-06-01T00:00:00Z",
        },
        "match_decision": {
            "match_decision_id": "md-1",
            "engine_type": "heuristic",
            "engine_version": "v0.1.0",
            "policy_version": "v0.1.0",
            "decision": "review",
            "confidence": 0.65,
            "reasons": ["same_machine_unit_owner_claim (OWNS_UNIT, person p-1, unit u-1)"],
            "blocking_conflicts": [],
            "created_at": "2026-06-01T00:00:00Z",
        },
        "left_kind": left_kind,
        "left_entity": left_entity,
        "left_address": None,
        "right_kind": "person",
        "right_entity": {
            "person_id": "p-right",
            "status": "active",
            "preferred_full_name": "Alice",
            "preferred_phone": None,
            "preferred_email": None,
            "preferred_dob": None,
        },
        "right_address": None,
        "sales_order": sales_order,
        "sales_units": sales_units if sales_units is not None else [],
    }


def test_map_review_case_detail_populates_sales_summary_for_sales_source_record() -> None:
    record = _minimal_review_record(
        left_kind="source_record",
        left_entity={
            "source_record_pk": "sr-1",
            "source_record_id": "sale-001",
            "record_type": "sales",
            "normalized_payload": None,
            "observed_at": "2026-06-01T00:00:00Z",
        },
        sales_order={
            "order_id": "o-1",
            "order_no": "ORD-001",
            "total_amount": 1200.0,
            "currency": "SGD",
            "ordered_at": None,
        },
        sales_units=[
            {
                "machine_unit_id": "mu-1",
                "machine_product": "Forklift X",
                "normalized_lta_tag": "LTA001",
                "normalized_serial_number": None,
                "conflict_flag": False,
            }
        ],
    )

    detail = map_review_case_detail(record)

    assert detail.comparison_left is not None
    assert detail.comparison_left.entity_kind == "source_record"
    assert detail.comparison_left.sales_summary is not None
    assert detail.comparison_left.sales_summary.order_id == "o-1"
    assert len(detail.comparison_left.sales_summary.units) == 1
    assert detail.comparison_left.sales_summary.units[0].machine_unit_id == "mu-1"


def test_map_review_case_detail_no_sales_summary_for_person_left() -> None:
    record = _minimal_review_record(
        left_kind="person",
        left_entity={
            "person_id": "p-left",
            "status": "active",
            "preferred_full_name": "Bob",
            "preferred_phone": None,
            "preferred_email": None,
            "preferred_dob": None,
        },
        sales_order=None,
        sales_units=[],
    )

    detail = map_review_case_detail(record)

    assert detail.comparison_left is not None
    assert detail.comparison_left.entity_kind == "person"
    assert detail.comparison_left.sales_summary is None
```

- [ ] **Step 2: Run tests to confirm FAIL**

```bash
uv run pytest services/api/tests/test_review_mappers.py -v
```

Expected: FAIL with `ImportError: cannot import name '_map_sales_summary'` or `AttributeError: 'PersonComparisonEntity' object has no attribute 'sales_summary'`.

- [ ] **Step 3: Add `SalesUnitSummary`, `SalesOrderSummary`, `sales_summary` field to `types.py`**

In `services/api/src/types.py`, add the two new models just before `PersonComparisonEntity` (around line 390):

```python
class SalesUnitSummary(BaseModel):
    machine_unit_id: str
    machine_product: str | None = None
    normalized_lta_tag: str | None = None
    normalized_serial_number: str | None = None
    conflict_flag: bool = False


class SalesOrderSummary(BaseModel):
    order_id: str
    order_no: str | None = None
    total_amount: float | None = None
    currency: str | None = None
    ordered_at: str | None = None
    units: list[SalesUnitSummary] = Field(default_factory=list)
```

Then add `sales_summary` to `PersonComparisonEntity`:

```python
class PersonComparisonEntity(BaseModel):
    entity_kind: Literal["person", "source_record"] = "person"
    person_id: str | None = None
    source_record_pk: str | None = None
    source_record_id: str | None = None
    status: str | None = None
    preferred_full_name: str | None = None
    preferred_phone: str | None = None
    preferred_email: str | None = None
    preferred_dob: str | None = None
    preferred_address: AddressSummary | None = None
    sales_summary: SalesOrderSummary | None = None   # ← new
```

- [ ] **Step 4: Update `mappers.py`**

**4a.** Add `SalesOrderSummary` and `SalesUnitSummary` to the `from src.types import (...)` block in `services/api/src/graph/mappers.py`:

```python
from src.types import (
    ...
    SalesOrderSummary,   # ← new
    SalesUnitSummary,    # ← new
    ...
)
```

**4b.** Add the `_map_sales_summary` helper — insert it just before `_map_comparison_entity` (around line 564):

```python
def _map_sales_summary(
    sales_order: GraphValue, sales_units: GraphValue
) -> SalesOrderSummary | None:
    order = _as_dict(sales_order)
    if not order:
        return None
    units: list[SalesUnitSummary] = []
    if isinstance(sales_units, list):
        for raw in sales_units:
            u = _as_dict(raw)
            if not u:
                continue
            units.append(
                SalesUnitSummary(
                    machine_unit_id=to_str(u.get("machine_unit_id")),
                    machine_product=to_optional_str(u.get("machine_product")),
                    normalized_lta_tag=to_optional_str(u.get("normalized_lta_tag")),
                    normalized_serial_number=to_optional_str(u.get("normalized_serial_number")),
                    conflict_flag=bool(u.get("conflict_flag", False)),
                )
            )
    return SalesOrderSummary(
        order_id=to_str(order.get("order_id")),
        order_no=to_optional_str(order.get("order_no")),
        total_amount=to_optional_float(order.get("total_amount")),
        currency=to_optional_str(order.get("currency")),
        ordered_at=to_iso_or_none(order.get("ordered_at")),
        units=units,
    )
```

**4c.** Update `_map_comparison_entity` signature and body to accept and forward the sales params:

```python
def _map_comparison_entity(
    kind: GraphValue,
    entity: GraphValue,
    address: GraphValue,
    sales_order: GraphValue = None,
    sales_units: GraphValue = None,
) -> PersonComparisonEntity | None:
    e = _as_dict(entity)
    if not e:
        return None
    kind_str = to_optional_str(kind)
    if kind_str == "source_record":
        return _map_source_record_comparison(e, sales_order, sales_units)
    return PersonComparisonEntity(
        entity_kind="person",
        person_id=to_optional_str(e.get("person_id")),
        status=to_optional_str(e.get("status")),
        preferred_full_name=to_optional_str(e.get("preferred_full_name")),
        preferred_phone=to_optional_str(e.get("preferred_phone")),
        preferred_email=to_optional_str(e.get("preferred_email")),
        preferred_dob=to_optional_str(e.get("preferred_dob")),
        preferred_address=map_address(address),
    )
```

**4d.** Update `_map_source_record_comparison` signature and body:

```python
def _map_source_record_comparison(
    e: GraphRecord,
    sales_order: GraphValue = None,
    sales_units: GraphValue = None,
) -> PersonComparisonEntity:
    payload = _parse_normalized_payload(e.get("normalized_payload"))
    return PersonComparisonEntity(
        entity_kind="source_record",
        source_record_pk=to_optional_str(e.get("source_record_pk")),
        source_record_id=to_optional_str(e.get("source_record_id")),
        status=None,
        preferred_full_name=_attribute_value(payload, "full_name"),
        preferred_phone=_identifier_value(payload, "phone"),
        preferred_email=_identifier_value(payload, "email"),
        preferred_dob=_attribute_value(payload, "dob"),
        preferred_address=_source_record_address(payload),
        sales_summary=_map_sales_summary(sales_order, sales_units),
    )
```

**4e.** Update `map_review_case_detail` to pass `sales_order`/`sales_units` for the left entity:

```python
        comparison_left=_map_comparison_entity(
            record.get("left_kind"),
            record.get("left_entity"),
            record.get("left_address"),
            record.get("sales_order"),   # ← new
            record.get("sales_units"),   # ← new
        ),
        comparison_right=_map_comparison_entity(
            record.get("right_kind"), record.get("right_entity"), record.get("right_address")
        ),
```

- [ ] **Step 5: Run mapper tests — expect PASS**

```bash
uv run pytest services/api/tests/test_review_mappers.py -v
```

Expected: all tests PASS.

- [ ] **Step 6: Run the full API test suite to catch regressions**

```bash
uv run pytest services/api/tests -v
```

Expected: all existing tests still PASS (the `map_review_case_detail` change is backward-compatible — `sales_order` defaults to `None`, `_map_sales_summary(None, None)` returns `None`, so `sales_summary` remains `None` for existing person-to-person cases).

- [ ] **Step 7: Type-check and lint**

```bash
uv run --package profile-unifier-api mypy --strict services/api/src/types.py services/api/src/graph/mappers.py
uv run --package profile-unifier-api ruff check services/api/src/types.py services/api/src/graph/mappers.py
```

---

## Task 6: Review repository — sales-link merge branch + reject/unresolved

**Files:**
- Modify: `services/api/src/repositories/neo4j/review.py`
- Modify: `services/api/tests/test_review_repository_merge.py`

- [ ] **Step 1: Update existing test to reflect new query order**

In `services/api/tests/test_review_repository_merge.py`, add `MARK_REVIEW_SALES_RECORD_UNRESOLVED` to the imports:

```python
from src.graph.queries import (
    CHECK_BOTH_PERSONS_ACTIVE,
    CHECK_NO_MATCH_LOCK,
    CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED,
    CREATE_NO_MATCH_LOCK_FROM_REVIEW,
    EXECUTE_MANUAL_MERGE,
    GET_PERSONS_FOR_REVIEW_MERGE,
    MARK_REVIEW_SALES_RECORD_UNRESOLVED,     # ← new
    REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
)
```

Update `test_manual_no_match_creates_review_lock_after_action` — change the assertion at the bottom from:

```python
    assert tx.calls[-1].query == CREATE_NO_MATCH_LOCK_FROM_REVIEW
    assert tx.calls[-1].params == {
        "review_case_id": "case-1",
        "notes": "not the same person",
        "actor_id": "reviewer@example.com",
    }
```

to:

```python
    assert tx.calls[-2].query == CREATE_NO_MATCH_LOCK_FROM_REVIEW
    assert tx.calls[-2].params == {
        "review_case_id": "case-1",
        "notes": "not the same person",
        "actor_id": "reviewer@example.com",
    }
    assert tx.calls[-1].query == MARK_REVIEW_SALES_RECORD_UNRESOLVED
    assert tx.calls[-1].params == {"review_case_id": "case-1"}
```

- [ ] **Step 2: Write new test cases for the sales-link branch**

Append the following three tests to `services/api/tests/test_review_repository_merge.py`:

```python
@pytest.mark.asyncio
async def test_merge_sales_link_approves_and_links() -> None:
    """MERGE on a sales-link review case runs PURCHASED+BOUGHT_UNIT+action and returns ActionResult."""
    from src.graph.queries import (
        LINK_REVIEW_SALES_BOUGHT_UNIT,
        LINK_REVIEW_SALES_PURCHASED_ORDER,
        MARK_REVIEW_SALES_RECORD_LINKED,
    )

    # Records consumed in order:
    # 1. GET_PERSONS_FOR_REVIEW_MERGE → None (left is SourceRecord, not Person)
    # 2. LINK_REVIEW_SALES_PURCHASED_ORDER → None (write, no return)
    # 3. LINK_REVIEW_SALES_BOUGHT_UNIT → None (write, no return)
    # 4. MARK_REVIEW_SALES_RECORD_LINKED → linked record (presence check)
    # 5. build_review_action_cypher → review_case record
    tx = _Tx(
        [
            None,
            None,
            None,
            {"source_record_pk": "sr-1"},
            {"review_case": {"review_case_id": "case-2", "queue_state": "resolved", "resolution": "merge"}},
        ]
    )

    result = await _action_tx(
        cast(AsyncManagedTransaction, tx),
        "case-2",
        ApiReviewActionType.MERGE.value,
        "resolved",
        "merge",
        "same machine unit",
        None,
        "reviewer@example.com",
        None,
        [],
    )

    assert result == {
        "review_case_id": "case-2",
        "queue_state": "resolved",
        "resolution": "merge",
    }
    assert "survivor_person_id" not in result  # no golden-profile recompute

    query_sequence = [c.query for c in tx.calls]
    assert query_sequence[0] == GET_PERSONS_FOR_REVIEW_MERGE
    assert query_sequence[1] == LINK_REVIEW_SALES_PURCHASED_ORDER
    assert query_sequence[2] == LINK_REVIEW_SALES_BOUGHT_UNIT
    assert query_sequence[3] == MARK_REVIEW_SALES_RECORD_LINKED


@pytest.mark.asyncio
async def test_merge_returns_not_applicable_when_no_persons_and_no_sales_link() -> None:
    """MERGE with neither a person pair nor a sales SourceRecord returns merge_not_applicable."""
    tx = _Tx([])  # All queries return None (records exhausted)

    result = await _action_tx(
        cast(AsyncManagedTransaction, tx),
        "case-3",
        ApiReviewActionType.MERGE.value,
        "resolved",
        "merge",
        None,
        None,
        "reviewer@example.com",
        None,
        [],
    )

    assert result == {"merge_not_applicable": True}


@pytest.mark.asyncio
async def test_reject_marks_sales_record_unresolved() -> None:
    """REJECT runs build_review_action_cypher then MARK_REVIEW_SALES_RECORD_UNRESOLVED."""
    tx = _Tx(
        [
            {"review_case": {"review_case_id": "case-4", "queue_state": "resolved", "resolution": "reject"}},
        ]
    )

    result = await _action_tx(
        cast(AsyncManagedTransaction, tx),
        "case-4",
        ApiReviewActionType.REJECT.value,
        "resolved",
        "reject",
        "not the same person",
        None,
        "reviewer@example.com",
        None,
        [],
    )

    assert result == {
        "review_case_id": "case-4",
        "queue_state": "resolved",
        "resolution": "reject",
    }
    assert tx.calls[-1].query == MARK_REVIEW_SALES_RECORD_UNRESOLVED
    assert tx.calls[-1].params == {"review_case_id": "case-4"}
```

- [ ] **Step 3: Run the tests to confirm they fail**

```bash
uv run pytest services/api/tests/test_review_repository_merge.py -v
```

Expected: 4 failures (the updated manual_no_match test + 3 new tests).

- [ ] **Step 4: Implement the changes in `review.py`**

**4a.** Add the new query imports in `services/api/src/repositories/neo4j/review.py`:

```python
from src.graph.queries import (
    ASSIGN_REVIEW_CASE,
    CHECK_BOTH_PERSONS_ACTIVE,
    CHECK_NO_MATCH_LOCK,
    CREATE_NO_MATCH_LOCK_FROM_REVIEW,
    EXECUTE_MANUAL_MERGE,
    GET_PERSONS_FOR_REVIEW_MERGE,
    GET_REVIEW_CASE,
    LINK_REVIEW_SALES_BOUGHT_UNIT,        # ← new
    LINK_REVIEW_SALES_PURCHASED_ORDER,    # ← new
    MARK_REVIEW_SALES_RECORD_LINKED,      # ← new
    MARK_REVIEW_SALES_RECORD_UNRESOLVED,  # ← new
    build_count_review_cases_query,
    build_list_review_cases_query,
    build_review_action_cypher,
)
```

**4b.** Add `_sales_link_merge_tx` as a new private coroutine — insert it just before `_action_tx` (around line 168):

```python
async def _sales_link_merge_tx(
    tx: AsyncManagedTransaction,
    review_case_id: str,
    new_state: str,
    resolution: str | None,
    follow_up_at: str | None,
    action_type: str,
    actor_id: str,
    notes: str | None,
) -> ActionResult:
    """Sales-link merge branch: links Person→Order+MachineUnit, transitions link_status→linked.

    All three write queries are no-ops (MATCH requires SourceRecord {record_type:'sales'})
    when the left side is not a sales SourceRecord, so this is safe to call as a
    fallback after GET_PERSONS_FOR_REVIEW_MERGE returns None.
    Returns merge_not_applicable when MARK_REVIEW_SALES_RECORD_LINKED matches nothing.
    """
    await tx.run(LINK_REVIEW_SALES_PURCHASED_ORDER, review_case_id=review_case_id)
    await tx.run(LINK_REVIEW_SALES_BOUGHT_UNIT, review_case_id=review_case_id)
    linked_result = await tx.run(MARK_REVIEW_SALES_RECORD_LINKED, review_case_id=review_case_id)
    linked_record = await linked_result.single()
    if linked_record is None:
        return ActionResult(merge_not_applicable=True)

    cypher = build_review_action_cypher(resolution, follow_up_at)
    action_result = await tx.run(
        cypher,
        review_case_id=review_case_id,
        new_state=new_state,
        resolution=resolution,
        follow_up_at=follow_up_at,
        action_json=_action_entry_json(action_type, "reviewer", actor_id, notes),
    )
    record = await action_result.single()
    if record is None:
        return ActionResult(merge_not_applicable=True)
    rc = dict(record["review_case"])
    return ActionResult(
        review_case_id=to_str(rc.get("review_case_id")),
        queue_state=to_str(rc.get("queue_state")),
        resolution=to_optional_str(rc.get("resolution")),
    )
```

**4c.** In `_action_tx`, change the early return when `persons_record is None` (line ~186–187):

```python
# Before:
        if persons_record is None:
            return ActionResult(merge_not_applicable=True)

# After:
        if persons_record is None:
            return await _sales_link_merge_tx(
                tx, review_case_id, new_state, resolution, follow_up_at,
                action_type, actor_id, notes,
            )
```

**4d.** In `_action_tx`, after the final `elif action_type == MERGE ...` block (line ~253) and before `return out`, add:

```python
    if action_type in (
        ApiReviewActionType.MANUAL_NO_MATCH.value,
        ApiReviewActionType.REJECT.value,
    ):
        await tx.run(MARK_REVIEW_SALES_RECORD_UNRESOLVED, review_case_id=review_case_id)

    return out
```

The complete modified `_action_tx` post-action block (replacing lines 233–255) should look like:

```python
    if action_type == ApiReviewActionType.MANUAL_NO_MATCH.value:
        await tx.run(
            CREATE_NO_MATCH_LOCK_FROM_REVIEW,
            review_case_id=review_case_id,
            notes=notes or "Manual no-match from review",
            actor_id=actor_id,
        )
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
            await apply_merge_review_side_effects(tx, merge_event_id, absorbed_id, survivor_id)
        out["survivor_person_id"] = survivor_id
        out["golden_profile_selections"] = golden_profile_selections

    if action_type in (
        ApiReviewActionType.MANUAL_NO_MATCH.value,
        ApiReviewActionType.REJECT.value,
    ):
        await tx.run(MARK_REVIEW_SALES_RECORD_UNRESOLVED, review_case_id=review_case_id)

    return out
```

- [ ] **Step 5: Run all review tests — expect PASS**

```bash
uv run pytest services/api/tests/test_review_repository_merge.py -v
```

Expected: all 7 tests PASS (4 original + 3 new).

- [ ] **Step 6: Run the full API test suite**

```bash
uv run pytest services/api/tests -v
```

Expected: all tests PASS.

- [ ] **Step 7: Type-check and lint**

```bash
uv run --package profile-unifier-api mypy --strict services/api/src/repositories/neo4j/review.py
uv run --package profile-unifier-api ruff check services/api/src/repositories/neo4j/review.py
```

---

## Final Verification

- [ ] **Run all ingestion tests**

```bash
uv run pytest services/ingestion/tests -v
```

- [ ] **Run all API tests**

```bash
uv run pytest services/api/tests -v
```

- [ ] **Type-check all modified Python files**

```bash
uv run --package profile-unifier-ingestion mypy --strict \
  services/ingestion/src/matching/machine_unit_heuristic.py \
  services/ingestion/src/graph/queries/sales.py \
  services/ingestion/src/pipeline_sales.py \
  services/ingestion/src/main.py

uv run --package profile-unifier-api mypy --strict \
  services/api/src/graph/queries/review.py \
  services/api/src/types.py \
  services/api/src/graph/mappers.py \
  services/api/src/repositories/neo4j/review.py
```

- [ ] **Lint all modified files**

```bash
uv run --package profile-unifier-ingestion ruff check \
  services/ingestion/src/matching/machine_unit_heuristic.py \
  services/ingestion/src/graph/queries/sales.py \
  services/ingestion/src/graph/queries/__init__.py \
  services/ingestion/src/pipeline_sales.py \
  services/ingestion/src/main.py

uv run --package profile-unifier-api ruff check \
  services/api/src/graph/queries/review.py \
  services/api/src/graph/queries/__init__.py \
  services/api/src/types.py \
  services/api/src/graph/mappers.py \
  services/api/src/repositories/neo4j/review.py
```

---

## Self-Review

**Spec coverage check:**

| Spec section | Task |
|---|---|
| Trigger & state machine (`pending_customer` → `pending_review` → `linked`/`unresolved`) | Task 2 (MARK_SALES_RECORD_PENDING_REVIEW), Task 4 (MARK_REVIEW_SALES_RECORD_LINKED, MARK_REVIEW_SALES_RECORD_UNRESOLVED), Task 6 (`_action_tx`) |
| Candidate generation (INVOLVES_UNIT traversal → BOUGHT_UNIT/OWNS_UNIT) | Task 2 (FIND_MACHINE_UNIT_CANDIDATES_FOR_SALES), Task 3 (_propose_one_pending_sale) |
| Scoring constants (0.65/0.60, review-only band) | Task 1 (machine_unit_heuristic.py) |
| MatchDecision + ReviewCase creation (reuse pipeline_writes) | Task 3 |
| End-of-run trigger in main.py | Task 3 |
| Review merge: PURCHASED + BOUGHT_UNIT + link_status=linked | Task 4 (queries) + Task 6 (_sales_link_merge_tx) |
| Review reject/manual_no_match: link_status=unresolved | Task 4 (MARK_REVIEW_SALES_RECORD_UNRESOLVED) + Task 6 (_action_tx) |
| No golden-profile recompute on sales-link merge | Covered by design: `_sales_link_merge_tx` never sets `survivor_person_id` → `submit_action` skips recompute |
| GET_REVIEW_CASE enrichment (sales_order + sales_units) | Task 4 (GET_REVIEW_CASE WITH+collect) |
| API types: SalesUnitSummary, SalesOrderSummary, PersonComparisonEntity.sales_summary | Task 5 (types.py) |
| Mapper: _map_sales_summary, updated _map_comparison_entity | Task 5 (mappers.py) |

**Placeholder scan:** No TBDs or TODOs found.

**Type consistency check:**
- `MachineUnitCandidate` defined in Task 1, used in Tasks 1 and 3 ✓
- `SalesOrderSummary`/`SalesUnitSummary` defined in Task 5 (types.py), used in Task 5 (mappers.py) and test ✓
- `MARK_REVIEW_SALES_RECORD_UNRESOLVED` added to query module in Task 4, imported in review.py in Task 6, imported in test file in Task 6 ✓
- `_sales_link_merge_tx` defined and called in Task 6 only ✓
- `_map_sales_summary(sales_order: GraphValue, sales_units: GraphValue)` signature consistent with call site in `_map_source_record_comparison` and mapper test ✓
