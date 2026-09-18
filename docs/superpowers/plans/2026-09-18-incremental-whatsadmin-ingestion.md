# Implementation Plan: Incremental WhatsAdmin Ingestion (#435)

Branch: `issue-435-incremental-whatsadmin-ingestion`
Design spec: `docs/superpowers/specs/2026-09-18-watermark-ingestion-model-design.md`
Core framework: #454 (`incremental_connector.py`, `watermark_store.py`, `watermark_runner.py`, `watermark_scheduler.py`, `tasks.py:run_incremental_task`)

## 1. Overview & Acceptance Criteria Mapping

| Acceptance criterion | Plan step |
|---|---|
| 1. WhatsAdmin implements `IncrementalConnector` (open_query → fetch_next_page → IncrementalPage) | Step A |
| 2. Drop PageCheckpointStore (mid-page resume unnecessary; on safe-stop the whole query replays from the watermark) | Step B |
| 3. Unify watermark store under `IngestionWatermark` / `WatermarkStore` from #454 | Step A (connector uses #454 `load_watermark`/`save_watermark` via the runner, not its own `RedisWatermarkStore`) |
| 4. Drop `BoundedConnector` wrapper, checkpoint descriptors, and `ExtractionRetryStore` | Step B |
| 5. Register in connector registry for `run_incremental_task` dispatch | Step C |
| 6. Tests & lint compliance | Step D |

## 2. Files to Modify / Add / Remove

### New files
| File | Purpose |
|---|---|
| `services/ingestion/src/connectors/whatsadmin_api/incremental.py` | New `WhatsAdminIncrementalConnector` implementing `IncrementalConnector` |
| `services/ingestion/tests/test_whatsadmin_incremental.py` | Tests for the new incremental connector |

### Modified files
| File | Change |
|---|---|
| `services/ingestion/src/tasks.py` | Register `whatsapp_chat` in `INCREMENTAL_CONNECTORS` dict with a factory function |
| `services/ingestion/src/connectors/whatsadmin_api/__init__.py` | Re-export new incremental connector |
| `services/ingestion/tests/test_worker_topology.py` | Ensure `run_incremental_task` route table covers `whatsapp_chat` if source-level dispatch test exists |

### Files NOT modified (kept as-is)
| File | Rationale |
|---|---|
| `connector.py` (existing `WhatsAdminChatApiConnector`) | **Kept**. The existing `SourceConnector` implementation stays for dump-mode and backward-compatible `run_ingestion_task` dispatch until all sources migrate and the bounded framework is removed. Removing it is out of scope for #435. |
| `watermark.py` (`RedisWatermarkStore`, `PageCheckpointStore`, `ExtractionRetryStore`, `PageCheckpoint`) | **Kept**. The existing connector uses these. Removal is a follow-up when the old connector is retired. |
| `retry_queue.py` | **Kept** for same reason as above. |
| `client.py`, `credentials.py`, `models.py` | **No changes needed** — the new connector reuses these directly. |
| `watermark_runner.py`, `watermark_store.py`, `incremental_connector.py` | **No changes** — #454 framework used as-is. |

