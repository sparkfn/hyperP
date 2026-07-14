# Profile Unifier Record Update Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make immutable upstream record updates preserve accepted evidence until a replacement is accepted, with one concurrency-safe lifecycle shared by every ingestion path.

**Architecture:** Add a focused lifecycle module that locks a source identity, detects duplicate active or pending hashes, allocates immutable versions, and performs compare-and-transition activation. Existing identity and specialized pipelines supply record-specific projection callbacks; review resolution invokes the same activation contract so pending records have no active side effects.

**Tech Stack:** Python 3.12, Pydantic, Neo4j/Cypher, FastAPI, pytest, mypy strict, Ruff.

---

## File map

- Create `services/ingestion/src/record_lifecycle.py`: typed lifecycle context, duplicate detection, staging, activation, and rejection orchestration.
- Create `services/ingestion/tests/test_record_lifecycle.py`: shared state-machine and concurrency contract tests.
- Modify `services/ingestion/src/models.py`: lifecycle status and transition models.
- Modify `services/ingestion/src/graph/queries/source_records.py`: locked state reads and conditional lifecycle transitions.
- Modify `services/ingestion/src/graph/queries/__init__.py`: export lifecycle queries.
- Modify `services/ingestion/src/graph/schema_init.py`: unique source-version constraint and source-identity lock constraint.
- Modify `services/ingestion/src/pipeline.py`: consume lifecycle context and continuity candidate; delay projection retirement until activation.
- Modify `services/ingestion/src/pipeline_writes.py`: stage records and isolate identity projector activation/retirement.
- Modify `services/ingestion/src/matching/engine.py`: accept a distinguished continuity Person and apply reassignment policy.
- Modify `services/ingestion/src/pipeline_addresses.py`: use lifecycle coordination and sourced address retirement.
- Modify `services/ingestion/src/pipeline_sales.py`: stage pending customer updates and atomically replace sales projections.
- Modify `services/ingestion/src/pipeline_knows.py`: materialize active records only.
- Modify `services/ingestion/src/pipeline_bankruptcy.py`: expose activation and retirement operations.
- Modify `services/ingestion/src/graph/queries/sales.py`, `knows.py`, `bankruptcy.py`, and `vehicle.py`: source-scoped active projection queries.
- Modify `services/api/src/graph/queries/review.py`: approve/reject lifecycle transitions without duplicating ingestion rules.
- Modify `services/api/src/graph/queries/persons.py`, `services/api/src/types.py`, and frontend API mirrors if required: expose lifecycle status and select active records deliberately.
- Modify `services/ingestion/src/graph/migrations.py`: migrate legacy `is_latest` state safely.
- Modify focused tests beside each pipeline and API query module.
- Modify `docs/profile-unifier-architecture.md`, `docs/profile-unifier-api-spec.md`, and `docs/profile-unifier-openapi-3.1.yaml`: document lifecycle semantics and response fields.

### Task 1: Define lifecycle types and locked source-state queries

**Files:**
- Modify: `services/ingestion/src/models.py`
- Modify: `services/ingestion/src/graph/queries/source_records.py`
- Modify: `services/ingestion/src/graph/queries/__init__.py`
- Modify: `services/ingestion/src/graph/schema_init.py`
- Test: `services/ingestion/tests/test_record_lifecycle.py`

- [ ] **Step 1: Write failing model and query contract tests**

```python
from src.graph import queries
from src.models import SourceRecordLifecycleStatus


def test_lifecycle_status_values_are_stable() -> None:
    assert [status.value for status in SourceRecordLifecycleStatus] == [
        "active",
        "pending_review",
        "superseded",
        "rejected",
        "link_failed",
    ]


def test_locked_state_query_reads_active_and_pending_versions() -> None:
    query = queries.LOCK_AND_GET_SOURCE_STATE
    assert "MERGE (lock:SourceRecordIdentityLock" in query
    assert "lifecycle_status = 'active'" in query
    assert "lifecycle_status = 'pending_review'" in query
    assert "source_record_version" in query


def test_activation_query_is_compare_and_transition() -> None:
    query = queries.ACTIVATE_SOURCE_RECORD_VERSION
    assert "old.lifecycle_status = 'active'" in query
    assert "new.lifecycle_status = 'pending_review'" in query
    assert "RETURN new.source_record_pk" in query
```

- [ ] **Step 2: Run the tests and confirm the missing symbols fail**

Run: `uv run --package profile-unifier-ingestion pytest services/ingestion/tests/test_record_lifecycle.py -v`

Expected: collection fails because `SourceRecordLifecycleStatus` and lifecycle queries do not exist.

- [ ] **Step 3: Add strict lifecycle models**

