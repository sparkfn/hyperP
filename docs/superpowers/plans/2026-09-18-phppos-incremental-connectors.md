# Plan: PHPPOS IncrementalConnector Wrappers (#434)

## 1. Intended Behavior & Acceptance Criteria

Add `IncrementalConnector` protocol support (from #454's watermark framework) to the
existing PHPPOS API connectors for all 4 source keys:

| Source key | Resource | Brand |
|---|---|---|
| `eko_phppos` | customers | Eko |
| `eko_phppos:sales` | sales | Eko |
| `speedzone_phppos` | customers | SpeedZone |
| `speedzone_phppos:sales` | sales | SpeedZone |

**Acceptance criteria:**

1. Each connector satisfies `isinstance(connector, IncrementalConnector)` (structural).
2. `open_query(updated_since)` configures the cursor-pagination query with the watermark
   datetime (formatted as ISO string for the PHPPOS API's `updated_since` parameter),
   resets internal cursor state.
3. `fetch_next_page()` returns one `IncrementalPage` per API cursor page:
   - `records`: tuple of source-record envelopes (same mapping as today — customers via
     `EkoConnector._build_one`/`SpeedZoneConnector._build_envelope_with_customer`; sales
     via the existing `_build_record` method).
   - `has_more`: mirrors `Pagination.has_more` from the API response.
   - `max_updated_at`: the latest `last_modified`/`create_date` (customers) or `sale_time`
     (sales) seen in this page, parsed to UTC `datetime`.
4. `get_source_key()` returns the correct key per connector (already exists).
5. `close()` delegates to `PhpposApiClient.close()` (already exists).
6. `fetch_records()` (SourceConnector) is rewritten to drain `fetch_next_page()`,
   maintaining backward compatibility with the `run_ingestion` code path.
7. All 4 source keys are registered in `INCREMENTAL_CONNECTORS` in `tasks.py`; each
   factory creates a fresh `PhpposApiClient` via `create_phppos_api_client` and wraps it
   in the corresponding connector.
8. Legacy ad-hoc watermark code is removed from the PHPPOS API connectors: `WatermarkStore`
   protocol, `commit_watermark`, `_track_watermark`, `_watermark_key`, `_latest_updated_at`,
   `watermark_store` constructor parameter. Watermark persistence is now handled exclusively
   by `IngestionWatermark` from `src.watermark_store` via the watermark runner.
9. `main.py`'s PHPPOS API connector construction is simplified: the
   `checkpoint_store.get("profile_unifier:phppos_api:watermark:...")` lookup and
   `watermark_store=checkpoint_store` / `updated_since=updated_since` args are removed.
   The old `run_ingestion_task` path becomes full-fetch only for PHPPOS API sources;
   incremental ingestion goes through `run_incremental_task`.
10. Unit tests cover: protocol satisfaction, page-level pagination, row mapping for all 4
    connector variants, `max_updated_at` tracking, `open_query` with and without watermark,
    `close` delegation, empty page handling, runner integration (watermark advance on full
    drain; no advance on safe-stop).
11. Existing legacy watermark test
    (`test_customer_connector_uses_checkpoint_and_stages_normalized_source_watermark`)
    is replaced with an IncrementalConnector-aware test validating `open_query` +
    `fetch_next_page` incremental behavior.

## 2. Files to Touch / Create

### Modified files

| File | Change |
|---|---|
| `services/ingestion/src/connectors/phppos_api/client.py` | Add `fetch_customer_page(cursor, updated_since) -> CustomerPage` and `fetch_sales_page(cursor, updated_since) -> SalesPage` public methods. Refactor `iter_customers`/`iter_sales` to delegate to these. |
| `services/ingestion/src/connectors/phppos_api/connectors.py` | Add `open_query` and `fetch_next_page` to `_CustomerApiConnector` and `_SalesApiConnector`. Remove legacy watermark code (`WatermarkStore`, `commit_watermark`, `_track_watermark`, `_watermark_key`, `_latest_updated_at`, `watermark_store` param). Rewrite `fetch_records()` to drain `fetch_next_page()`. Remove `updated_since` constructor param (now handled by `open_query`). |
| `services/ingestion/src/main.py` | Remove `checkpoint_store.get("profile_unifier:phppos_api:watermark:...")` lookup (lines 674-678). Simplify connector construction (lines 679-683) to `connector_type(create_phppos_api_client(source_key))` — no `updated_since`, no `watermark_store`. |
| `services/ingestion/src/tasks.py` | Populate `INCREMENTAL_CONNECTORS` dict with 4 factory callables. |
| `services/ingestion/tests/test_phppos_api_connectors.py` | Replace `test_customer_connector_uses_checkpoint_and_stages_normalized_source_watermark` with an IncrementalConnector-aware test. Remove `CapturingWatermarkStore`, `IncrementalCustomerClient`. Update connector construction calls to drop `updated_since`/`watermark_store` args. |

### New files

| File | Purpose |
|---|---|
| `services/ingestion/tests/test_phppos_incremental_connectors.py` | Unit tests for IncrementalConnector behavior: page-level pagination, mapping, `max_updated_at` tracking, runner integration. |

### Files NOT touched

- `models.py` — no changes needed.
- `connectors/phppos_api/__init__.py` — no new classes to export (existing classes gain the protocol in-place).
- `watermark_runner.py`, `watermark_store.py`, `watermark_scheduler.py` — framework code, untouched.
- `ci_support/selection_manifest.py` — new tests are active by default (no manifest update).
- `test_worker_topology.py` — `run_incremental_task` is already in the task routes dict.

## 3. Ordered Implementation Steps

### Step 1: Add page-level fetch methods to `PhpposApiClient`

In `client.py`, add two public methods that extract the page-fetch + model-validate call
from `iter_customers`/`iter_sales`:

```python
def fetch_customer_page(
    self, cursor: str | None, updated_since: str | None,
) -> CustomerPage:
    return CustomerPage.model_validate(self._get_page("customers", cursor, updated_since))

def fetch_sales_page(
    self, cursor: str | None, updated_since: str | None,
) -> SalesPage:
    return SalesPage.model_validate(self._get_page("sales", cursor, updated_since))
```

Refactor `iter_customers`/`iter_sales` to delegate to these:

```python
def iter_customers(self, *, updated_since: str | None = None) -> Iterator[CustomerRow]:
    cursor: str | None = None
    while True:
        page = self.fetch_customer_page(cursor, updated_since)
        yield from page.data
        if not page.pagination.has_more:
            return
        cursor = page.pagination.next_cursor
```

Same pattern for `iter_sales` → `fetch_sales_page`.

### Step 2: Add IncrementalConnector protocol to existing connectors

Modify `_CustomerApiConnector` and `_SalesApiConnector` in `connectors.py` in-place.

**Remove legacy watermark code from both classes:**
- Delete `WatermarkStore` protocol class (lines 26-27)
- Delete `watermark_store` constructor parameter and `self._watermark_store` attribute
- Delete `_latest_updated_at` attribute
- Delete `commit_watermark()`, `_track_watermark()`, `_watermark_key()` methods
- Delete `updated_since` constructor parameter (now handled by `open_query`)

**Add to `_CustomerApiConnector`:**

```python
def open_query(self, updated_since: datetime | None) -> None:
    self._updated_since_str = (
        updated_since.isoformat() if updated_since is not None else None
    )
    self._cursor: str | None = None

def fetch_next_page(self) -> IncrementalPage:
    page = self._client.fetch_customer_page(self._cursor, self._updated_since_str)
    records: list[dict[str, JsonValue]] = []
    max_ts: datetime | None = None
    for row in page.data:
        values = row.model_dump()
        ts = _parse_source_timestamp(
            values.get("last_modified") or values.get("create_date")
        )
        if ts is not None and (max_ts is None or ts > max_ts):
            max_ts = ts
        for field in _OPTIONAL_CUSTOMER_FIELDS:
            values.setdefault(field, None)
        api_row = ApiRow(values)
        if self.source_key == "eko_phppos":
            records.append(EkoConnector._build_one(api_row))
        else:
            records.append(SpeedZoneConnector._build_envelope_with_customer(api_row))
    self._cursor = page.pagination.next_cursor
    return IncrementalPage(
        records=tuple(records),
        has_more=page.pagination.has_more,
        max_updated_at=max_ts or _EPOCH_FLOOR,
    )
```

Where `_EPOCH_FLOOR = datetime.min.replace(tzinfo=UTC)` — a module-level sentinel for
empty pages.

**Rewrite `fetch_records()` to drain `fetch_next_page`:**

```python
def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
    try:
        self.open_query(None)
        while True:
            page = self.fetch_next_page()
            yield from page.records
            if not page.has_more:
                return
    finally:
        self._client.close()
```

This is the key DRY win: all mapping logic lives in `fetch_next_page()` only.
`fetch_records()` is a thin drain loop for SourceConnector backward compatibility.
No code duplication.

**Same pattern for `_SalesApiConnector`:**
- `open_query(updated_since)` stores ISO string, resets cursor.
- `fetch_next_page()` calls `client.fetch_sales_page(cursor, updated_since_str)`,
  maps each `SaleRow` via the existing `_build_record` method (which stays as-is),
  computes `max_updated_at` from `sale_time`, returns `IncrementalPage`.
- `fetch_records()` rewritten as drain loop.

**Note on `_SalesApiConnector._build_record`:** This method uses `self.source_key` and
the `extract_bike_plate` comparison. It stays as an instance method — no extraction or
duplication needed, since `fetch_next_page()` is on the same class and calls it directly.

**New imports needed in `connectors.py`:**
```python
from datetime import UTC, datetime
from src.incremental_connector import IncrementalConnector, IncrementalPage
```

(`IncrementalConnector` import is for type-checking/documentation only — the protocol is
structural via `@runtime_checkable`, no inheritance needed.)

### Step 3: Simplify `main.py` PHPPOS connector construction

In `main.py`, the PHPPOS API connector construction block (lines 664-683) is simplified.

**Before:**
```python
api_types = {
    "eko_phppos": EkoApiConnector,
    "eko_phppos:sales": EkoSalesApiConnector,
    "speedzone_phppos": SpeedZoneApiConnector,
    "speedzone_phppos:sales": SpeedZoneSalesApiConnector,
}
try:
    connector_type = api_types[source_key]
except KeyError as exc:
    raise ValueError(...) from exc
updated_since = None
if incremental and checkpoint_store is not None:
    updated_since = checkpoint_store.get(
        f"profile_unifier:phppos_api:watermark:{source_key}"
    )
return connector_type(
    create_phppos_api_client(source_key),
    updated_since=updated_since,
    watermark_store=checkpoint_store,
)
```

**After:**
```python
api_types = {
    "eko_phppos": EkoApiConnector,
    "eko_phppos:sales": EkoSalesApiConnector,
    "speedzone_phppos": SpeedZoneApiConnector,
    "speedzone_phppos:sales": SpeedZoneSalesApiConnector,
}
try:
    connector_type = api_types[source_key]
except KeyError as exc:
    raise ValueError(...) from exc
return connector_type(create_phppos_api_client(source_key))
```

The old `profile_unifier:phppos_api:watermark:{source_key}` Redis key read is removed.
The new watermark key (`profile_unifier:watermark:{source_key}`) is managed by
`IngestionWatermark` / `watermark_store.py` and read by the watermark runner — no
`main.py` involvement needed.

The `run_ingestion_task` path for PHPPOS API sources is now full-fetch only (no incremental).
Incremental ingestion goes through `run_incremental_task` exclusively.

### Step 4: Register factories in `INCREMENTAL_CONNECTORS`

In `tasks.py`, populate the `INCREMENTAL_CONNECTORS` dict:

```python
INCREMENTAL_CONNECTORS: dict[str, object] = {
    "eko_phppos": _create_eko_customer_incremental,
    "eko_phppos:sales": _create_eko_sales_incremental,
    "speedzone_phppos": _create_speedzone_customer_incremental,
    "speedzone_phppos:sales": _create_speedzone_sales_incremental,
}
```

Each factory function (module-level, deferred imports to avoid circular deps):

```python
def _create_eko_customer_incremental() -> EkoApiConnector:
    from src.connectors.phppos_api.connectors import EkoApiConnector
    from src.main import create_phppos_api_client
    return EkoApiConnector(create_phppos_api_client("eko_phppos"))
```

Same pattern for the other 3. The factory returns the existing connector class
(which now satisfies both `SourceConnector` and `IncrementalConnector`).

### Step 5: Update existing tests

In `test_phppos_api_connectors.py`:

1. **Remove** `CapturingWatermarkStore` class (legacy watermark test infrastructure).
2. **Remove** `IncrementalCustomerClient` class (legacy incremental test infrastructure).
3. **Remove** `test_customer_connector_uses_checkpoint_and_stages_normalized_source_watermark`.
4. **Update** all `EkoApiConnector(client, ...)` / `SpeedZoneApiConnector(client, ...)` /
   `EkoSalesApiConnector(client, ...)` construction calls to drop `updated_since` and
   `watermark_store` keyword args (now just `ConnectorType(client)`).
5. **Add** a replacement test verifying that `open_query(datetime)` passes the ISO string
   through to the client's `fetch_*_page` method (replacing the removed watermark test).

### Step 6: Write new unit tests

In `services/ingestion/tests/test_phppos_incremental_connectors.py`:

**Test infrastructure:**
- `StubPageClient` — has `fetch_customer_page(cursor, updated_since)` and
  `fetch_sales_page(cursor, updated_since)` returning canned `CustomerPage`/`SalesPage`
  objects from a list. Tracks `(cursor, updated_since)` args per call. Also implements
  `iter_customers`/`iter_sales`/`close` for SourceConnector compat.

**Test cases (all 4 connectors):**

1. **Protocol satisfaction** — `isinstance(connector, IncrementalConnector)` for all 4.
2. **Single-page customer fetch** — `open_query(None)`, `fetch_next_page()` returns mapped
   records with correct envelope structure (source_record_id, identifiers, attributes,
   raw_payload), `has_more=False`.
3. **Single-page sales fetch** — same as above for sales connectors.
4. **Multi-page pagination** — 2 pages with cursor advancement. Verify cursor forwarded
   from page 1's `next_cursor` to page 2's fetch call.
5. **`updated_since` passthrough** — `open_query(datetime(...))` passes the ISO string to
   the client's `fetch_*_page` method.
6. **`max_updated_at` tracking** — verifies the page's `max_updated_at` is the latest
   timestamp from the page's rows.
7. **Empty page** — 0 records, `has_more=False`, `max_updated_at` falls back to epoch floor.
8. **Close delegation** — `close()` calls `client.close()`.
9. **`get_source_key` correctness** — returns the expected key for each connector.
10. **Customer mapping: Eko vs SpeedZone** — verify `_build_one` vs
    `_build_envelope_with_customer` dispatch produces correct envelope structures.
11. **Sales mapping** — decimal coercion, line extraction, `extract_bike_plate` flag
    (True for SpeedZone, False for Eko).
12. **`fetch_records` backward compat** — `fetch_records()` yields the same records as
    `open_query(None)` + draining `fetch_next_page()`.
13. **Registry test** — verify all 4 keys are present in `INCREMENTAL_CONNECTORS` and
    their factories are callable.

**Runner integration test:**

14. **Runner drains pages and advances watermark** — Wire a PHPPOS IncrementalConnector
    (using `StubPageClient` with 2 pages) into `watermark_runner.run_incremental` with
    `FakeRedis` + `FakeNeo4jClient` (patching `_load_exclusion_context` and
    `_process_page_records` as in `test_watermark_runner.py`). Verify:
    - `run_incremental` returns `status="caught_up"`, `pages_processed=2`, correct
      `records_processed` count.
    - `IngestionWatermark` is saved to Redis with `max_updated_at` from the last page.
    - `watermark_start` reflects the initial watermark (or `None` for bootstrap).
15. **Runner safe-stop does not advance watermark** — Same setup but
    `shutdown_signal` returns `True` before page 2. Verify:
    - `run_incremental` returns `status="yielded"`.
    - Redis watermark is NOT updated (no `save_watermark` call for yielded runs).
16. **Runner time-window-closing does not advance watermark** — Same as above but
    `time_window_closing` returns `True`. Verify `status="yielded"`, no watermark advance.

## 4. Risk Assessment & Assumptions

### Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Removing `updated_since` from connector constructor breaks callers | Very Low | Only caller is `main.py` `get_connector` — updated in Step 3. No external callers. |
| Removing `commit_watermark` breaks `_finalize_connector_progress` | None | `_finalize_connector_progress` uses `isinstance(connector, _WatermarkCommitter)` — when PHPPOS connectors no longer implement `commit_watermark`, the check simply returns False. Other connectors (bitrix, whatsadmin) still implement it. No `main.py` change needed. |
| `EkoConnector._build_one` / `SpeedZoneConnector._build_envelope_with_customer` are instance methods | Low | Existing `connectors.py` already calls them as `ClassName._build_one(api_row)` without an instance — effectively static. Verified in code. |
| Adding public methods to `PhpposApiClient` breaks existing tests | Very Low | Additive change (new methods) + trivial refactor of `iter_*` to delegate. Existing tests call `iter_*` and should pass unchanged. |
| Old Redis keys (`profile_unifier:phppos_api:watermark:...`) become orphaned | Very Low | These keys had no TTL and stored the last watermark. They become inert — no code reads or writes them after this change. A cleanup sweep can delete them later if needed. |

### Assumptions

1. `EkoConnector._build_one` and `SpeedZoneConnector._build_envelope_with_customer` are
   stable static-like methods callable without an instance.
2. The PHPPOS API's `updated_since` parameter accepts ISO datetime strings (the existing
   connectors already pass ISO strings).
3. `create_phppos_api_client` in `main.py` correctly handles all 4 source keys (verified:
   `_PHPPOS_SCOPES_BY_SOURCE` maps all 4).
4. No callers of the PHPPOS API connectors pass `updated_since` or `watermark_store`
   outside of `main.py` `get_connector`.
5. The `_OPTIONAL_CUSTOMER_FIELDS` defaulting logic is already in `connectors.py` and
   stays in `fetch_next_page()` — no duplication.

## 5. Validation Plan

### CI pipeline (primary)

Push to PR branch and verify via Woodpecker CI (`wpci home`):
- `ruff check` + `ruff format --check` pass for changed files.
- `mypy --strict` passes for `services/api/src` and `services/ingestion/src`.
- `pytest` passes all test suites including new and updated tests.
- Verify zero net lint warnings added.

Do NOT run pytest, mypy, or ruff locally to verify — the Woodpecker PR pipeline is the
authoritative validation. Exception: one-shot `ruff check --fix` / `ruff format` to
*generate* deterministic fixes (per CLAUDE.md Coding Workflow exception), then push and
let CI verify.

### Deferred (not in scope)

- Integration testing with live PHPPOS API (requires credentials + running API).
- Watermark scheduler group registration for PHPPOS sources (separate task — scheduler config).
- Cleanup of orphaned `profile_unifier:phppos_api:watermark:*` Redis keys.
- Bounded ingestion code cleanup (no PHPPOS-specific bounded code exists — confirmed by grep).