### Why keep the old connector
The acceptance criteria say "wrap existing watermark pattern" and "register in connector registry", not "delete the old connector". The old `WhatsAdminChatApiConnector` is still imported by `main.py:create_whatsadmin_api_connector()` and used by `run_ingestion_task` for dump-mode and the legacy `api` mode. Deleting it now would break dump-mode ingestion and existing Celery task deliveries. The old connector is retired when the bounded framework (#430) is removed — a separate cleanup PR.

## 3. Detailed Technical Design & Implementation Steps

### Step A — New `WhatsAdminIncrementalConnector`

**File:** `services/ingestion/src/connectors/whatsadmin_api/incremental.py`

The connector implements `IncrementalConnector` from `src.incremental_connector`.

**Design:**

```python
class WhatsAdminIncrementalConnector:
    """IncrementalConnector for WhatsAdmin API chat ingestion."""

    def __init__(
        self,
        clients: tuple[WhatsAdminApiClient, ...],
        *,
        legacy_entity: WhatsAdminEntity | None = None,
    ) -> None:
        self._clients = clients
        self._legacy_entity = legacy_entity
        # Internal iteration state
        self._page_iter: Iterator[IncrementalPage] | None = None

    def open_query(self, updated_since: datetime | None) -> None:
        """Build a lazy iterator that walks sessions × chat-pages across all clients."""
        self._page_iter = self._all_pages(updated_since)

    def fetch_next_page(self) -> IncrementalPage:
        """Return the next page from the cursor."""
        ...

    def get_source_key(self) -> str:
        return "whatsapp_chat"

    def close(self) -> None:
        for client in self._clients:
            client.close()
```

**Key internal method — `_all_pages(updated_since)`:**

This generator yields **raw** `IncrementalPage` objects (with `has_more=True` as a placeholder — the authoritative `has_more` is assigned by the peeking buffer in `fetch_next_page`, not by the generator) by iterating:
1. For each client (entity):
2. For each session from `client.iter_sessions()`:
3. Compute `changed_since = updated_since.isoformat() if updated_since else None`
4. For each `ChatPage` from `client.iter_chat_pages(session_id, changed_since)`:
   - Validate chat identities (same as existing connector)
   - Build `_ChatBundle` list from page data (same as existing `_bundles` method)
   - Call `process_whatsapp_bundles(bundles, on_extraction_failure=...)` to get source records
   - Compute `max_updated_at` from `page.meta.snapshot_at` (the server-side snapshot timestamp)
   - Yield `IncrementalPage(records=tuple(records), has_more=True, max_updated_at=...)`
5. When the generator is exhausted (all clients/sessions/pages drained), `StopIteration` is raised naturally.

**`has_more` semantics — peeking buffer in `fetch_next_page`:**

The `IncrementalConnector` protocol uses `has_more` to tell the runner whether the cursor is exhausted. For WhatsAdmin, "cursor exhausted" means all sessions across all clients have been fully paginated. The generator `_all_pages` does **not** know whether it's yielding the last page — it only knows about the current API page's `pagination.has_more`, not whether more sessions or clients remain.

The peeking buffer in `fetch_next_page` assigns the **authoritative** `has_more`:

1. On `open_query(updated_since)`, build the generator and attempt to buffer the first page via `next(self._page_iter, None)`. If the generator yields zero pages (e.g. `iter_sessions()` returns no sessions), store `self._buffered = None`.

2. On `fetch_next_page()`:
   - If `self._buffered is None` (zero-page case), return the **empty terminal page**: `IncrementalPage(records=(), has_more=False, max_updated_at=updated_since or datetime.now(tz=UTC))`. This is critical because `watermark_runner.py`'s `while True` loop calls `fetch_next_page()` and does NOT catch `StopIteration` — an unguarded `StopIteration` would bubble as a broad `Exception` and mark the run `failed`.
   - Otherwise, try to advance the generator: `next_page = next(self._page_iter, None)`.
   - If `next_page is not None`: return `dataclasses.replace(self._buffered, has_more=True)`, then shift `self._buffered = next_page`.
   - If `next_page is None` (generator exhausted): return `dataclasses.replace(self._buffered, has_more=False)`, then set `self._buffered = None`.

**`max_updated_at` source:** Use `page.meta.snapshot_at` from the WhatsAdmin API response. This is the server-side snapshot timestamp guaranteed to be monotonically increasing across pages within a session. This matches how the existing connector computes its watermark (`_pending_watermarks[state_key] = page.meta.snapshot_at`). If `snapshot_at` is None, raise `RuntimeError` (same as existing connector).

**`updated_since` mapping:** The WhatsAdmin API's `changedSince` parameter uses `>=` semantics — records at exactly the watermark timestamp are re-fetched. This matches the design spec's "small, bounded overlap" and ingestion's idempotent upsert.

**Extraction failure handling:** Extraction failures during `process_whatsapp_bundles` are logged but do not prevent the page from being included in the `IncrementalPage.records`. The watermark runner processes records through the existing pipeline, which handles failures. Extraction retries (the `ExtractionRetryStore` pattern) are **not** carried forward to the incremental connector — inline retry within the connector is sufficient per the design spec, and the runner's next tick replays from the same watermark on failure.

**Per-entity dispatch and watermarks:** The scheduled ingestion groups (`scheduled_ingestion_groups.py`) dispatch WhatsAdmin per-entity: Tuesday runs `ScheduledIngestionSpec('whatsapp_chat', entity_key='eko')` and Wednesday runs `ScheduledIngestionSpec('whatsapp_chat', entity_key='speedzone')`. This means `run_incremental_task` is called with `entity_key='eko'` or `entity_key='speedzone'`, and `run_incremental` passes `entity_key` to `load_watermark` / `save_watermark` — so watermarks are stored at `profile_unifier:watermark:whatsapp_chat:eko` and `profile_unifier:watermark:whatsapp_chat:speedzone` respectively.

The factory function (Step C) accepts `entity_key` and passes it to `resolver.resolve_job(entity_key)`, which filters to only the requested entity's credential. When `entity_key=None`, both entities are resolved (used for ad-hoc dispatch). The connector itself is entity-agnostic — it iterates whatever clients the factory provides.

This aligns with the existing scheduling model and provides clean entity-level isolation: each entity has its own watermark, lock scope, and run cadence.

### Step B — Simplification (what is dropped from the new connector)

The new `WhatsAdminIncrementalConnector` does **not** include:

1. **`PageCheckpointStore` / `PageCheckpoint`** — No mid-page resume. On safe stop, the watermark runner does not advance the watermark, and the next run replays the entire query from the last committed watermark.

2. **`ExtractionRetryStore` / retry queue** — No durable retry obligations. Failed extractions are naturally retried on the next watermark run (the records are re-fetched since the watermark didn't advance). Transient API errors are retried inline by `WhatsAdminApiClient._post()` (existing exponential backoff).

3. **`commit_watermark()` / `record_processed()` / `commit_progress_with_errors()`** — These are `SourceConnector` patterns. The `IncrementalConnector` protocol doesn't have them — watermark advancement is handled by `WatermarkRunner`.

4. **`failure_checkpoint()` / `_active_checkpoint`** — Diagnostic state for the old `run_ingestion` path. The watermark runner logs its own progress.

5. **`incremental: bool` flag** — The incremental connector is always incremental. Bootstrap vs. delta is determined by the watermark store (None → bootstrap, has value → delta), handled by the runner.

### Step C — Connector Registry Registration

**File:** `services/ingestion/src/tasks.py`

**Change 1 — Factory function** that accepts `entity_key` and registers in `INCREMENTAL_CONNECTORS`:

```python
def _create_whatsadmin_incremental(entity_key: str | None = None) -> object:
    from src.connectors.whatsadmin_api.incremental import WhatsAdminIncrementalConnector
    from src.connectors.whatsadmin_api.client import WhatsAdminApiClient
    from src.connectors.whatsadmin_api.credentials import WhatsAdminCredentialResolver

    settings = get_settings()
    resolver = WhatsAdminCredentialResolver(
        base_url=settings.whatsadmin_api_base_url,
        eko_api_key=settings.whatsadmin_eko_api_key,
        speedzone_api_key=settings.whatsadmin_speedzone_api_key,
    )
    clients = tuple(
        WhatsAdminApiClient(
            credential=credential,
            page_size=settings.whatsadmin_api_page_size,
            timeout_seconds=settings.whatsadmin_api_timeout_seconds,
            max_attempts=settings.whatsadmin_api_max_attempts,
            retry_base_delay_seconds=settings.whatsadmin_api_retry_base_delay_seconds,
        )
        for credential in resolver.resolve_job(entity_key)
    )
    return WhatsAdminIncrementalConnector(
        clients,
        legacy_entity=settings.whatsadmin_legacy_entity,
    )

INCREMENTAL_CONNECTORS: dict[str, object] = {
    "whatsapp_chat": _create_whatsadmin_incremental,
}
```

**Change 2 — Pass `entity_key` to factory** at line 2078:

```python
# Before:
connector = connector_factory()

# After:
connector = connector_factory(entity_key)
```

This makes `run_incremental_task.delay("whatsapp_chat", entity_key="eko")` dispatch only the eko client, with its own per-entity watermark at `profile_unifier:watermark:whatsapp_chat:eko`. The scheduler dispatches per-entity (Tuesday=eko, Wednesday=speedzone), so each entity runs independently with its own watermark, lock scope, and cadence.

### Step D — Tests

**File:** `services/ingestion/tests/test_whatsadmin_incremental.py`

Tests modeled on `test_watermark_runner.py` patterns but exercising the WhatsAdmin-specific connector:

1. **`test_incremental_connector_open_query_passes_updated_since`** — Verifies `open_query(datetime)` translates to `changedSince` in API calls.

2. **`test_incremental_connector_bootstrap_passes_none`** — Verifies `open_query(None)` calls the API without `changedSince`.

3. **`test_incremental_connector_drains_all_sessions_and_pages`** — Two clients (eko, speedzone), each with one session, each with two pages. Verifies all records are yielded, `has_more` transitions correctly, and the final page has `has_more=False`.

4. **`test_incremental_connector_max_updated_at_tracks_snapshot`** — Verifies `max_updated_at` reflects the highest `snapshot_at` across pages.

5. **`test_incremental_connector_validates_chat_identity`** — Session/chat identity mismatch raises `RuntimeError`.

6. **`test_incremental_connector_rejects_missing_snapshot`** — `snapshot_at=None` raises `RuntimeError`.

7. **`test_incremental_connector_close_closes_all_clients`** — Verifies `close()` propagates to all clients.

8. **`test_incremental_connector_source_key`** — `get_source_key()` returns `"whatsapp_chat"`.

9. **`test_incremental_connector_registry_has_whatsapp_chat`** — Verifies `INCREMENTAL_CONNECTORS["whatsapp_chat"]` is callable.

10. **`test_incremental_connector_empty_sessions_returns_empty_terminal_page`** — When `iter_sessions()` returns zero sessions (generator yields zero pages), `fetch_next_page()` returns `IncrementalPage(records=(), has_more=False, max_updated_at=<updated_since or now>)` instead of raising `StopIteration`.

11. **`test_incremental_connector_factory_respects_entity_key`** — Verifies that calling the factory with `entity_key="eko"` results in `resolver.resolve_job("eko")` producing only the eko client (one client, not two).

All tests use stub clients and monkeypatched `process_whatsapp_bundles` — no real API, Redis, or Neo4j.

## 4. Verification & Testing Plan

### Local verification (permitted one-shot generates)
- `ruff check services/ingestion/src/connectors/whatsadmin_api/incremental.py`
- `ruff format services/ingestion/src/connectors/whatsadmin_api/incremental.py`
- Same for modified `tasks.py` lines.

### CI verification (push to PR branch)
- Push to `issue-435-incremental-whatsadmin-ingestion` branch.
- PR pipeline runs: `ruff check`, `ruff format --check`, `mypy --strict`, `pytest` for both services.
- New tests in `test_whatsadmin_incremental.py` are active by default (not in `HISTORICAL_TEST_MODULES`).
- Verify zero net ESLint warnings (no frontend changes in this PR).

### Test coverage
| Area | Test file | Coverage |
|---|---|---|
| IncrementalConnector protocol compliance | `test_whatsadmin_incremental.py` | open_query, fetch_next_page, get_source_key, close |
| Multi-entity iteration | `test_whatsadmin_incremental.py` | Two clients, correct page/session ordering |
| has_more boundary | `test_whatsadmin_incremental.py` | Intermediate=True, final=False |
| Empty query (zero sessions) | `test_whatsadmin_incremental.py` | Returns empty terminal page, no StopIteration |
| max_updated_at tracking | `test_whatsadmin_incremental.py` | Across pages/sessions |
| Validation guards | `test_whatsadmin_incremental.py` | Identity mismatch, missing snapshot |
| Registry wiring | `test_whatsadmin_incremental.py` | INCREMENTAL_CONNECTORS entry is callable |
| Factory entity_key filtering | `test_whatsadmin_incremental.py` | resolve_job(entity_key) produces only the requested client |
| Watermark runner integration | `test_watermark_runner.py` (existing) | Already covers runner loop with FakeConnector |
| Existing connector (unchanged) | `test_whatsadmin_api_connector.py` (existing) | Passes unchanged |

## 5. Risks & Assumptions

### Assumptions
1. **Per-entity watermarks aligned with scheduled groups.** The scheduler dispatches `whatsapp_chat` per-entity (Tuesday=eko, Wednesday=speedzone via `ScheduledIngestionSpec`). The factory accepts `entity_key`, passes it to `resolver.resolve_job(entity_key)` (filtering to one credential), and the runner stores watermarks at `profile_unifier:watermark:whatsapp_chat:eko` / `:speedzone`. Ad-hoc dispatch with `entity_key=None` resolves both entities into a single connector and uses an unscoped watermark — this is appropriate for manual/ad-hoc runs where entity isolation is not required.

2. **Old `SourceConnector` path stays until bounded removal.** The old `WhatsAdminChatApiConnector` and its associated `RedisWatermarkStore`/`PageCheckpointStore` stay until the bounded framework (#430) is removed and `run_ingestion_task`'s WhatsAdmin path is retired.

3. **Extraction retry is not needed in incremental.** The design spec explicitly says "inline retry within the connector is sufficient." The existing `WhatsAdminApiClient._post()` already retries transient HTTP errors. Extraction failures (LLM parse failures) are bounded and replayed on the next watermark run.

### Risks
1. **Existing per-session watermarks in `RedisWatermarkStore` are not migrated.** The old connector stores watermarks at `profile_unifier:whatsadmin-api:whatsapp_chat:{entity}:{session}:watermark` (per-session). The new model stores at `profile_unifier:watermark:whatsapp_chat:{entity}` (per-entity). On first incremental dispatch, the new watermark will be `None` (bootstrap), causing a full re-fetch from the beginning. This is acceptable: ingestion is idempotent (upsert by source key), and a one-time bootstrap is bounded by the total chat history. The old per-session watermarks remain in Redis but are no longer read by the new connector.

2. **`snapshot_at` may not be monotonic across sessions within an entity.** If session A's snapshot is ahead of session B's, `max_updated_at` reflects the global maximum across sessions, which is correct for watermark semantics — re-fetch from that point includes all earlier records from all sessions.

### Deferred items (out of scope)
- Removal of `PageCheckpointStore`, `ExtractionRetryStore`, `retry_queue.py` — follow-up when old connector is retired.
- Migration of old per-session Redis watermarks to per-entity — not needed; bootstrap re-fetch is acceptable.
- Scheduler wiring for `dispatch_incremental_group` — the scheduler (#431 replacement) is a separate concern.
- Removal of bounded framework files — tracked in the design spec's migration path step 5–6.
