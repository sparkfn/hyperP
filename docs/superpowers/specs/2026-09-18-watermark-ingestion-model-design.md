# Watermark Ingestion Model Design

Date: 2026-09-18

## Purpose

Replace the bounded ingestion orchestration layer (~5,200 lines, 17 files,
6 Neo4j node types) with a simpler high-water-mark (HWM) model. Each source
tracks a single watermark — the last successfully committed `updated_at`
timestamp — and each run queries forward from that point. Safe stop is
preserved: the run checks the time window between pages and stops gracefully,
resuming from the last committed watermark on the next scheduled tick.

Source API changes (adding `updated_since` query parameters) are acceptable.
Source database migrations are not.

## Problem with the Current Model

The bounded ingestion framework (#429/#430) was designed for exact-resume with
zero re-work after arbitrary interruption. It requires:

- **Frozen snapshot upper bounds** on the source API — the connector must
  guarantee no backdated inserts within the query window. For sources like
  Fundbox (MySQL) or PHPPOS, this implies DB-level snapshot isolation or
  change-version columns, requiring schema migrations on systems we don't own.
- **One-unit-at-a-time execution** — each Celery task delivery fetches exactly
  one page, commits it, then returns. The next delivery resumes. This demands
  6 Neo4j node types for fencing, leasing, receipts, reservations, retries,
  and budget accounting.
- **~5,200 lines across 17 files** for orchestration that wraps connectors
  already using simple watermark patterns internally.

The open children (#432–#437) are each blocked on "needs upstream API change"
precisely because the bounded contract demands snapshot capabilities the sources
don't have and shouldn't be forced to add via DB migration.

## Design

### Core: cursor-drain-then-advance loop

The watermark is an **inter-run bookmark** — it records where the last
successful run left off. Within a run, the source API's own cursor handles
pagination. The watermark only advances when a full query is drained to
exhaustion.

```
load watermark (updated_at) from Redis
open query: source.query(updated_since=watermark.updated_at)
max_updated_at = watermark.updated_at
loop:
    if time_window_closing() or shutdown_signal():
        break  # safe stop — watermark NOT advanced (will re-process from W)
    page = source.fetch_next_page(cursor)   # uses source API's own cursor
    if page is empty:
        advance watermark to max_updated_at
        persist watermark to Redis
        mark run as caught_up
        break
    commit page records to graph (existing ingestion pipeline)
    max_updated_at = max(max_updated_at, page's latest updated_at)
    if not page.has_more:
        advance watermark to max_updated_at
        persist watermark to Redis
        mark run as caught_up
        break
```

Two outcomes:
- **Caught up** — source cursor exhausted, watermark advanced, next run starts
  from new position.
- **Yielded** (safe stop) — watermark NOT advanced, next run replays from the
  last committed watermark. Re-processing is bounded (only records since W)
  and idempotent (source records are upserted by source key).

This separation — source cursor for intra-run pagination, watermark for
inter-run position — avoids the timestamp-cluster stalling problem entirely.
The source API's cursor handles same-timestamp records naturally (it knows its
own ordering), and the watermark never needs an exclusion set.

### Watermark structure

```python
@dataclass(frozen=True)
class IngestionWatermark:
    updated_at: datetime | None      # None = bootstrap (no prior run)
    source_key: str
    entity_key: str | None
```

Stored in Redis at `profile_unifier:watermark:{source_key}[:{entity_key}]` as
JSON: `{"updated_at": "2026-09-18T10:30:00Z"}`.

- **Bootstrap** (no watermark / `updated_at=None`): query with no
  `updated_since` filter — fetches everything from the beginning.
- **Delta** (watermark exists): query with
  `updated_since=watermark.updated_at`. The source API's `>=` semantics mean
  records at exactly the watermark timestamp are re-fetched — this is a small,
  bounded overlap and ingestion is idempotent.

### Why no exclusion set

The source API's own cursor guarantees forward progress within a query. The
watermark only advances after a full drain, so there's no partial-page state
to track. On safe stop, the entire query restarts from the watermark — some
re-processing, but:
- Re-processing is idempotent (upsert by source key).
- The overlap is bounded: at most the records processed since the last
  watermark, which is one run's worth.
- No same-timestamp stalling: the cursor walks past timestamp clusters
  naturally.

### Safe stop and re-processing budget

On safe stop (time window closing or SIGTERM), the watermark is NOT advanced.
The next run replays from the last committed watermark. This means some records
are re-processed, but:

1. **Re-processing is idempotent** — source records are upserted by source key;
   identifiers and relationships are matched idempotently.
2. **The overlap is bounded** — at most one run's worth of records (the pages
   committed since the last watermark advance). In practice this is small:
   weekly runs process a week's changes, and a safe stop near the window cutoff
   means at most a few hours of changes are replayed.
3. **No accumulated drift** — the watermark only advances on full drain, so
   partial runs never leave a gap.

### Safe stop

The time-window check runs between cursor pages. The check is simple:

