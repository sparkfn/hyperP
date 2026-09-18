# Plan: Incremental SG Rental-Flat Ingestion via Watermark

**Issue:** #437
**Branch:** `issue-437-incremental-sg-rental-flats`
**Blueprint:** Issue #436 / PR #460 (SG bankruptcy incremental connector)

---

## 1 Objective and scope

Add an `IncrementalConnector` implementation for SG rental flats, mirroring the
existing SG bankruptcy incremental connector (`bankruptcy_incremental.py`).  The
new connector uses cursor-based pagination and `updated_since` filtering against
the scraper export endpoint, advancing the watermark only on full cursor drain.

**In scope:** wire-model additions, new incremental connector class, task-registry
entry, unit tests.

**Out of scope / do NOT touch:**
- `rental_flats.py` (dump connector) — unchanged.
- `rental_flats_api.py` (legacy offset-based API connector) — unchanged.
- `config.py` — no new settings; hardcode `max_attempts=3` default (config has no
  `sgrentalflats_api_max_attempts` field; matches bankruptcy's default).
- `__init__.py` — bankruptcy incremental is not exported there; neither is this.

**Prerequisite:** the scraper endpoint at `/integrations/hyperp/rental-flats` must
already support `updated_since` + keyset cursor params and the cursor-page response
shape.  This plan does not modify the scraper.

---

## 2 Acceptance criteria mapping

| # | Acceptance criterion | Deliverable |
|---|---|---|
| AC-1 | Wire models support `updated_at` and cursor pagination | Task 1: add `updated_at` to `RentalFlatRow`, add `RentalFlatCursorPage` model |
| AC-2 | New `SGGovernmentRentalFlatsIncrementalConnector` implements `IncrementalConnector` | Task 2: new file `rental_flats_incremental.py` |
| AC-3 | `sgrentalflats` registered in `INCREMENTAL_CONNECTORS` | Task 3: factory + dict entry in `tasks.py` |
| AC-4 | Watermark advances only on full cursor drain | Inherited from `watermark_runner`; tested in Task 4 integration test |
| AC-5 | Dump connector and legacy API connector continue to work | No files modified; verified by existing tests |
| AC-6 | Comprehensive unit tests | Task 4: 14 test cases |
| AC-7 | Strict typing, trailing newlines, line length ≤ 100 | All tasks |

---

## 3 Detailed file changes

### Task 1 — Wire model additions

**File:** `services/ingestion/src/connectors/sggov/rental_flats_api_models.py`

Add `updated_at` optional field to `RentalFlatRow` and a new cursor-page model.
The existing `RentalFlatPage` (offset-based) is untouched — the legacy API
connector depends on it.

```python
# --- RentalFlatRow (line 19-30): add one field after is_active ---
class RentalFlatRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    block_no: str
    street_name: str
    postal_code: str
    flat_type: str
    first_seen_at: datetime
    last_seen_at: datetime
    is_active: bool
    town: RentalFlatTown
    updated_at: datetime | None = None          # <-- NEW (AC-1)


# --- NEW model after RentalFlatPage ---
class RentalFlatCursorPage(BaseModel):
    """Cursor-based page for the incremental export endpoint."""

    model_config = ConfigDict(extra="forbid")

    items: list[RentalFlatRow]
    next_cursor: str | None
```

**Changes:** +9 lines (1 field + 1 new class).  `RentalFlatPage` untouched.

---

### Task 2 — Incremental connector

**New file:** `services/ingestion/src/connectors/sggov/rental_flats_incremental.py`

Mirrors `bankruptcy_incremental.py` structurally.  Key differences:

| Aspect | Bankruptcy | Rental Flats |
|---|---|---|
| Endpoint | `/api/v1/export/bankruptcy-records` | `/integrations/hyperp/rental-flats` |
| Wire page model | `BankruptcyExportPage` | `RentalFlatCursorPage` |
| Envelope builder | `build_api_envelope(item)` | `_build_envelope(item)` (local, calls `build_rental_flat_envelope` + `_legacy_dump_text`/`_postgres_dump_datetime` from `rental_flats_api.py`) |
| Source key | `sgbankruptcy` | `sgrentalflats` |
| Error prefix | `"SG bankruptcy ..."` | `"SG rental flats ..."` |

**Class signature:**

```python
class SGGovernmentRentalFlatsIncrementalConnector:
    """Incremental connector for SG rental flats
    via the scraper export API."""

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

    def open_query(
        self, updated_since: datetime | None,
    ) -> None: ...

    def fetch_next_page(self) -> IncrementalPage: ...
    def get_source_key(self) -> str: ...  # returns "sgrentalflats"
    def close(self) -> None: ...
```

**`fetch_next_page` logic:**

1. Assert `_query_opened`.
2. Build params: `limit`, optional `cursor`, optional `updated_since`.
3. Call `_get_page(params)` (retry with exponential backoff on transport
   errors / 429 / 5xx — identical to bankruptcy).
4. Validate as `RentalFlatCursorPage`.
5. For each item, build envelope via `_build_envelope(item)`:
   - Imports `build_rental_flat_envelope` from `rental_flats.py`.
   - Imports `_legacy_dump_text`, `_postgres_dump_datetime` from
     `rental_flats_api.py`.
   - Applies `_legacy_dump_text` to string fields (`block_no`,
     `street_name`, `postal_code`, `flat_type`, `town.name`,
     `town.map_id`, `town.map_zone`) and `_postgres_dump_datetime`
     to `last_seen_at`.
   - Constructs `raw_payload` dict matching `rental_flats_api.py`
     lines 140-158.
   - Calls `build_rental_flat_envelope(...)` with same kwargs as
     `SGGovernmentRentalFlatsApiConnector.fetch_records`.
6. Compute `max_updated_at`: `max(updated_at or last_seen_at)` for each
   item, normalized to UTC via `_ensure_utc`.  Empty page: fall back to
   `updated_since` or `datetime.min(UTC)`.
7. Detect cursor stall (repeated/seen cursor → `RuntimeError`).
8. Return `IncrementalPage(records, has_more, max_updated_at)`.

**Estimated size:** ~130 lines (close to bankruptcy's 121).

---

### Task 3 — Task registry

**File:** `services/ingestion/src/tasks.py`

Add after `INCREMENTAL_CONNECTORS["sgbankruptcy"]` (line 2246):

```python
def _create_sgrentalflats_incremental() -> IncrementalConnector:
    from src.connectors.sggov.rental_flats_incremental import (
        SGGovernmentRentalFlatsIncrementalConnector,
    )

    settings = get_settings()
    return SGGovernmentRentalFlatsIncrementalConnector(
        base_url=settings.sgrentalflats_api_base_url,
        api_key=settings.sgrentalflats_api_key.get_secret_value(),
        page_size=settings.sgrentalflats_api_page_size,
        timeout_seconds=settings.sgrentalflats_api_timeout_seconds,
    )


INCREMENTAL_CONNECTORS["sgrentalflats"] = _create_sgrentalflats_incremental
```

**Changes:** +13 lines.  No new config fields; uses existing
`sgrentalflats_api_*` settings.

---

### Task 4 — Unit tests

**New file:** `services/ingestion/tests/test_sggov_rental_flats_incremental.py`

Mirrors `test_sggov_bankruptcy_incremental.py` structure.  14 test cases:

| # | Test name | Validates |
|---|---|---|
| 1 | `test_incremental_connector_satisfies_protocol` | `isinstance(connector, IncrementalConnector)` |
| 2 | `test_incremental_fetch_all_pages_bootstrap` | Multi-page bootstrap (no `updated_since`), cursor advances, `has_more` flags, final page `has_more=False` |
| 3 | `test_incremental_fetch_with_updated_since_delta` | `updated_since` param forwarded in query string |
| 4 | `test_incremental_updated_at_fallback_to_last_seen_at` | `max_updated_at` uses `updated_at` when present, falls back to `last_seen_at` when `None` |
| 5 | `test_incremental_empty_page_returns_no_records` | Empty items → `records == ()`, `has_more=False` |
| 6 | `test_incremental_cursor_stall_raises` | Repeated cursor → `RuntimeError("did not advance")` |
| 7 | `test_incremental_retries_server_errors` | 503 retried, eventual 200 succeeds |
| 8 | `test_incremental_retries_transport_errors` | `ConnectError` retried, eventual 200 succeeds |
| 9 | `test_incremental_propagates_auth_failure` | 401 → `HTTPStatusError` without retry |
| 10 | `test_incremental_fetch_before_open_raises` | `fetch_next_page` before `open_query` → `RuntimeError` |
| 11 | `test_incremental_get_source_key` | Returns `"sgrentalflats"` |
| 12 | `test_incremental_connector_registered_in_task_registry` | `"sgrentalflats" in INCREMENTAL_CONNECTORS` |
| 13 | `test_incremental_rental_flats_run_advances_watermark_on_drain` | Integration: full drain → watermark advanced; interrupted → watermark not advanced |
| 14 | `test_incremental_timestamps_normalized_to_utc` | Naive and aware timestamps both produce UTC `max_updated_at` |

**Fixtures:**

```python
def _make_item(**overrides: Any) -> dict[str, Any]:
    """Minimal RentalFlatRow-shaped dict."""
    defaults: dict[str, Any] = {
        "id": 1,
        "block_no": "123",
        "street_name": "ANG MO KIO AVE 3",
        "postal_code": "560123",
        "flat_type": "3-Room",
        "first_seen_at": "2026-01-01T00:00:00Z",
        "last_seen_at": "2026-01-02T00:00:00Z",
        "is_active": True,
        "town": {
            "id": 1,
            "name": "ANG MO KIO",
            "map_id": "AMKT",
            "map_zone": None,
        },
    }
    defaults.update(overrides)
    return defaults


def _connector(
    handler: Any,
    *,
    sleeper: Any = None,
) -> SGGovernmentRentalFlatsIncrementalConnector:
    kwargs: dict[str, Any] = {
        "base_url": "https://rentalflats.test",
        "api_key": "secret",
        "page_size": 10,
        "http": httpx.Client(
            transport=httpx.MockTransport(handler),
        ),
    }
    if sleeper is not None:
        kwargs["sleeper"] = sleeper
    return SGGovernmentRentalFlatsIncrementalConnector(**kwargs)
```

**Integration test (test 13):** uses `_FakeGraphClient`, `fakeredis.FakeRedis`,
patches `_load_exclusion_context` and `_process_page_records` — identical
pattern to bankruptcy.

**Estimated size:** ~450 lines.

---

## 4 Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Scraper endpoint not yet updated with cursor + `updated_since` support | Medium | Connector is correct regardless; integration requires scraper deploy. Tests mock the endpoint. |
| `_legacy_dump_text` / `_postgres_dump_datetime` are private functions in `rental_flats_api.py` | Low | These are module-level functions, importable despite underscore prefix. Stable — they have not changed since creation. If they become unavailable, copy the 15-line implementations locally. |
| No `sgrentalflats_api_max_attempts` config field | None | Constructor defaults to `max_attempts=3`. If config field is needed later, add it — no code change required in connector. |
| `RentalFlatRow.updated_at` addition breaks existing `extra="forbid"` parsing if scraper sends it before consumer is deployed | None | `updated_at` defaults to `None`; Pydantic `extra="forbid"` only rejects *unknown* keys. An absent key is fine. |

---

## 5 Test plan

**Unit tests (14 cases in Task 4)** cover:
- Protocol conformance
- Bootstrap (full scan) and delta (with `updated_since`) modes
- Multi-page cursor traversal
- `max_updated_at` computation (with/without `updated_at`, UTC normalization)
- Empty pages
- Cursor stall detection
- Retry behavior (5xx, transport errors)
- Auth failure propagation (no retry on 4xx)
- Guard: `fetch_next_page` before `open_query`
- Source key correctness
- Task registry presence
- Integration: watermark advancement on drain vs. non-advancement on yield

**CI validation:** push to PR branch, verify Woodpecker PR pipeline passes
(ruff check, ruff format, mypy --strict, pytest).

---

## 6 Implementation order

1. Task 1 — Wire models (`rental_flats_api_models.py`)
2. Task 2 — Incremental connector (`rental_flats_incremental.py`)
3. Task 3 — Task registry (`tasks.py`)
4. Task 4 — Unit tests (`test_sggov_rental_flats_incremental.py`)

Tasks 1-3 are sequential (each builds on the previous).
Task 4 depends on all three.