```python
class SourceRecordLifecycleStatus(StrEnum):
    ACTIVE = "active"
    PENDING_REVIEW = "pending_review"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    LINK_FAILED = "link_failed"


@dataclass(frozen=True)
class SourceVersionState:
    source_record_pk: str
    source_record_version: int
    record_hash: str
    lifecycle_status: SourceRecordLifecycleStatus
    linked_person_ids: tuple[str, ...]


@dataclass(frozen=True)
class SourceLifecycleState:
    active: SourceVersionState | None
    pending: SourceVersionState | None
    next_version: int
```

Import `StrEnum` from `enum` and `dataclass` from `dataclasses`; do not use loose dictionaries in coordinator-facing code.

- [ ] **Step 4: Add lock, stage, activate, reject, and fail queries**

Add these query constants and export them from `queries/__init__.py`:

```cypher
LOCK_AND_GET_SOURCE_STATE = """
MERGE (lock:SourceRecordIdentityLock {
    source_system: $source_system,
    source_record_id: $source_record_id
})
ON CREATE SET lock.created_at = datetime()
SET lock.touched_at = datetime()
WITH lock
OPTIONAL MATCH (sr:SourceRecord {source_record_id: lock.source_record_id})
      -[:FROM_SOURCE]->(:SourceSystem {source_key: lock.source_system})
WHERE sr.lifecycle_status IN ['active', 'pending_review']
OPTIONAL MATCH (sr)-[:LINKED_TO]->(p:Person)
RETURN sr.source_record_pk AS source_record_pk,
       sr.source_record_version AS source_record_version,
       sr.record_hash AS record_hash,
       sr.lifecycle_status AS lifecycle_status,
       collect(DISTINCT p.person_id) AS linked_person_ids
ORDER BY toInteger(sr.source_record_version) DESC
"""

ACTIVATE_SOURCE_RECORD_VERSION = """
MATCH (old:SourceRecord {source_record_pk: $old_source_record_pk})
WHERE old.lifecycle_status = 'active'
MATCH (new:SourceRecord {source_record_pk: $new_source_record_pk})
WHERE new.lifecycle_status = 'pending_review'
SET old.lifecycle_status = 'superseded',
    old.superseded_at = datetime(),
    new.lifecycle_status = 'active',
    new.activated_at = datetime()
MERGE (old)-[:PREVIOUS_VERSION_OF]->(new)
RETURN new.source_record_pk AS source_record_pk
"""
```

Also add `ACTIVATE_FIRST_SOURCE_RECORD_VERSION`, `REJECT_PENDING_SOURCE_RECORD`, and `MARK_SOURCE_RECORD_LINK_FAILED` with explicit allowed-from states and a returned `source_record_pk` so callers can detect a lost compare-and-transition race.

- [ ] **Step 5: Add schema constraints**

Add idempotent Neo4j constraints for the lock tuple and source version tuple. Use a synthetic `source_version_key` property formatted as `source_system + '|' + source_record_id + '|' + source_record_version` because the `SourceSystem` key currently lives across a relationship and cannot participate in a node uniqueness constraint.

```cypher
CREATE CONSTRAINT source_record_version_key_unique IF NOT EXISTS
FOR (sr:SourceRecord) REQUIRE sr.source_version_key IS UNIQUE
```

- [ ] **Step 6: Run focused tests and static checks**

Run:

```powershell
uv run --package profile-unifier-ingestion pytest services/ingestion/tests/test_record_lifecycle.py -v
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src/models.py services/ingestion/src/graph/queries/source_records.py
uv run --package profile-unifier-ingestion ruff check services/ingestion/src/models.py services/ingestion/src/graph/queries/source_records.py services/ingestion/tests/test_record_lifecycle.py
```

Expected: all commands pass.

- [ ] **Step 7: Commit the lifecycle primitives**

```powershell
git add services/ingestion/src/models.py services/ingestion/src/graph/queries/source_records.py services/ingestion/src/graph/queries/__init__.py services/ingestion/src/graph/schema_init.py services/ingestion/tests/test_record_lifecycle.py
git commit -m "feat(ingestion): add source record lifecycle primitives"
```

### Task 2: Implement the lifecycle coordinator

**Files:**
- Create: `services/ingestion/src/record_lifecycle.py`
- Modify: `services/ingestion/tests/test_record_lifecycle.py`

- [ ] **Step 1: Write failing coordinator tests**

Add tests using a recording transaction that verify:

```python
def test_begin_returns_duplicate_for_active_or_pending_hash() -> None:
    state = SourceLifecycleState(
        active=_version("pk-1", 1, "hash-1", SourceRecordLifecycleStatus.ACTIVE),
        pending=_version("pk-2", 2, "hash-2", SourceRecordLifecycleStatus.PENDING_REVIEW),
        next_version=3,
    )
    assert classify_incoming_hash(state, "hash-1") == DuplicateVersion("pk-1")
    assert classify_incoming_hash(state, "hash-2") == DuplicateVersion("pk-2")


def test_new_hash_rejects_older_pending_but_keeps_active() -> None:
    transition = plan_incoming_version(_state_with_active_and_pending(), "hash-3")
    assert transition.version == 3
    assert transition.pending_to_reject == "pk-2"
    assert transition.active_source_record_pk == "pk-1"


def test_activate_requires_expected_active_version() -> None:
    tx = _ActivationTx(return_row=None)
    with pytest.raises(SourceLifecycleConflict):
        activate_staged_version(tx, old_source_record_pk="pk-1", new_source_record_pk="pk-2")
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `uv run --package profile-unifier-ingestion pytest services/ingestion/tests/test_record_lifecycle.py -v`

Expected: failures name the missing coordinator types and functions.

- [ ] **Step 3: Implement focused typed coordinator functions**

Keep functions under 50 lines and separate reads from transitions:

```python
@dataclass(frozen=True)
class DuplicateVersion:
    source_record_pk: str