```python
def time_window_closing(self) -> bool:
    now = datetime.now(tz=ZoneInfo("Asia/Singapore"))
    return now.hour >= 23 or self._shutdown_requested
```

This preserves the existing weekly schedule semantics (09:00–23:00 SGT per
group's weekday) without occurrence binding, lease fencing, or budget
reservations.

The `_shutdown_requested` flag is set by a Celery signal handler (SIGTERM /
worker shutdown), same as today.

On safe stop, the watermark is NOT advanced — the next run replays from the
last committed watermark, re-processing at most one run's worth of records
(bounded, idempotent).

### Connector interface

The existing `SourceConnector` (dump) and `BoundedConnector` (bounded) are
replaced by `IncrementalConnector`. The connector wraps the source API's own
cursor and exposes one page at a time:

```python
class IncrementalConnector(Protocol):
    def open_query(
        self,
        updated_since: datetime | None,
    ) -> None: ...

    def fetch_next_page(self) -> IncrementalPage: ...

    def get_source_key(self) -> str: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class IncrementalPage:
    records: tuple[dict[str, JsonValue], ...]
    has_more: bool             # source API's cursor has more pages
    max_updated_at: datetime   # latest updated_at in this page
```

- `open_query(updated_since=None)` starts a bootstrap (no filter).
- `open_query(updated_since=W)` starts a delta query from watermark W.
- `fetch_next_page()` returns the next cursor page from the source API.
  The connector manages the source API's cursor internally (e.g. Fundbox's
  `next_cursor`, PHPPOS's `next_cursor`, Bitrix's offset).
- `has_more=False` means the source cursor is exhausted — the query is fully
  drained and the watermark can advance.
- Each record dict must include `updated_at` (ISO string) and a stable `id`.

Dump connectors keep the existing `SourceConnector` interface — they process
the entire file in one pass and don't need watermarks.

### Run tracking

`IngestRun` nodes remain for UI/monitoring. Each run records:

- `source_key`, `entity_key`, `mode` (bootstrap / delta / dump)
- `started_at`, `completed_at`
- `status` (running / caught_up / yielded / failed)
- `records_processed`, `pages_processed`
- `watermark_start`, `watermark_end` (the watermark before and after the run)

No `IngestionLogicalRun` bounded fields, no `BoundedIngestionScope`, no slots,
reservations, receipts, or retries.

### Scheduler simplification

The scheduler reduces from a durable Neo4j workflow to:

1. **Celery Beat** fires per group on its weekday at 01:00 UTC (unchanged).
2. The task checks:
   - Is scheduling enabled? (config switch, unchanged)
   - Is the group's time window open? (simple clock check)
   - Is a run already in progress for this source? (Redis lock)
3. If clear, dispatch `run_incremental_task.delay(source_key, entity_key)`.
4. Group ordering (fundbox customers before fundbox sales, etc.) is enforced
   by running specs sequentially within the task — not by a durable workflow.

The `ScheduledIngestionControl` Neo4j node, occurrence binding, rebinding,
generation-bound safety, and obligation coalescing are removed. The Redis lock
per source is the only concurrency guard needed.

### Concurrency control

- **Per-source Redis lock** — prevents two runs of the same source overlapping.
  Already exists.
- **Global semaphore** — the existing Redis semaphore (`_INGEST_SEMAPHORE_KEY`,
  cap 2) limits total concurrent ingestion tasks. Stays as-is.
- **Neo4j global slots** (`BoundedIngestionGlobalSlot`) — removed. The Redis
  semaphore is sufficient.

### Retry on failure

If a run fails mid-page (API error, graph write failure):

1. The watermark is NOT advanced (it stays at the last successfully committed
   page).
2. The run is marked `failed` with the error.
3. The next scheduled tick retries from the same watermark — automatic,
   no durable retry obligations needed.

Transient API errors within a page can be retried inline (simple backoff loop
within `fetch_page`), not as separate Neo4j retry obligations.

## What Gets Removed

### Neo4j node types (6)

| Node type | Purpose | Replacement |
|---|---|---|
| `BoundedIngestionScope` | Scope key for bounded runs | None (Redis watermark key) |
| `IngestionLogicalRun` (bounded fields) | Checkpoint, lease, budget, occurrence | Redis watermark + IngestRun |
| `BoundedIngestionGlobalSlot` | Graph concurrency cap | Redis semaphore (exists) |
| `BoundedUsageReservation` | Per-unit budget reservation | None |
| `BoundedIngestionReceipt` | Idempotent commit proof | None (watermark is the proof) |
| `BoundedIngestionRetry` | Durable retry obligation | None (next tick retries) |

### Modules (~5,200 lines across 17 files)

