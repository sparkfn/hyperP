# Plan: Incremental SG Bankruptcy via Watermark (#436)

Date: 2026-09-18

## Objective

Implement incremental SG bankruptcy ingestion using the `IncrementalConnector`
protocol (from #454). The scraper export endpoint already provides cursor-based
pagination; this plan adds `updated_since` filtering support to the wire models
and implements a new `IncrementalConnector` that wraps the existing API connector
logic with watermark-aware querying.

## Scope

In scope:
- Add `updated_at` field to `BankruptcyExportItem` wire model
- Add `updated_since` query parameter support to the export page request
- Implement `SGGovernmentBankruptcyIncrementalConnector` satisfying the `IncrementalConnector` protocol
- Register factory in `INCREMENTAL_CONNECTORS["sgbankruptcy"]`
- Unit tests for protocol compliance, pagination, `updated_since` filtering, error handling, registry

Out of scope (deferred):
- Removing the existing `SourceConnector`-based API connector (dump connector continues for manual imports)
- Scheduler integration (dispatching `run_incremental_task` from Beat)
- SG rental flats (#437)
- Removing the bounded ingestion framework

## Pre-implementation Checklist

- [x] Design spec reviewed: `docs/superpowers/specs/2026-09-18-watermark-ingestion-model-design.md`
- [x] Core framework (#454) is merged: `incremental_connector.py`, `watermark_store.py`, `watermark_runner.py`, `tasks.py` (INCREMENTAL_CONNECTORS + run_incremental_task)
- [x] Existing bankruptcy API connector reviewed: `services/ingestion/src/connectors/sggov/bankruptcy_api.py`
- [x] Wire models reviewed: `services/ingestion/src/connectors/sggov/bankruptcy_api_models.py`
- [x] Common envelope builder reviewed: `services/ingestion/src/connectors/sggov/bankruptcy_common.py`

## Implementation Tasks

### Task 1: Update `BankruptcyExportItem` wire model

**File:** `services/ingestion/src/connectors/sggov/bankruptcy_api_models.py`

Add `updated_at` field to `BankruptcyExportItem`:

```python
updated_at: datetime | None = None
```

Default `None` allows backward compatibility — the scraper API will start
returning this field, but older responses without it still validate. The
incremental connector will fall back to `last_seen_at` when `updated_at` is
`None`.

No changes to `BankruptcyExportPage` — it already has `next_cursor`.

**Impact:** The existing `SGGovernmentBankruptcyApiConnector` (SourceConnector)
continues to work unchanged because it ignores `updated_at`.

### Task 2: Implement `SGGovernmentBankruptcyIncrementalConnector`

**File:** `services/ingestion/src/connectors/sggov/bankruptcy_incremental.py` (new)

This connector implements `IncrementalConnector` from `src.incremental_connector`.
It wraps the same HTTP endpoint used by the existing API connector, adding
`updated_since` filtering.

```python
class SGGovernmentBankruptcyIncrementalConnector:
    """Incremental connector for SG bankruptcy via the scraper export API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        page_size: int = 500,
        timeout_seconds: float = 30.0,
        http: httpx.Client | None = None,
        max_attempts: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None: ...

    def open_query(self, updated_since: datetime | None) -> None: ...
    def fetch_next_page(self) -> IncrementalPage: ...
    def get_source_key(self) -> str: ...
    def close(self) -> None: ...
```

Key implementation details:

0. **Constructor**: When `http` is not provided, create
   `httpx.Client(timeout=timeout_seconds)`. When `http` IS provided (tests),
   `timeout_seconds` is ignored — the caller controls the client.

1. **`open_query(updated_since)`**: Stores `updated_since` and resets internal cursor
   state. Does NOT make an HTTP call — the first `fetch_next_page()` does.

2. **`fetch_next_page()`**: Calls the same `/api/v1/export/bankruptcy-records`
   endpoint as the existing connector, with these params:
   - `limit`: page size
   - `cursor`: internal cursor from the previous page (if any)
   - `updated_since`: serialized via `.isoformat()` when present; **omitted
     entirely** (not sent as empty/null) when `None` (bootstrap)

   Returns `IncrementalPage`:
   - `records`: tuple of envelopes built via `build_api_envelope(item)`
   - `has_more`: `page.next_cursor is not None`
   - `max_updated_at`: computed from page items (see detail 2a below).

   2a. **`max_updated_at` computation — timezone normalization & empty guard**:

   For each item, the effective timestamp is `item.updated_at or item.last_seen_at`.
   All timestamps MUST be timezone-aware UTC before comparison. The Pydantic model
   parses ISO strings to `datetime` — if a value arrives naive (no `tzinfo`),
   defensively attach `UTC`:

   ```python
   def _ensure_utc(dt: datetime) -> datetime:
       if dt.tzinfo is None:
           return dt.replace(tzinfo=UTC)
       return dt
   ```

   Apply `_ensure_utc` to every effective timestamp before `max()`. This prevents
   `TypeError: can't compare offset-naive and offset-aware datetimes` in the
   watermark runner's `page.max_updated_at > max_updated_at` comparison.

   **Empty page guard**: When `page.items` is empty, do NOT call `max()` on an
   empty sequence. Instead, use the `updated_since` value stored from
   `open_query()` (or `datetime.min.replace(tzinfo=UTC)` for bootstrap). This
   produces a `max_updated_at` that never advances the watermark past its
   starting position — correct behavior for an empty result.

3. **`get_source_key()`**: Returns `"sgbankruptcy"`.

4. **`close()`**: Closes the `httpx.Client`.

5. **HTTP retry logic**: Reuse the same retry pattern from the existing API
   connector (`_get_page`). Factor this into a private method or reuse the
   existing `_get_page` by composition/inheritance — prefer composition (a
   private helper in the same module or shared from the existing connector).

   **Decision: copy the `_get_page` retry pattern** into the new connector as a
   private method rather than extracting a shared base class. The pattern is 20
   lines and the existing connector should remain untouched. If the retry pattern
   drifts between the two, it's a future cleanup, not a current concern.

6. **Cursor stall guard**: Same as existing connector — track `seen_cursors` and
   raise `RuntimeError` if the cursor repeats.

7. **Empty page handling**: Covered by detail 2a above. If `page.items` is empty
   and `page.next_cursor` is `None`, return `IncrementalPage` with empty
   `records`, `has_more=False`, and `max_updated_at` set to the `updated_since`
   value from `open_query` (or `datetime.min.replace(tzinfo=UTC)` for bootstrap).

8. **`open_query` must be called before `fetch_next_page`**: Raise `RuntimeError`
   if `fetch_next_page()` is called before `open_query()`.

### Task 3: Register factory in `INCREMENTAL_CONNECTORS`

**File:** `services/ingestion/src/tasks.py`

At the bottom of the file, after the `INCREMENTAL_CONNECTORS` dict definition,
register a factory:

```python
def _create_sgbankruptcy_incremental() -> IncrementalConnector:
    from src.connectors.sggov.bankruptcy_incremental import (
        SGGovernmentBankruptcyIncrementalConnector,
    )
    settings = get_settings()
    return SGGovernmentBankruptcyIncrementalConnector(
        base_url=settings.sgbankruptcy_api_base_url,
        api_key=settings.sgbankruptcy_api_key.get_secret_value(),
        page_size=settings.sgbankruptcy_api_page_size,
        timeout_seconds=settings.sgbankruptcy_api_timeout_seconds,
        max_attempts=settings.sgbankruptcy_api_max_attempts,
    )

INCREMENTAL_CONNECTORS["sgbankruptcy"] = _create_sgbankruptcy_incremental
```

The factory is lazy — it reads settings only when the task invokes it. The
existing `run_incremental_task` already handles `Reject` for unconfigured sources
(empty `base_url` / `api_key` will raise `ValueError` in the connector
constructor, which the task catches and rejects).

### Task 4: Unit tests

**File:** `services/ingestion/tests/test_sggov_bankruptcy_incremental.py` (new)

Tests use `httpx.MockTransport` (same pattern as `test_sggov_bankruptcy_api_connector.py`).

#### Test cases:

1. **`test_incremental_connector_satisfies_protocol`**
   Verify `isinstance(connector, IncrementalConnector)` (runtime_checkable).

2. **`test_incremental_fetch_all_pages_bootstrap`**
   `open_query(updated_since=None)` → no `updated_since` param sent.
   Two pages with `next_cursor` → third page with `next_cursor=None`.
   Verify all records returned, `has_more` correct per page, `max_updated_at`
   tracks the maximum `updated_at` (or `last_seen_at` fallback).

3. **`test_incremental_fetch_with_updated_since_delta`**
   `open_query(updated_since=datetime(...))` → verify `updated_since` ISO param
   is sent to the API. Verify records from a single page returned correctly.

4. **`test_incremental_updated_at_fallback_to_last_seen_at`**
   Items where `updated_at` is `None` → `max_updated_at` uses `last_seen_at`.

5. **`test_incremental_empty_page_returns_no_records`**
   API returns `{"items": [], "next_cursor": null}` → `IncrementalPage` with
   empty `records`, `has_more=False`.

6. **`test_incremental_cursor_stall_raises`**
   API returns same cursor repeatedly → `RuntimeError`.

7. **`test_incremental_retries_server_errors`**
   503 on first attempt, 200 on second → succeeds.

8. **`test_incremental_retries_transport_errors`**
   `httpx.ConnectError` on first attempt, 200 on second → succeeds.

9. **`test_incremental_propagates_auth_failure`**
   401 → `httpx.HTTPStatusError` without retry.

10. **`test_incremental_fetch_before_open_raises`**
    Calling `fetch_next_page()` before `open_query()` → `RuntimeError`.

11. **`test_incremental_get_source_key`**
    Returns `"sgbankruptcy"`.

12. **`test_incremental_connector_registered_in_task_registry`**
    `assert "sgbankruptcy" in INCREMENTAL_CONNECTORS`
    `assert callable(INCREMENTAL_CONNECTORS["sgbankruptcy"])`

13. **`test_incremental_bankruptcy_run_advances_watermark_on_drain`**
    Integration-level test using `run_incremental` from `watermark_runner` with
    a real `SGGovernmentBankruptcyIncrementalConnector` (httpx.MockTransport) and
    `fakeredis.FakeRedis`. Two scenarios in one test function:
    - **Full drain** (`has_more=False` on final page): verify `load_watermark`
      returns the advanced `updated_at` from Redis after `run_incremental`
      returns `status="caught_up"`.
    - **Safe stop / yielded** (simulate `shutdown_signal` returning `True` before
      second page): verify `load_watermark` returns the **original** watermark
      (unchanged) after `run_incremental` returns `status="yielded"`.

    This directly validates acceptance criterion: "Watermark advances only on
    full cursor drain." The test stubs `Neo4jClient` with a minimal fake that
    records `CREATE_INGEST_RUN` / `UPDATE_INGEST_RUN` calls (same pattern as
    `test_incremental_checkpoints.py`'s `_Client`).

14. **`test_incremental_timestamps_normalized_to_utc`**
    Items with naive `last_seen_at` (no tzinfo) and aware `updated_at` (with
    UTC) in the same page → `max_updated_at` is timezone-aware UTC, no
    `TypeError` raised.

**Dump regression:** Existing `test_sggov_bankruptcy_connector.py` covers the
dump connector path. Verify it passes unchanged after the wire model update (the
new `updated_at` field defaults to `None` so existing dump test fixtures remain
valid). No new dump tests needed.

**CI note:** New test modules are active by default (selection manifest only
lists HISTORICAL modules), so no manifest update needed.

### Task 5: Update `__init__.py` exports (optional)

**File:** `services/ingestion/src/connectors/sggov/__init__.py`

Add `SGGovernmentBankruptcyIncrementalConnector` to `__all__` exports. This is
optional — the task factory imports directly from the module path. Include only
if the existing pattern exports all public connectors.

**Decision:** Skip this. The `__init__.py` currently exports only dump connectors.
The incremental connector is used only by the task factory via direct import.

## File Change Summary

| File | Change |
|---|---|
| `services/ingestion/src/connectors/sggov/bankruptcy_api_models.py` | Add `updated_at: datetime \| None = None` to `BankruptcyExportItem` |
| `services/ingestion/src/connectors/sggov/bankruptcy_incremental.py` | **New** — `SGGovernmentBankruptcyIncrementalConnector` implementing `IncrementalConnector` |
| `services/ingestion/src/tasks.py` | Register `_create_sgbankruptcy_incremental` factory in `INCREMENTAL_CONNECTORS` |
| `services/ingestion/tests/test_sggov_bankruptcy_incremental.py` | **New** — 14 unit tests (incl. watermark-drain integration + UTC normalization) |

## Risks & Mitigations

1. **Scraper API not yet returning `updated_at`**: The wire model defaults to
   `None`, and the connector falls back to `last_seen_at`. No breakage. The
   incremental connector works correctly with the fallback until the scraper is
   updated.

2. **Scraper API not yet supporting `updated_since` param**: On bootstrap
   (`open_query(None)`), no param is sent — full fetch works today. On delta,
   the scraper may ignore the unknown param and return all records. This is
   wasteful but correct: the watermark runner processes idempotently and advances
   the watermark. Once the scraper implements the filter, delta runs become
   efficient.

3. **Existing dump/API connectors**: Unchanged. The dump connector
   (`SGGovernmentBankruptcyConnector`) and the existing API connector
   (`SGGovernmentBankruptcyApiConnector`) continue to work for manual imports
   via `run_ingestion_task`.

## Deferred Items

- **Scheduler integration**: Dispatching `run_incremental_task.delay("sgbankruptcy")`
  from Celery Beat on the SG Gov group's weekday — tracked separately.
- **Removing the SourceConnector-based API connector**: After the incremental
  path is validated in production, the old `SGGovernmentBankruptcyApiConnector`
  can be removed.
- **SG rental flats incremental (#437)**: Same pattern, separate issue.