@dataclass(frozen=True)
class PlannedVersion:
    version: int
    active_source_record_pk: str | None
    prior_person_ids: tuple[str, ...]
    pending_to_reject: str | None


def classify_incoming_hash(
    state: SourceLifecycleState,
    record_hash: str,
) -> DuplicateVersion | None:
    for version in (state.active, state.pending):
        if version is not None and version.record_hash == record_hash:
            return DuplicateVersion(version.source_record_pk)
    return None


def plan_incoming_version(
    state: SourceLifecycleState,
    record_hash: str,
) -> DuplicateVersion | PlannedVersion:
    duplicate = classify_incoming_hash(state, record_hash)
    if duplicate is not None:
        return duplicate
    return PlannedVersion(
        version=state.next_version,
        active_source_record_pk=(
            state.active.source_record_pk if state.active is not None else None
        ),
        prior_person_ids=(state.active.linked_person_ids if state.active is not None else ()),
        pending_to_reject=(
            state.pending.source_record_pk if state.pending is not None else None
        ),
    )
```

Add `load_locked_source_state`, `reject_replaced_pending`, and `activate_staged_version`. Parse Neo4j rows at this boundary and raise `SourceLifecycleConflict` when conditional transitions return no row.

- [ ] **Step 4: Run focused tests**

Run: `uv run --package profile-unifier-ingestion pytest services/ingestion/tests/test_record_lifecycle.py -v`

Expected: all coordinator unit tests pass.

- [ ] **Step 5: Run strict typing and lint**

Run:

```powershell
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src/record_lifecycle.py services/ingestion/tests/test_record_lifecycle.py
uv run --package profile-unifier-ingestion ruff check services/ingestion/src/record_lifecycle.py services/ingestion/tests/test_record_lifecycle.py
```

Expected: both commands pass.

- [ ] **Step 6: Commit the coordinator**

```powershell
git add services/ingestion/src/record_lifecycle.py services/ingestion/tests/test_record_lifecycle.py
git commit -m "feat(ingestion): coordinate immutable record versions"
```

### Task 3: Integrate identity ingestion and continuity matching

**Files:**
- Modify: `services/ingestion/src/pipeline.py`
- Modify: `services/ingestion/src/pipeline_writes.py`
- Modify: `services/ingestion/src/matching/engine.py`
- Test: `services/ingestion/tests/test_pipeline_person_merge.py`
- Test: `services/ingestion/tests/test_review_provisional_attach.py`
- Test: `services/ingestion/tests/test_record_update_continuity.py`

- [ ] **Step 1: Write failing identity update tests**

Create `test_record_update_continuity.py` covering:

```python
def test_review_update_does_not_retire_active_evidence() -> None:
    result, calls = ingest_changed_record(match_decision=MatchDecision.REVIEW)
    assert result.review_case_id is not None
    assert query_calls(calls, queries.RETIRE_IDENTITY_PROJECTIONS) == []
    assert query_calls(calls, queries.ACTIVATE_SOURCE_RECORD_VERSION) == []


def test_accepted_update_prefers_prior_person_and_recomputes_it() -> None:
    result, calls = ingest_changed_record(
        match_decision=MatchDecision.MERGE,
        prior_person_id="person-1",
    )
    assert result.person_id == "person-1"
    assert called_with(calls, queries.ACTIVATE_SOURCE_RECORD_VERSION)
    assert golden_profile_person_ids(calls) == {"person-1"}


def test_reassignment_recomputes_old_and_new_persons() -> None:
    _result, calls = ingest_changed_record(
        match_decision=MatchDecision.MERGE,
        prior_person_id="person-old",
        matched_person_id="person-new",
    )
    assert golden_profile_person_ids(calls) == {"person-old", "person-new"}
```

- [ ] **Step 2: Run the new tests and confirm current immediate supersession fails**

Run: `uv run --package profile-unifier-ingestion pytest services/ingestion/tests/test_record_update_continuity.py -v`

Expected: failures show `SUPERSEDE_SOURCE_RECORD` or evidence-retirement calls occurring before review resolution and missing prior-person recomputation.

- [ ] **Step 3: Stage records before active projection**

Change `persist_source_record` to require a lifecycle status and version key:

```python
def persist_source_record(
    tx: ManagedTransaction,
    *,
    envelope: SourceRecordEnvelope,
    lifecycle_status: SourceRecordLifecycleStatus,
    # existing typed arguments remain
) -> str:
    return _create_source_record(
        tx,
        envelope=envelope,
        lifecycle_status=lifecycle_status.value,
        source_version_key=(
            f"{envelope.source_system}|{envelope.source_record_id}|"
            f"{envelope.source_record_version}"
        ),
        # existing normalized payload arguments remain
    )
