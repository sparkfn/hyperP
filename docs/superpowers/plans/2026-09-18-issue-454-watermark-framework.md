# Implementation Plan: Issue #454 — Core Watermark Framework and Scheduler Simplification

Date: 2026-09-18
Branch: `issue-454-core-watermark-framework`
Design spec: `docs/superpowers/specs/2026-09-18-watermark-ingestion-model-design.md`

## 1. Intended Behavior & Acceptance Criteria Mapping

This issue introduces the core watermark ingestion infrastructure that replaces the bounded ingestion framework (#430) and the durable scheduler workflow (#431). All per-source connector migrations (#432–#437) depend on this foundation.

| Acceptance criterion | Plan section |
|---|---|
| `IncrementalConnector` protocol and `IngestionWatermark` store implemented and tested | Steps 1–2, Tests A–B |
| Watermark runner handles bootstrap, delta, safe stop (window/SIGTERM), and failure | Step 3, Tests C |
| Scheduler dispatches via simple Redis-locked task, no Neo4j workflow state | Step 4, Tests D |
| Existing dump connectors continue to work unchanged | Step 5 (routing), Tests E |
| `IngestRun` nodes created with `watermark_start`/`watermark_end` fields | Step 3 (runner creates/finalizes runs) |

## 2. Files to Create, Modify, and Delete

### New files (ingestion service)

| File | Purpose | Est. lines |
|---|---|---|
| `services/ingestion/src/watermark_store.py` | `IngestionWatermark` dataclass + Redis load/save/reset | ~60 |
| `services/ingestion/src/incremental_connector.py` | `IncrementalConnector` protocol + `IncrementalPage` dataclass | ~40 |
| `services/ingestion/src/watermark_runner.py` | cursor-drain-then-advance loop, safe stop, IngestRun tracking | ~200 |
| `services/ingestion/src/watermark_scheduler.py` | Simplified group dispatch task: config check, window check, Redis lock, dispatch | ~120 |
| `services/ingestion/tests/test_watermark_store.py` | Unit tests for `IngestionWatermark` | ~80 |
| `services/ingestion/tests/test_incremental_connector.py` | Protocol structural tests | ~30 |
| `services/ingestion/tests/test_watermark_runner.py` | Runner loop tests with fakes | ~250 |
| `services/ingestion/tests/test_watermark_scheduler.py` | Scheduler dispatch tests | ~150 |

### Modified files

| File | Change |
|---|---|
| `services/ingestion/src/tasks.py` | Add `run_incremental_task` Celery task; add incremental registry check to `run_ingestion_task` that routes registered sources to the watermark runner, falling back to existing path for dump/non-incremental sources |
| `services/ingestion/src/celery_app.py` | Register new task module in `include` list; add task route for `run_incremental_task` to `INGESTION_QUEUE`. Beat schedule entries stay unchanged — the new simplified dispatch is not wired to beat in this PR |
| `services/ingestion/src/scheduled_ingestion_groups.py` | Remove import of `BoundedMode` from `bounded_ingestion_models`; `mode_for` returns a simple `Literal["bootstrap", "delta"]` instead |
| `services/ingestion/src/connectors/base.py` | No change (SourceConnector stays for dump connectors) |
| `services/ingestion/src/graph/queries/source_records.py` | Add `watermark_start` and `watermark_end` optional fields to `CREATE_INGEST_RUN` and `CREATE_OR_REUSE_WORKER_INGEST_RUN` Cypher; add to `UPDATE_INGEST_RUN` |
| `services/ingestion/src/graph/queries/__init__.py` | Export any new query constants |
| `services/ingestion/src/main.py` | Pass `watermark_start`/`watermark_end` params through `finalize_ingest_run` |

### Files NOT deleted in this PR (deferred to cleanup after all sources migrated)

The bounded ingestion modules, `scheduled_ingestion_control.py`, `scheduled_ingestion_tasks.py`, and related Neo4j query files remain intact. Removing them requires all connectors to be migrated first (#432–#437). The new watermark infrastructure is additive — it runs alongside the existing bounded path until migration is complete.

## 3. Ordered Implementation Steps

### Step 1: `IngestionWatermark` dataclass + Redis store

**File:** `services/ingestion/src/watermark_store.py`

```python
@dataclass(frozen=True)
class IngestionWatermark:
    updated_at: datetime | None    # None = bootstrap
    source_key: str
    entity_key: str | None
```

Methods (module-level functions operating on a `redis.Redis` client):
- `load_watermark(client, source_key, entity_key) -> IngestionWatermark` — read from `profile_unifier:watermark:{source_key}[:{entity_key}]`, parse JSON `{"updated_at": "..."}`, return `IngestionWatermark` with `updated_at=None` if key absent.
- `save_watermark(client, watermark) -> None` — serialize `updated_at` as UTC ISO string, write to Redis. Raise if `updated_at is None` (save is only called after a successful drain).
- `reset_watermark(client, source_key, entity_key) -> None` — delete the Redis key (forces next run to bootstrap).

Redis key format: `profile_unifier:watermark:{source_key}` when `entity_key` is `None`, else `profile_unifier:watermark:{source_key}:{entity_key}`.

The JSON value is `{"updated_at": "2026-09-18T10:30:00+00:00"}`. Minimal — no extra fields. All datetimes stored as UTC ISO with timezone info.

**Typing:** use `redis.Redis` Protocol-compatible interface (same pattern as `fundbox_api/checkpoints.py` — accept a `Protocol` with `get`/`set`/`delete` methods).

### Step 2: `IncrementalConnector` protocol + `IncrementalPage` dataclass

**File:** `services/ingestion/src/incremental_connector.py`

```python
class IncrementalConnector(Protocol):
    def open_query(self, updated_since: datetime | None) -> None: ...
    def fetch_next_page(self) -> IncrementalPage: ...
    def get_source_key(self) -> str: ...
    def close(self) -> None: ...

@dataclass(frozen=True)
class IncrementalPage:
    records: tuple[dict[str, JsonValue], ...]
    has_more: bool
    max_updated_at: datetime
```

This is a `typing.Protocol` (not an ABC), consistent with the repository pattern (`PersonRepository`, etc.). The protocol is stateful: `open_query` initializes internal cursor state, `fetch_next_page` advances the cursor, `close` releases resources.

Each record dict in `IncrementalPage.records` is a raw source record matching the `SourceRecordEnvelope` shape or a connector-specific dict that the existing pipeline can process.

### Step 3: Watermark runner

**File:** `services/ingestion/src/watermark_runner.py`

Core function signature:
```python
def run_incremental(
    connector: IncrementalConnector,
    redis_client: redis.Redis,
    graph_client: Neo4jClient,
    *,
    entity_key: str | None = None,
    shutdown_signal: Callable[[], bool],
    time_window_closing: Callable[[], bool],
    control_instance_id: str = LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
) -> IncrementalRunSummary:
```

**`IncrementalRunSummary`** (TypedDict):
```python
class IncrementalRunSummary(TypedDict):
    ingest_run_id: str
    status: str           # "caught_up" | "yielded" | "failed"
    records_processed: int
    pages_processed: int
    watermark_start: str | None   # ISO datetime or None (bootstrap)
    watermark_end: str | None     # ISO datetime or None (not advanced)
    source_key: str
    entity_key: str | None
```

**Algorithm (cursor-drain-then-advance):**

1. Load watermark from Redis via `load_watermark`.
2. Create `IngestRun` node in Neo4j with `watermark_start = watermark.updated_at` (ISO string or `null`), `status = "running"`, `mode = "bootstrap"` or `"delta"`.
3. Call `connector.open_query(updated_since=watermark.updated_at)`.
4. Initialize `max_updated_at = watermark.updated_at`, `records_processed = 0`, `pages_processed = 0`.
5. Loop:
   a. Check `time_window_closing()` or `shutdown_signal()`. If true: finalize IngestRun as `"yielded"` (watermark NOT advanced), close connector, return.
   b. `page = connector.fetch_next_page()`.
   c. Process `page.records`: for each raw record, build `SourceRecordEnvelope.model_validate({"source_system": connector.get_source_key(), **raw_record})`, check exclusions via `_record_is_excluded`, and route through `_process_record(client, pipeline, envelope, ingest_run_id, exclusion_context, control_instance_id=control_instance_id)` where `pipeline` is an `IngestPipeline(client, control_instance_id=control_instance_id)` created once at the start of the run. This mirrors the existing record loop in `run_ingestion` (lines 931–948 of `main.py`) without calling the monolithic `run_ingestion` function, which manages its own IngestRun lifecycle and cannot be interrupted at page boundaries.
   d. Update `max_updated_at = max(max_updated_at, page.max_updated_at)` (handling `None` initial case).
   e. Increment `pages_processed += 1`, `records_processed += len(page.records)`.
   f. If `not page.has_more`: advance watermark to `max_updated_at`, save to Redis, finalize IngestRun as `"caught_up"` with `watermark_end = max_updated_at`, return.
6. On exception: finalize IngestRun as `"failed"` (watermark NOT advanced), close connector, re-raise.

**Safe stop semantics:**

- `time_window_closing()`: checks `datetime.now(ZoneInfo("Asia/Singapore")).hour >= 23` (same 09:00–23:00 SGT window as existing).
- `shutdown_signal()`: set by a Celery worker signal handler for SIGTERM/worker-shutdown, same pattern as `bounded_shutdown_signal()` in `bounded_ingestion_task_runtime.py`.
- Both are injected as callables so the runner is testable without Celery.

**IngestRun creation:**

Use the existing `CREATE_OR_REUSE_WORKER_INGEST_RUN` query pattern (keyed by `worker_task_id`) extended with `watermark_start` and `watermark_end` optional properties. The runner creates the run at the start and finalizes it with `UPDATE_INGEST_RUN` (extended to accept `watermark_end`).

The mode is `"bootstrap"` when `watermark.updated_at is None`, `"delta"` otherwise.

### Step 4: Simplified scheduler

**File:** `services/ingestion/src/watermark_scheduler.py`

Replace the durable Neo4j workflow dispatch with a simple Celery task:

```python
def dispatch_incremental_group(
    group: ScheduledIngestionGroup,
    now: datetime,
) -> ScheduledGroupDispatchSummary:
```

Logic:
1. Check `get_ingestion_config().scheduled_ingestion.enabled`. If false, return `"disabled"`.
2. Check time window: convert `now` to SGT, verify `9 <= hour < 23` on the group's weekday. If outside, return `"window_closed"`.
3. For each spec in `group.tasks` (sequentially, preserving ordering):
   a. Check Redis per-source lock (`_SOURCE_LOCK_PREFIX:{source_key}[:{entity_key}]`). If locked, skip (already running).
   b. Dispatch `run_incremental_task.delay(spec.source_key, entity_key=spec.entity_key)`.
4. Return `"published"` with the count of dispatched tasks.

**Key simplifications vs current scheduler:**
- No Neo4j `ScheduledIngestionControl` node or durable workflow.
- No `OccurrenceContext`, occurrence binding, rebinding, or generation-bound safety.
- No `SchedulerReadiness` protocol or readiness gates.
- No `claim_current_child` / `confirm_child_publication` / `reconcile_current_child`.
- No Bitrix successor dispatch (stays in the old path until Bitrix is migrated).
- One-child-at-a-time ordering is preserved by iterating specs sequentially; concurrency guard is the Redis per-source lock.

**Celery Beat schedule change in `celery_app.py`:**

The existing beat entries for `dispatch_ingestion_group_task` stay as-is initially. The new simplified dispatch is registered under a separate task name (`src.watermark_scheduler.dispatch_watermark_group_task`). Once all sources are migrated, the old beat entries are removed and the new ones take over. For this PR, add the new task but gate it behind a config flag or simply don't add it to the beat schedule yet — the dispatch is callable manually or by future per-source migration PRs.

**Decision:** For this foundational PR, the simplified scheduler is implemented as a callable module but NOT wired into the Celery Beat schedule. The beat schedule change happens when the first source is migrated to `IncrementalConnector` and the old durable workflow path is no longer needed for that group. This avoids a big-bang cutover.

### Step 5: Task dispatch and routing

**File:** `services/ingestion/src/tasks.py`

Add a new Celery task `run_incremental_task`:

```python
@celery_app.task(
    name="src.tasks.run_incremental_task",
    bind=True,
    acks_late=True,
    autoretry_for=(_SlotUnavailableError,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=None,
)
def run_incremental_task(
    self: Task,
    source_key: str,
    entity_key: str | None = None,
) -> IncrementalRunSummary:
    ...
```

**Routing logic inside `run_incremental_task`:**
1. Look up whether `source_key` has a registered `IncrementalConnector` (via a new lightweight registry, `INCREMENTAL_CONNECTORS: dict[str, Callable[..., IncrementalConnector]]`). If not found, raise `Reject` — the source must be dispatched through the existing `run_ingestion_task`.
2. Acquire ingestion slot (`_acquire_ingestion_slot`).
3. Acquire source locks (`_acquire_source_locks`).
4. Set up shutdown signal handler.
5. Call `run_incremental(connector, redis_client, graph_client, ...)`.
6. On success, enqueue lifecycle reconciliation and KNOWS materialization (same post-run hooks as existing `run_ingestion_task`).

**Modify `run_ingestion_task` to route incremental sources (issue #454, Code Changes #5):**

Add an incremental registry check near the top of `run_ingestion_task`, before the existing dump/batch/bounded dispatch:

```python
# In run_ingestion_task, after argument validation:
if source_key in INCREMENTAL_CONNECTORS and mode != "dump":
    # Delegate to the watermark path — re-dispatch as run_incremental_task.
    return run_incremental_task.delay(source_key, entity_key=entity_key)
```

This makes `run_ingestion_task` the universal entry point: callers (including the existing scheduler) don't need to know whether a source is incremental. Sources not in the registry, and all `mode="dump"` calls, fall through to the existing path unchanged. Dump-only sources (`sgbankruptcy`, `sgrentalflats`, `onediver`) and all currently-working batch/API connectors continue through the existing code.

### Step 6: IngestRun graph schema extension

**Files:** `services/ingestion/src/graph/queries/source_records.py`, `__init__.py`

Add optional properties to `CREATE_INGEST_RUN` and `CREATE_OR_REUSE_WORKER_INGEST_RUN`:
- `watermark_start: $watermark_start` (nullable datetime/string)
- `watermark_end: $watermark_end` (nullable datetime/string, set on finalization)

Extend `UPDATE_INGEST_RUN` to accept `watermark_end`.

**Backwards compatibility:** `finalize_ingest_run` gains a `watermark_end: str | None = None` keyword parameter with a default of `None`. Existing callers (dump path, bounded path) pass `None` implicitly and are unaffected. Only the watermark runner passes a non-`None` value on successful drain.

These are additive optional properties on existing `IngestRun` nodes — no schema migration needed. Existing runs (from dump/bounded paths) simply have `null` watermark fields.

## 4. Validation and Testing Strategy

### Test A: `test_watermark_store.py`

Unit tests (no Redis/Neo4j required — use a fake in-memory dict):
- `test_load_returns_none_when_absent` — bootstrap case.
- `test_save_and_load_roundtrip` — write a watermark, read it back, verify datetime.
- `test_save_rejects_none_updated_at` — cannot save a bootstrap watermark.
- `test_reset_removes_watermark` — after reset, load returns `None`.
- `test_entity_key_in_redis_key` — verify key format with and without entity_key.
- `test_timezone_awareness` — verify stored/loaded datetimes are UTC-aware.

### Test B: `test_incremental_connector.py`

Structural typing tests:
- `test_protocol_satisfied_by_fake` — a minimal fake class satisfies `IncrementalConnector`.
- `test_incremental_page_frozen` — verify `IncrementalPage` is immutable.

### Test C: `test_watermark_runner.py`

Tests with a fake `IncrementalConnector` and in-memory Redis/Neo4j stubs:

- **Bootstrap (no prior watermark):**
  - `test_bootstrap_drains_all_pages_and_advances_watermark` — connector returns 3 pages then `has_more=False`; verify watermark advanced to max `updated_at`, IngestRun status = `"caught_up"`, mode = `"bootstrap"`.

- **Delta (existing watermark):**
  - `test_delta_passes_updated_since_to_connector` — verify `open_query` called with the existing watermark datetime.
  - `test_delta_advances_watermark_on_drain` — verify watermark updated to new max.

- **Safe stop (window closing):**
  - `test_window_closing_yields_without_advancing_watermark` — `time_window_closing` returns `True` after 1 page; verify watermark NOT advanced, IngestRun status = `"yielded"`.

- **Safe stop (SIGTERM):**
  - `test_shutdown_signal_yields` — `shutdown_signal` returns `True`; verify same yielded behavior.

- **Failure:**
  - `test_exception_in_fetch_marks_run_failed` — connector raises during `fetch_next_page`; verify IngestRun status = `"failed"`, watermark NOT advanced, exception re-raised.
  - `test_exception_in_pipeline_marks_run_failed` — graph write fails; same.

- **IngestRun tracking:**
  - `test_ingest_run_has_watermark_start_and_end` — verify `watermark_start` set on creation, `watermark_end` set on finalization (caught_up only).
  - `test_ingest_run_pages_and_records_counted` — verify `records_processed` and `pages_processed` match actual page data.

- **Edge cases:**
  - `test_empty_first_page_is_caught_up` — source returns no records; watermark stays the same, status = `"caught_up"`.
  - `test_max_updated_at_tracks_across_pages` — pages with out-of-order `max_updated_at`; verify the overall max is used.

### Test D: `test_watermark_scheduler.py`

Tests with fakes:
- `test_disabled_scheduling_returns_disabled` — config flag off.
- `test_outside_window_returns_window_closed` — wrong weekday or hour.
- `test_dispatches_all_group_specs` — verify `run_incremental_task.delay` called for each spec.
- `test_skips_locked_source` — source lock exists; that spec skipped, others dispatched.
- `test_sequential_ordering_preserved` — verify specs dispatched in group order.

### Test E: Existing dump connector regression

No new test needed — existing tests for dump connectors (`test_connectors.py`, etc.) continue to pass because `run_ingestion_task` routes only registered incremental sources and falls through for everything else. New test modules are picked up by the CI active profile automatically (per `ci_support/selection_manifest.py` line 13: "New tests are active by default").

### CI integration

All new tests are pure-unit (no Neo4j, no Redis, no Docker) — they use in-memory fakes. No CI configuration changes needed.

## 5. Risks, Assumptions, and Open Questions

### Assumptions

1. **Additive, not replacement.** This PR adds the watermark infrastructure alongside the bounded path. No existing code paths are removed or broken. Per-source migration happens in subsequent PRs (#432–#437).

2. **No connectors migrated in this PR.** The `IncrementalConnector` protocol is defined but no existing connector implements it yet. The first connector migration (likely Fundbox API, #433) will be a separate PR that registers a `FundboxIncrementalConnector`.

3. **Beat schedule unchanged.** The Celery Beat schedule continues to use `dispatch_ingestion_group_task` for all groups. The new `dispatch_watermark_group_task` is implemented but not wired to beat until the first source migrates.

4. **IngestRun schema is additive.** Adding optional properties (`watermark_start`, `watermark_end`) to IngestRun nodes does not require a migration — Neo4j properties are schema-less.

### Risks

| Risk | Mitigation |
|---|---|
| Watermark runner's record processing needs to integrate with existing pipeline | Runner creates its own `IngestPipeline` and calls `_process_record` per record (same per-record loop as `run_ingestion` lines 931–948). This avoids calling the monolithic `run_ingestion`, which manages its own IngestRun lifecycle and cannot be interrupted at page boundaries. |
| New tests inflate CI time | All tests are pure-unit; no graph or Redis fixtures. Should add < 5s to CI. |
| Dual-path complexity during migration | Accepted — bounded path is untouched and continues working. Once all sources are migrated, a cleanup PR removes bounded modules. |

### Open Questions

1. **Lifecycle / KNOWS post-hooks:** The watermark runner should trigger the same post-ingestion hooks (lifecycle reconciliation, KNOWS materialization) as the existing task. This is handled in `run_incremental_task` (Step 5), same as existing `run_ingestion_task`.

2. **Bitrix chat group special-casing:** The current `dispatch_ingestion_group_task` has special handling for the `bitrix_chat` group (successor dispatch). The new simplified scheduler does NOT replicate this — the Bitrix group stays on the old durable workflow path until Bitrix connectors are migrated. The new scheduler only dispatches groups whose specs have registered `IncrementalConnector`s.

## 6. Implementation Order (for agents)

| Order | Step | Dependency |
|---|---|---|
| 1 | `watermark_store.py` + `test_watermark_store.py` | None |
| 2 | `incremental_connector.py` + `test_incremental_connector.py` | None |
| 3 | Graph schema extension (Cypher queries for `watermark_start`/`watermark_end`) | None |
| 4 | `watermark_runner.py` + `test_watermark_runner.py` | Steps 1, 2, 3 |
| 5 | `watermark_scheduler.py` + `test_watermark_scheduler.py` | Step 2 |
| 6 | `run_incremental_task` in `tasks.py` + `run_ingestion_task` routing + `celery_app.py` registration | Steps 4, 5 |

Steps 1, 2, and 3 can be implemented in parallel. Steps 4 and 5 depend on 1–3. Step 6 depends on 4–5.