| File | Lines | Purpose |
|---|---|---|
| `bounded_ingestion_models.py` | ~370 | Scope, context, unit, connector protocol |
| `bounded_ingestion_runner.py` | ~150 | One-unit executor |
| `bounded_ingestion_commit.py` | ~200 | Commit store |
| `bounded_ingestion_dispatch.py` | ~250 | Dispatch logic |
| `bounded_ingestion_task_runtime.py` | ~300 | Celery task runtime |
| `bounded_ingestion_budget.py` | ~200 | Budget accounting |
| `bounded_ingestion_retry.py` | ~250 | Durable retry |
| `bounded_ingestion_window.py` | ~200 | Occurrence windows |
| `graph/bounded_ingestion_control.py` | ~800 | Neo4j lifecycle + commit |
| `graph/bounded_ingestion_records.py` | ~150 | Record mapping |
| `graph/bounded_ingestion_schema.py` | ~100 | Neo4j schema/indexes |
| `graph/queries/bounded_ingestion_control.py` | ~1,000 | Cypher queries |
| `scheduled_ingestion_control.py` | ~577 | Durable workflow |
| Remaining bounded modules | ~650 | Registry, policy, etc. |

### Scheduler machinery

- `ScheduledIngestionControl` Neo4j node + queries
- Occurrence binding / rebinding / generation-bound safety
- Obligation coalescing for lifecycle/KNOWS
- Durable one-child-at-a-time workflow

## What Stays

| Component | Why |
|---|---|
| `IngestRun` nodes | UI/monitoring |
| Celery Beat schedule | Group dispatch timing |
| Redis per-source locks | Concurrency guard |
| Redis global semaphore | Total concurrency cap |
| `SourceConnector` (dump) | Dump file processing |
| Existing connector watermark stores | Already Redis-based; unify under `IngestionWatermark` |
| Record processing pipeline | Normalize → match → merge unchanged |
| Safe-stop signal handling | SIGTERM / window check between pages |
| Config-based scheduling enable/disable | Unchanged |

## Per-Source Impact

### Already watermark-ready (no source changes needed)

| Source | Current state | Migration |
|---|---|---|
| **Fundbox API** | `updated_since` + Redis watermark | Drop bounded wrapper, keep watermark store |
| **PHPPOS (Eko, Speedzone)** | `updated_since` + Redis watermark | Drop bounded wrapper, keep watermark store |
| **Bitrix Open Lines** | `RedisWatermarkStore` + overlap | Drop bounded wrapper, keep watermark store |
| **WhatsAdmin** | `changed_since` + per-session watermark | Drop bounded wrapper, simplify page checkpoint to just watermark |

### Need source API changes (no DB migration)

| Source | What's needed | Scope |
|---|---|---|
| **SG Bankruptcy** | Add `updated_since` filter + cursor pagination + `updated_at` in response to the scraper export endpoint | API endpoint change on scraper service |
| **SG Rental Flats** | Add `updated_since` filter + keyset cursor to the scraper integration endpoint | API endpoint change on scraper service |

These sources currently have no `updated_since` filter or stable cursor. The
API changes add query parameters and response fields — no database schema
changes on the scraper side.

### Dump-only (no change)

| Source | Notes |
|---|---|
| **OneDiver** | Dump-only; no watermark needed until a live feed is built |

### Deferred

| Source | Notes |
|---|---|
| **Fundbox deletion reconciliation** | The unfiltered second pass (#433) remains a separate concern — replace with a deletion feed from the source API when available |

## Relation to #429 Children

| Issue | Status under watermark model |
|---|---|
| **#430** (bounded framework) | Merged but becomes **superseded** — the framework it introduced is removed |
| **#431** (scheduler coalescing) | Merged but becomes **superseded** — durable workflow replaced by simple dispatch |
| **#432** (Bitrix deals + OL) | Open Lines: already watermark-ready. Deals: still needs changed-deal feed from Bitrix API — unchanged |
| **#433** (Fundbox incremental) | Watermark part is done. Deletion reconciliation deferred to source API change |
| **#434** (PHPPOS deltas) | **Simplified** — frozen-window cursors no longer needed; existing `updated_since` suffices |
| **#435** (WhatsAdmin) | **Simplified** — drop mid-page resume; watermark per session is enough |
| **#436** (SG bankruptcy) | Unchanged — still needs `updated_since` on scraper endpoint (API change, not DB migration) |
| **#437** (SG rental flats) | **Simplified** — keyset cursor + `updated_since` on scraper endpoint (API change, not DB migration) |
| **#438** (OneDiver seed) | Unchanged — dump-only, no watermark needed |

## Migration Path

1. Implement `IngestionWatermark` store and `IncrementalConnector` protocol.
2. Implement the page-commit-advance runner with safe-stop checks.
3. Migrate each source connector from `BoundedConnector` to
   `IncrementalConnector`, reusing existing watermark stores.
4. Replace the durable scheduler with the simplified dispatch task.
5. Remove bounded modules and Neo4j node types.
6. Clean up graph schema (drop bounded indexes/constraints).

Steps 1–2 can land as a single PR. Step 3 is per-source (one PR each).
Steps 4–6 are cleanup after all sources are migrated.