```

New records that can be accepted immediately start staged and transition through `ACTIVATE_FIRST_SOURCE_RECORD_VERSION`; review records remain `pending_review`.

- [ ] **Step 4: Add prior-Person continuity to matching**

Use a typed optional argument rather than mutating candidate order:

```python
def evaluate(
    self,
    tx: ManagedTransaction,
    candidates: list[CandidateResult],
    identifiers: list[NormalizedIdentifier],
    address: NormalizedAddress | None,
    attributes: list[NormalizedAttribute],
    *,
    record_type: RecordType,
    continuity_person_id: str | None = None,
) -> MatchResult:
```

Add `REASSIGNMENT_AUTO_THRESHOLD` as a named policy constant. If the best Person differs from `continuity_person_id` and does not meet that threshold without a sensitive-identifier conflict, return `REVIEW`; otherwise retain the existing deterministic match behavior.

- [ ] **Step 5: Extract identity projector activation and retirement**

Add one query that retires only edges carrying the old `source_record_pk` and a helper returning affected Persons:

```python
def retire_identity_projections(
    tx: ManagedTransaction,
    source_record_pk: str,
) -> tuple[str, ...]:
    rows = tx.run(
        queries.RETIRE_IDENTITY_PROJECTIONS,
        source_record_pk=source_record_pk,
    )
    return tuple(sorted({str(row["person_id"]) for row in rows}))
```

Only accepted transitions call this helper. Pending review continues to link the source record for review context but does not attach identifiers, addresses, facts, bankruptcy cases, or vehicle mentions.

- [ ] **Step 6: Activate atomically and recompute the complete affected set**

Build `affected_person_ids` from prior projection owners, selected Person, and additional linked Persons. Retire old projections, activate new projections, transition lifecycle state, then call `compute_golden_profile` once per sorted Person ID inside the same write transaction.

- [ ] **Step 7: Run focused identity tests**

Run:

```powershell
uv run --package profile-unifier-ingestion pytest services/ingestion/tests/test_record_update_continuity.py services/ingestion/tests/test_pipeline_person_merge.py services/ingestion/tests/test_review_provisional_attach.py -v
```

Expected: all tests pass.

- [ ] **Step 8: Run ingestion typing and lint for touched modules**

Run:

```powershell
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src/pipeline.py services/ingestion/src/pipeline_writes.py services/ingestion/src/matching/engine.py
uv run --package profile-unifier-ingestion ruff check services/ingestion/src/pipeline.py services/ingestion/src/pipeline_writes.py services/ingestion/src/matching/engine.py services/ingestion/tests/test_record_update_continuity.py
```

Expected: both commands pass.

- [ ] **Step 9: Commit identity lifecycle integration**

```powershell
git add services/ingestion/src/pipeline.py services/ingestion/src/pipeline_writes.py services/ingestion/src/matching/engine.py services/ingestion/src/graph/queries/source_records.py services/ingestion/tests/test_record_update_continuity.py services/ingestion/tests/test_pipeline_person_merge.py services/ingestion/tests/test_review_provisional_attach.py
git commit -m "feat(ingestion): preserve identity continuity on record updates"
```

### Task 4: Make review resolution activate or reject staged versions

**Files:**
- Modify: `services/api/src/graph/queries/review.py`
- Modify: `services/api/src/repositories/neo4j/review.py`
- Modify: `services/api/src/repositories/protocols/review.py`
- Test: `services/api/tests/test_review_repository_merge.py`
- Test: `services/api/tests/test_merge_review_side_effects.py`

- [ ] **Step 1: Write failing review lifecycle tests**

Add cases asserting that merge approval activates the pending SourceRecord and retires the expected active version, rejection marks only the pending version rejected, and defer changes no SourceRecord lifecycle state.

```python
def test_reject_update_keeps_previous_version_active() -> None:
    result = repository.resolve_review_case("case-1", action="reject", actor_id="user-1")
    assert result.resolution == "reject"
    assert graph.lifecycle("old-pk") == "active"
    assert graph.lifecycle("pending-pk") == "rejected"
```

- [ ] **Step 2: Run the review tests and verify lifecycle assertions fail**

Run: `uv run --package profile-unifier-api pytest services/api/tests/test_review_repository_merge.py services/api/tests/test_merge_review_side_effects.py -v`

Expected: new lifecycle assertions fail while existing review behavior remains visible.

- [ ] **Step 3: Add conditional review transition queries**

Extend approval queries to require `pending.lifecycle_status = 'pending_review'` and `old.lifecycle_status = 'active'`. Return both old and new Person IDs so the repository recomputes every affected golden profile. Extend rejection to set:

```cypher
SET pending.lifecycle_status = 'rejected',
    pending.rejection_reason = $reason,
    pending.resolved_at = datetime()
```

Do not change active projections on reject or defer.

- [ ] **Step 4: Reuse a shared activation contract**

If importing ingestion code into API would violate service boundaries, mirror the transition in an API query builder with the same named lifecycle states and add query-contract tests in both packages. Do not import `src.graph.*` from routes; keep the operation behind the review repository Protocol.

- [ ] **Step 5: Run focused API tests, typing, and lint**

Run:

```powershell
uv run --package profile-unifier-api pytest services/api/tests/test_review_repository_merge.py services/api/tests/test_merge_review_side_effects.py -v
uv run --package profile-unifier-api mypy --strict services/api/src/repositories services/api/src/graph/queries/review.py
uv run --package profile-unifier-api ruff check services/api/src/repositories services/api/src/graph/queries/review.py services/api/tests/test_review_repository_merge.py services/api/tests/test_merge_review_side_effects.py
```

Expected: all commands pass.

- [ ] **Step 6: Commit review lifecycle transitions**

```powershell
git add services/api/src/graph/queries/review.py services/api/src/repositories/neo4j/review.py services/api/src/repositories/protocols/review.py services/api/tests/test_review_repository_merge.py services/api/tests/test_merge_review_side_effects.py
git commit -m "feat(api): resolve pending record lifecycle reviews"
```

### Task 5: Adopt lifecycle coordination for address records

**Files:**
- Modify: `services/ingestion/src/pipeline_addresses.py`
- Modify: `services/ingestion/src/graph/queries/source_records.py`
- Test: `services/ingestion/tests/test_rental_flats_address_pipeline.py`

- [ ] **Step 1: Write failing changed-address tests**

```python
def test_changed_address_allocates_next_version_and_retires_old_assertion() -> None:
    result, calls = ingest_changed_address(old_pk="address-v1", old_version=1)
    assert created_source_version(calls) == "2"
    assert called_with(
        calls,
        queries.RETIRE_ADDRESS_PROJECTION,
        source_record_pk="address-v1",
    )
    assert result.skipped_duplicate is False
```

- [ ] **Step 2: Run the address tests and verify current version reuse fails**

Run: `uv run --package profile-unifier-ingestion pytest services/ingestion/tests/test_rental_flats_address_pipeline.py -v`

Expected: the new test fails because address ingestion only checks exact idempotency and leaves the envelope version unchanged.

- [ ] **Step 3: Use locked lifecycle planning and source-scoped retirement**

Replace `_check_idempotency` with `load_locked_source_state` inside the write transaction. Stage the new record, deactivate only the old `LINK_SOURCE_RECORD_TO_ADDRESS` assertion by `source_record_pk`, activate the new assertion, and transition lifecycle state before returning.

- [ ] **Step 4: Run address tests and touched-module checks**

Run:

```powershell
uv run --package profile-unifier-ingestion pytest services/ingestion/tests/test_rental_flats_address_pipeline.py -v
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src/pipeline_addresses.py
uv run --package profile-unifier-ingestion ruff check services/ingestion/src/pipeline_addresses.py services/ingestion/tests/test_rental_flats_address_pipeline.py
```

Expected: all commands pass.

- [ ] **Step 5: Commit address lifecycle adoption**

```powershell
git add services/ingestion/src/pipeline_addresses.py services/ingestion/src/graph/queries/source_records.py services/ingestion/tests/test_rental_flats_address_pipeline.py
git commit -m "fix(ingestion): version changed address records"
```

### Task 6: Make sales replacement activation atomic

**Files:**
- Modify: `services/ingestion/src/pipeline_sales.py`
- Modify: `services/ingestion/src/graph/queries/sales.py`
- Test: `services/ingestion/tests/test_sales_vehicle_matching.py`
- Test: `services/ingestion/tests/test_phppos_sales_loyalty.py`
- Test: `services/ingestion/tests/test_sales_record_updates.py`

- [ ] **Step 1: Write failing sales replacement tests**

Create `test_sales_record_updates.py` with:

```python
def test_unresolved_replacement_preserves_active_purchase() -> None:
    result, calls = ingest_changed_sale(customer_resolves=False)
    assert result.person_id is None
    assert query_calls(calls, queries.CLEAR_SUPERSEDED_SALES_LINKS) == []
    assert active_version(calls) == "sale-v1"


def test_accepted_replacement_removes_deleted_line_relationships() -> None:
    _result, calls = ingest_changed_sale(
        customer_resolves=True,
        previous_line_ids=("line-1", "line-2"),
        new_line_ids=("line-1",),
    )
    assert called_with(
        calls,
        queries.RETIRE_REMOVED_SALES_LINES,
        active_line_ids=["line-1"],
    )
```

- [ ] **Step 2: Run sales update tests and confirm eager cleanup fails**

Run: `uv run --package profile-unifier-ingestion pytest services/ingestion/tests/test_sales_record_updates.py -v`

Expected: failures show `CLEAR_SUPERSEDED_SALES_LINKS` runs before replacement customer resolution and removed lines are retained.

- [ ] **Step 3: Restrict identity resolution to accepted active versions**

Change `LINK_SALES_TO_IDENTITY_RECORD` to include:

```cypher
WHERE identity_sr.lifecycle_status = 'active'
```

Return exactly one identity record or an explicit ambiguity result; never attach a sale to multiple historical identity versions.

- [ ] **Step 4: Stage unresolved replacements without clearing active projections**

Use lifecycle planning inside the sales write transaction. If the replacement customer cannot resolve, keep the new SourceRecord `pending_review` or `link_failed` according to the existing malformed-versus-retriable distinction and leave the active sale projection untouched.

- [ ] **Step 5: Add accepted sales projection replacement**

On accepted activation, update the durable Order, upsert current lines, delete `CONTAINS` and source-scoped vehicle relationships absent from the accepted payload, refresh `PURCHASED`, then retire the prior SourceRecord and transition the new version active in one transaction.

The removal query receives typed current IDs:

```cypher
MATCH (o:Order {source_system_key: $source_system_key, source_order_id: $source_order_id})
OPTIONAL MATCH (o)-[contains:CONTAINS]->(li:LineItem)
WHERE NOT li.source_line_item_id IN $active_line_item_ids
DELETE contains
```

- [ ] **Step 6: Run focused sales tests**

Run:

```powershell
uv run --package profile-unifier-ingestion pytest services/ingestion/tests/test_sales_record_updates.py services/ingestion/tests/test_sales_vehicle_matching.py services/ingestion/tests/test_phppos_sales_loyalty.py -v
```

Expected: all tests pass.

- [ ] **Step 7: Run sales typing and lint**

Run:

```powershell
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src/pipeline_sales.py services/ingestion/src/graph/queries/sales.py
uv run --package profile-unifier-ingestion ruff check services/ingestion/src/pipeline_sales.py services/ingestion/src/graph/queries/sales.py services/ingestion/tests/test_sales_record_updates.py
```

Expected: both commands pass.

- [ ] **Step 8: Commit sales lifecycle adoption**

```powershell
git add services/ingestion/src/pipeline_sales.py services/ingestion/src/graph/queries/sales.py services/ingestion/tests/test_sales_record_updates.py services/ingestion/tests/test_sales_vehicle_matching.py services/ingestion/tests/test_phppos_sales_loyalty.py
git commit -m "fix(ingestion): activate sales replacements atomically"
```

### Task 7: Retire relationship, bankruptcy, and conversation projections

**Files:**
- Modify: `services/ingestion/src/pipeline_knows.py`
- Modify: `services/ingestion/src/pipeline_bankruptcy.py`
- Modify: `services/ingestion/src/pipeline.py`
- Modify: `services/ingestion/src/graph/queries/knows.py`
- Modify: `services/ingestion/src/graph/queries/bankruptcy.py`
- Modify: `services/ingestion/src/graph/queries/vehicle.py`
- Test: `services/ingestion/tests/test_pipeline_knows.py`
- Test: `services/ingestion/tests/test_bankruptcy_graph.py`
- Test: `services/ingestion/tests/test_record_update_projections.py`

- [ ] **Step 1: Write failing specialized projection tests**

```python
def test_knows_scans_active_records_only() -> None:
    assert "sr.lifecycle_status = 'active'" in queries.SCAN_CONTACT_SOURCE_RECORDS
    assert "sr.lifecycle_status = 'active'" in queries.SCAN_CHAT_RELATIONSHIP_SOURCE_RECORDS


def test_bankruptcy_reassignment_retires_old_person_association() -> None:
    _result, calls = ingest_changed_bankruptcy(prior_person="p1", new_person="p2")
    assert called_with(
        calls,
        queries.RETIRE_BANKRUPTCY_PROJECTION,
        source_record_pk="bankruptcy-v1",
    )


def test_superseded_conversation_retires_vehicle_mentions() -> None:
    _result, calls = ingest_changed_conversation()
    assert called_with(
        calls,
        queries.RETIRE_CONVERSATION_VEHICLE_MENTIONS,
        source_record_pk="chat-v1",
    )
```

- [ ] **Step 2: Run tests and verify missing active filters and retirement queries fail**

Run: `uv run --package profile-unifier-ingestion pytest services/ingestion/tests/test_record_update_projections.py services/ingestion/tests/test_pipeline_knows.py services/ingestion/tests/test_bankruptcy_graph.py -v`

Expected: the new contract assertions fail.

- [ ] **Step 3: Filter relationship scans and resolution to active versions**

Add `sr.lifecycle_status = 'active'` to contact and chat scans. When resolving a declarer by human-readable source ID, select the active version explicitly. Add `RETIRE_KNOWS_PROJECTION` that sets `is_active = false` for relationships with the superseded `source_record_pk`; set `is_active = true` on activation.

- [ ] **Step 4: Add bankruptcy activation and retirement**

Keep the durable `BankruptcyCase`, but deactivate the old Person association by `source_record_pk` before activating the replacement association. Add `is_active`, `activated_at`, and `retired_at` to the relationship rather than deleting historical provenance.

- [ ] **Step 5: Retire conversation vehicle mentions**

Add a source-scoped retirement query for `MENTIONS_VEHICLE` and invoke it only during accepted replacement activation. Pending conversations write no active vehicle mentions.

- [ ] **Step 6: Run specialized tests, typing, and lint**

Run:

```powershell
uv run --package profile-unifier-ingestion pytest services/ingestion/tests/test_record_update_projections.py services/ingestion/tests/test_pipeline_knows.py services/ingestion/tests/test_bankruptcy_graph.py -v
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src/pipeline_knows.py services/ingestion/src/pipeline_bankruptcy.py services/ingestion/src/pipeline.py
uv run --package profile-unifier-ingestion ruff check services/ingestion/src/pipeline_knows.py services/ingestion/src/pipeline_bankruptcy.py services/ingestion/src/pipeline.py services/ingestion/tests/test_record_update_projections.py
```

Expected: all commands pass.

- [ ] **Step 7: Commit specialized projector lifecycle support**

```powershell
git add services/ingestion/src/pipeline_knows.py services/ingestion/src/pipeline_bankruptcy.py services/ingestion/src/pipeline.py services/ingestion/src/graph/queries/knows.py services/ingestion/src/graph/queries/bankruptcy.py services/ingestion/src/graph/queries/vehicle.py services/ingestion/tests/test_pipeline_knows.py services/ingestion/tests/test_bankruptcy_graph.py services/ingestion/tests/test_record_update_projections.py
git commit -m "fix(ingestion): retire superseded sourced projections"
```

### Task 8: Migrate legacy data and align API reads

**Files:**
- Modify: `services/ingestion/src/graph/migrations.py`
- Modify: `services/api/src/graph/queries/persons.py`
- Modify: `services/api/src/types.py`
- Modify: `services/frontend2/src/lib/api-types-person.ts`
- Test: `services/ingestion/tests/test_migrations_record_lifecycle.py`
- Test: `services/api/tests/test_source_records_tab.py`
- Test: `services/api/tests/test_persons_list_queries.py`

- [ ] **Step 1: Write failing migration and active-read tests**

```python
def test_lifecycle_migration_marks_one_accepted_version_active() -> None:
    assert "lifecycle_status" in migrations.MIGRATE_SOURCE_RECORD_LIFECYCLE
    assert "pending_review" in migrations.MIGRATE_SOURCE_RECORD_LIFECYCLE
    assert "superseded" in migrations.MIGRATE_SOURCE_RECORD_LIFECYCLE


def test_person_reads_select_active_versions_explicitly() -> None:
    assert "lifecycle_status = 'active'" in person_queries.GET_PERSON_SOURCE_RECORDS
    assert "coalesce(sr.is_latest, true)" not in person_queries.GET_PERSON_SOURCE_RECORDS
```

- [ ] **Step 2: Run the migration and API query tests**

Run:

```powershell
uv run --package profile-unifier-ingestion pytest services/ingestion/tests/test_migrations_record_lifecycle.py -v
uv run --package profile-unifier-api pytest services/api/tests/test_source_records_tab.py services/api/tests/test_persons_list_queries.py -v
```

Expected: both new tests fail before implementation.

- [ ] **Step 3: Implement an idempotent lifecycle migration**

The migration groups records by SourceSystem and `source_record_id`, orders by numeric version and ingestion time, preserves unresolved reviewed replacements as `pending_review`, marks the last previously accepted version `active`, and marks older accepted versions `superseded`. Populate `source_version_key` for every record before applying the unique constraint in environments where migration order requires it.

- [ ] **Step 4: Align API reads and types**

Replace active-person read filters using `coalesce(is_latest, true)` with `lifecycle_status = 'active'`. Add a discriminated string type:

```python
SourceRecordLifecycleStatus = Literal[
    "active",
    "pending_review",
    "superseded",
    "rejected",
    "link_failed",
]
```

Mirror the same union in `services/frontend2/src/lib/api-types-person.ts` and add `lifecycle_status` to the existing source-record interface without using `Record<string, unknown>` or unsafe casts.

- [ ] **Step 5: Run migration, API, and frontend type tests**

Run:

```powershell
uv run --package profile-unifier-ingestion pytest services/ingestion/tests/test_migrations_record_lifecycle.py -v
uv run --package profile-unifier-api pytest services/api/tests/test_source_records_tab.py services/api/tests/test_persons_list_queries.py -v
uv run --package profile-unifier-api mypy --strict services/api/src/types.py services/api/src/graph/queries/persons.py
npm run typecheck --prefix services/frontend2
```

Expected: all commands pass.

- [ ] **Step 6: Commit migration and read-contract alignment**

```powershell
git add services/ingestion/src/graph/migrations.py services/ingestion/tests/test_migrations_record_lifecycle.py services/api/src/graph/queries/persons.py services/api/src/types.py services/api/tests/test_source_records_tab.py services/api/tests/test_persons_list_queries.py services/frontend2/src/lib/api-types-person.ts
git commit -m "feat: expose accepted source record lifecycle state"
```

### Task 9: Update architecture and API contracts

**Files:**
- Modify: `docs/profile-unifier-architecture.md`
- Modify: `docs/profile-unifier-api-spec.md`
- Modify: `docs/profile-unifier-openapi-3.1.yaml`

- [ ] **Step 1: Update architecture lifecycle prose and sequence**

Document the five lifecycle states, the one-active-version invariant, continuity matching, pending replacement behavior, and atomic projector transition. Add a Mermaid sequence showing ingestion, coordinator, matcher, projector, and Neo4j transaction boundaries.

- [ ] **Step 2: Update API prose and OpenAPI together**

Add `lifecycle_status` with the exact enum values to source-record responses. Clarify that `link_status` represents domain-link progress and does not determine which immutable version is accepted.

- [ ] **Step 3: Check documentation consistency**

Run:

```powershell
rg -n "is_latest|lifecycle_status|pending_review|link_status" docs/profile-unifier-architecture.md docs/profile-unifier-api-spec.md docs/profile-unifier-openapi-3.1.yaml
git diff --check
```

Expected: lifecycle language is consistent across all three files and `git diff --check` reports no errors.

- [ ] **Step 4: Commit documentation**

```powershell
git add docs/profile-unifier-architecture.md docs/profile-unifier-api-spec.md docs/profile-unifier-openapi-3.1.yaml
git commit -m "docs: define accepted source record lifecycle"
```

### Task 10: Hostile review and full local verification

**Files:**
- Review all files changed by Tasks 1-9.

- [ ] **Step 1: Inspect the complete branch diff for lifecycle leaks**

Run:

```powershell
git diff development...HEAD --stat
git diff development...HEAD
rg -n "coalesce\(sr\.is_latest|SUPERSEDE_SOURCE_RECORD|GET_LATEST_SOURCE_RECORD" services/ingestion/src services/api/src
```

Expected: no active-domain query relies on ambiguous `is_latest`; any retained legacy symbol is migration-only or explicitly documented.

- [ ] **Step 2: Perform the hostile review**

Check and correct:

- duplicate active versions under concurrent delivery;
- duplicate pending review cases for the same hash;
- evidence retirement before accepted activation;
- stale golden profiles after reassignment or multi-person projection;
- queries that omit `source_record_pk` scoping;
- sales line or vehicle relationships left behind after payload removal;
- historical records accidentally participating in matching or materialization;
- route-level graph access or API/ingestion service coupling;
- migrations that are not safe to rerun;
- brittle tests that assert incidental query order rather than lifecycle outcomes.

- [ ] **Step 3: Run all Python format, lint, and type checks**

Run:

```powershell
uv run ruff format --check services/api/src services/ingestion/src services/api/tests services/ingestion/tests
uv run ruff check services/api/src services/ingestion/src services/api/tests services/ingestion/tests
uv run --package profile-unifier-api mypy --strict services/api/src
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src
```

Expected: every command passes.

- [ ] **Step 4: Run all backend tests**

Run:

```powershell
uv run --package profile-unifier-api pytest services/api/tests
uv run --package profile-unifier-ingestion pytest services/ingestion/tests
```

Expected: both suites pass with no skipped lifecycle contract tests.

- [ ] **Step 5: Run active frontend checks**

Run:

```powershell
npm run typecheck --prefix services/frontend2
npx --prefix services/frontend2 eslint services/frontend2/src --quiet
npm run build --prefix services/frontend2
```

Expected: typecheck, errors-only ESLint, and production build pass. Do not run checks in retired `services/frontend/`.

- [ ] **Step 6: Verify the final worktree and commits**

Run:

```powershell
git diff --check
git status -sb
git log --oneline --decorate development..HEAD
```

Expected: no uncommitted implementation changes, no diff-check errors, and only the planned lifecycle commits appear above `development`.

- [ ] **Step 7: Push and inspect Woodpecker only if separately authorized**

Do not push, open a PR, or access remote CI without explicit user authorization. After an authorized PR-branch push, run:

```powershell
wpci home doctor --json
wpci home pipeline last sparkfn/hyperP --branch feat/record-update-handling
wpci home pipeline show sparkfn/hyperP <pipeline-number>
```

Expected: record repository, branch, commit SHA, pipeline number, final status, and every step name. A missing, skipped, or failed required step blocks completion.
