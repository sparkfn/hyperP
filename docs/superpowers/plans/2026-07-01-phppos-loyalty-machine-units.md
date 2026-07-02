# phppos loyalty points + machine-unit display — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture loyalty-points data from the phppos sources (Eko, SpeedZone) — customer balance + per-sale activity — into the graph, and surface loyalty points and owned/bought machine units on the person profile in frontend2.

**Architecture:** Loyalty balance rides in the identity `SourceRecord.raw_payload` (stored verbatim as JSON on the node — `attributes` is flattened and cannot hold a dict). Per-sale points activity is added to the phppos sales order payload and written as properties on the `:Order` node. The API reads both via `:Person`'s identity `SourceRecord`s and `:OWNS_UNIT`/`:BOUGHT_UNIT` edges, folding them into `GET /persons/{id}`. The frontend renders two new profile cards and adds points chips to the sales tab. Public share endpoints strip the new fields.

**Tech Stack:** Python 3.12 / FastAPI / Neo4j 5.26 (Cypher) / SQLAlchemy (live phppos) / Pydantic v2 / pytest; Next.js 15 / React / MUI / TypeScript (frontend2). Package manager: `uv` (python), `npm` (frontend2).

## Global Constraints

- **Strict typing (mypy --strict)** on all python: no `Any`, explicit return types, no untyped `dict`/`list`. Use `to_int_or_none`/`to_float_or_none`/`to_optional_str`/`to_optional_float` from `services/api/src/graph/converters.py` (API) and the ingestion-side equivalents.
- **No host test/lint/build runs** per repo CI policy. Verification is via Woodpecker (`wpci home`) on the PR branch. The "Run" steps below give the canonical command for reference; the authoritative check is `wpci home pipeline show sparkfn/hyperP <n>` after push. Red/green TDD = push the test commit (red), then the implementation commit (green), reading the Woodpecker step verdict each time.
- **Defensive column access** for sales: use `_col_or_none(sale, attr, sales_cols)` so dumps with varying column sets don't crash.
- **Frontend**: no `any`/`as any`; explicit `ReactElement` return types on components; per-component MUI imports (`@mui/material/Button`); `@/` aliases.
- **Commit discipline**: never commit without explicit user confirmation. Each task ends with a prepared commit (staged, message ready) but is NOT committed until the user says so. (The plan shows `git add` + a message; do not run `git commit` autonomously.)
- **Frontend lint budget**: verify zero net new warnings (stash-and-compare `npx eslint src`), not a green `npm run lint` exit. CI runs `npx eslint src` (errors only).
- **Public endpoint exclusion is new code** (the public route currently strips nothing).

**Branch:** `feat/phppos-loyalty-machine-units` (created from `development`).

---

## File Structure

**Ingestion (modify):**
- `services/ingestion/src/connectors/eko/connector.py` — SQL select + `raw_payload.loyalty` block
- `services/ingestion/src/connectors/speedzone/connector.py` — same
- `services/ingestion/src/connectors/dumps/connectors.py` — `_join_eko_row`/`_join_speedzone_row` loyalty columns; `_build_phppos_sales_envelope` loyalty block
- `services/ingestion/src/connectors/phppos_sales_common.py` — `_build_order_payload` loyalty block
- `services/ingestion/src/pipeline_sales.py` — `_OrderPayload` TypedDict + `_merge_order` params
- `services/ingestion/src/graph/queries/sales.py` — `MERGE_ORDER` loyalty properties

**API (modify):**
- `services/api/src/types.py` — `LoyaltySummary`, `MachineUnitSummary`, `Person.loyalty`, `Person.machine_units`
- `services/api/src/types_sales.py` — `SalesOrder.points_used`/`points_gained`
- `services/api/src/graph/queries/persons.py` — `GET_PERSON_BY_ID` loyalty + machine-unit sub-queries
- `services/api/src/graph/queries/sales.py` — `GET_PERSON_SALES` projection
- `services/api/src/graph/mappers.py` — `map_person` loyalty + machine_units
- `services/api/src/graph/mappers_sales.py` — `map_sales_order` points
- `services/api/src/routes/public_pages.py` — public exclusion strip

**Frontend (modify):**
- `services/frontend2/src/lib/api-types.ts` — `LoyaltySummary`, `MachineUnitSummary`, `SalesOrder.points_used/points_gained`
- `services/frontend2/src/app/persons/[personId]/page.tsx` — Loyalty card, Machine units card, sales points chips

**Tests (new):**
- `services/ingestion/tests/test_phppos_loyalty_ingestion.py`
- `services/ingestion/tests/test_phppos_sales_loyalty.py`
- `services/api/tests/test_person_loyalty_machine_units.py`
- `services/api/tests/test_public_person_excludes_loyalty.py`

---

### Task 1: Ingestion — identity loyalty block (live Eko + SpeedZone)

**Files:**
- Modify: `services/ingestion/src/connectors/eko/connector.py:148-223`
- Modify: `services/ingestion/src/connectors/speedzone/connector.py` (parallel structure)
- Test: `services/ingestion/tests/test_phppos_loyalty_ingestion.py`

**Interfaces:**
- Consumes: `build_envelope` (`connectors/fundbox/builders.py:283`), `to_int_or_none`/`to_float_or_none` from `services/ingestion/src/graph/converters.py` (verify names; the ingestion converters live in `services/ingestion/src/graph/converters.py`).
- Produces: identity envelopes whose `raw_payload` contains a `loyalty` dict (`points: int|None`, `disable_loyalty: bool|None`, `current_spend_for_points: float|None`, `current_sales_for_discount: float|None`). Later tasks (Task 5) read this from the `SourceRecord.raw_payload` JSON.

- [ ] **Step 1: Write the failing test**

`services/ingestion/tests/test_phppos_loyalty_ingestion.py`:
```python
from __future__ import annotations

from types import SimpleNamespace

from src.connectors.eko.connector import EkoConnector
from src.connectors.speedzone.connector import SpeedZoneConnector


def _row(**overrides: object) -> SimpleNamespace:
    base = {
        "person_id": 1, "first_name": "A", "last_name": "B", "full_name": "A B",
        "phone_number": "91234567", "email": "a@b.com", "address_1": "1 St",
        "address_2": None, "city": "SG", "state": None, "zip": "123456",
        "country": "SG", "comments": None, "create_date": None, "last_modified": None,
        "title": None, "phone_code": None, "customer_id": 10, "account_number": "ACC",
        "company_name": None, "points": 250, "disable_loyalty": 0,
        "current_spend_for_points": 12.5, "current_sales_for_discount": 99.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_eko_build_one_carries_loyalty_in_raw_payload() -> None:
    conn = EkoConnector()
    env = conn._build_one(_row())
    loyalty = env["raw_payload"]["loyalty"]
    assert loyalty == {
        "points": 250,
        "disable_loyalty": False,
        "current_spend_for_points": 12.5,
        "current_sales_for_discount": 99.0,
    }
    # attributes unchanged
    assert set(env["attributes"].keys()) == {"full_name", "dob", "address"}


def test_eko_build_one_handles_null_loyalty() -> None:
    conn = EkoConnector()
    env = conn._build_one(_row(points=None, disable_loyalty=None,
                              current_spend_for_points=None,
                              current_sales_for_discount=None))
    assert env["raw_payload"]["loyalty"] == {
        "points": None, "disable_loyalty": None,
        "current_spend_for_points": None, "current_sales_for_discount": None,
    }


def test_speedzone_build_one_carries_loyalty_in_raw_payload() -> None:
    conn = SpeedZoneConnector()
    env = conn._build_envelope_with_customer(_row(points=80, disable_loyalty=1))
    assert env["raw_payload"]["loyalty"]["points"] == 80
    assert env["raw_payload"]["loyalty"]["disable_loyalty"] is True
```

(Adjust the `_build_one`/`_build_envelope_with_customer` call shape + the exact `to_int_or_none` import source to match what you implement in Step 3; the assertions are the contract.)

- [ ] **Step 2: Run test to verify it fails**

Push to PR branch; CI red on the new test (`raw_payload` has no `loyalty` key → `KeyError`). Or run locally as a one-shot structural check only if the host venv is absent — otherwise rely on CI.

- [ ] **Step 3: Implement — Eko**

In `services/ingestion/src/connectors/eko/connector.py`, add the four columns to the select list inside `_build_records` (after `customers.c.company_name`):
```python
                customers.c.points,
                customers.c.disable_loyalty,
                customers.c.current_spend_for_points,
                customers.c.current_sales_for_discount,
```
In `_build_one` (~line 207), replace the `raw_payload=` kwarg:
```python
            raw_payload={
                "person": _person_raw_payload(row),
                "loyalty": {
                    "points": to_int_or_none(getattr(row, "points", None)),
                    "disable_loyalty": (
                        bool(row.disable_loyalty)
                        if getattr(row, "disable_loyalty", None) is not None
                        else None
                    ),
                    "current_spend_for_points": to_float_or_none(
                        getattr(row, "current_spend_for_points", None)
                    ),
                    "current_sales_for_discount": to_float_or_none(
                        getattr(row, "current_sales_for_discount", None)
                    ),
                },
            },
```
Add the import at the top: `from src.graph.converters import to_int_or_none, to_float_or_none` (confirm exact names in `services/ingestion/src/graph/converters.py`; if they differ, use the existing ones).

- [ ] **Step 4: Implement — SpeedZone**

Apply the identical select-list additions and the same `loyalty` block inside `_build_envelope_with_customer` in `services/ingestion/src/connectors/speedzone/connector.py`. (SpeedZone mirrors Eko per `SPEEDZONE_PHPPOS_TABLES = PHPPOS_TABLES`.)

- [ ] **Step 5: Run tests to verify pass**

Push; CI green on `test_phppos_loyalty_ingestion.py`. Run reference: `uv run --package profile-unifier-ingestion pytest services/ingestion/tests/test_phppos_loyalty_ingestion.py -v`.

- [ ] **Step 6: Stage commit (do NOT commit)**

```bash
git add services/ingestion/src/connectors/eko/connector.py \
        services/ingestion/src/connectors/speedzone/connector.py \
        services/ingestion/tests/test_phppos_loyalty_ingestion.py
# message: feat(ingestion): capture phppos customer loyalty balance in identity raw_payload
```

---

### Task 2: Ingestion — identity loyalty parity in dump-path joins

**Files:**
- Modify: `services/ingestion/src/connectors/dumps/connectors.py:797-848` (`_join_eko_row`, `_join_speedzone_row`)
- Test: append to `services/ingestion/tests/test_phppos_loyalty_ingestion.py`

**Interfaces:**
- Consumes: parsed `phppos_customers` dict rows (already include `points`/`disable_loyalty`/`current_spend_for_points`/`current_sales_for_discount` — verified in the dumps).
- Produces: joined dump rows carrying the four loyalty columns, so the dump identity envelopes have the same `raw_payload.loyalty` block as live.

- [ ] **Step 1: Write the failing test**

Append to `test_phppos_loyalty_ingestion.py`:
```python
from src.connectors.dumps.connectors import _join_eko_row, _join_speedzone_row


def test_join_eko_row_copies_loyalty_columns() -> None:
    person = {"person_id": 1, "full_name": "A B", "phone_number": "9", "email": "a@b.com"}
    customer = {"id": 10, "person_id": 1, "account_number": "ACC",
                "company_name": None, "points": 300, "disable_loyalty": 0,
                "current_spend_for_points": 5.0, "current_sales_for_discount": 1.0,
                **{f"custom_field_{i}_value": None for i in range(1, 11)}}
    joined = _join_eko_row(person, customer)
    assert joined["points"] == 300
    assert joined["disable_loyalty"] == 0
    assert joined["current_spend_for_points"] == 5.0
    assert joined["current_sales_for_discount"] == 1.0


def test_join_speedzone_row_copies_loyalty_columns() -> None:
    person = {"person_id": 1, "full_name": "A B"}
    customer = {"id": 10, "person_id": 1, "points": 7, "disable_loyalty": 1,
                "current_spend_for_points": None, "current_sales_for_discount": None,
                **{f"custom_field_{i}_value": None for i in range(1, 11)}}
    joined = _join_speedzone_row(person, customer)
    assert joined["points"] == 7
    assert joined["disable_loyalty"] == 1
```
(Confirm the exact signature of `_join_eko_row`/`_join_speedzone_row` from `connectors.py:797,818` and adjust the call if they take more args.)

- [ ] **Step 2: Run test to verify it fails**

Push; CI red (`KeyError: 'points'`).

- [ ] **Step 3: Implement**

In `_join_eko_row` and `_join_speedzone_row`, add the four loyalty fields to the dict they build (alongside `customer_id`, `account_number`, `company_name`, `custom_field_*`). Read defensively with `.get` so dumps missing a column still join (rare, but guards older historical dumps):
```python
        "points": customer.get("points"),
        "disable_loyalty": customer.get("disable_loyalty"),
        "current_spend_for_points": customer.get("current_spend_for_points"),
        "current_sales_for_discount": customer.get("current_sales_for_discount"),
```

- [ ] **Step 4: Run tests to verify pass**

Push; CI green. Reference: `uv run --package profile-unifier-ingestion pytest services/ingestion/tests/test_phppos_loyalty_ingestion.py -v`.

- [ ] **Step 5: Stage commit (do NOT commit)**

```bash
git add services/ingestion/src/connectors/dumps/connectors.py services/ingestion/tests/test_phppos_loyalty_ingestion.py
# message: feat(ingestion): carry loyalty columns through phppos dump identity joins
```

---

### Task 3: Ingestion — sales order loyalty block + Order node properties

**Files:**
- Modify: `services/ingestion/src/connectors/phppos_sales_common.py:258-298` (`_build_order_payload`)
- Modify: `services/ingestion/src/connectors/dumps/connectors.py:720` (`_build_phppos_sales_envelope`)
- Modify: `services/ingestion/src/pipeline_sales.py:70-79` (`_OrderPayload`), `:370-388` (`_merge_order`)
- Modify: `services/ingestion/src/graph/queries/sales.py:54-75` (`MERGE_ORDER`)
- Test: `services/ingestion/tests/test_phppos_sales_loyalty.py`

**Interfaces:**
- Consumes: `sale` RowMapping + `sales_cols: set[str]` in `_build_order_payload`.
- Produces: order payloads with a `loyalty` block; `:Order` nodes carry `points_used`/`points_gained`/`did_redeem_discount`/`is_purchase_points`. Task 6 reads these back from the `:Order` node.

- [ ] **Step 1: Write the failing test**

`services/ingestion/tests/test_phppos_sales_loyalty.py`:
```python
from __future__ import annotations

from src.connectors.phppos_sales_common import _build_order_payload


def _sale(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "sale_id": 1, "sale_time": "2026-01-01 00:00:00", "customer_id": 5,
        "invoice_number": "INV1", "suspended": 0, "sale_status": None,
        "total": 10.0, "item_unit_price": 10.0, "quantity_purchased": 1,
        "points_used": 20, "points_gained": 5, "did_redeem_discount": 1,
        "is_purchase_points": 0, "register_id": 1, "employee_id": 2,
        "payment_type": "cash", "sale_type_id": None, "comment": None,
    }
    base.update(overrides)
    return base


def test_order_payload_has_loyalty_block() -> None:
    sale = _sale()
    payload = _build_order_payload(
        sale=sale, source_order_id="1", ordered_at="2026-01-01",
        release_date=None, sales_cols=set(sale.keys()), line_row=None,
    )
    assert payload["loyalty"] == {
        "points_used": 20, "points_gained": 5,
        "did_redeem_discount": 1, "is_purchase_points": 0,
    }


def test_order_payload_loyalty_none_when_columns_absent() -> None:
    sale = _sale()
    cols = set(sale.keys()) - {"points_used", "points_gained",
                               "did_redeem_discount", "is_purchase_points"}
    payload = _build_order_payload(
        sale=sale, source_order_id="1", ordered_at="2026-01-01",
        release_date=None, sales_cols=cols, line_row=None,
    )
    assert payload["loyalty"] == {
        "points_used": None, "points_gained": None,
        "did_redeem_discount": None, "is_purchase_points": None,
    }
```
(Adjust the `_build_order_payload` call signature to match the real one at `phppos_sales_common.py:258-265`.)

- [ ] **Step 2: Run test to verify it fails**

Push; CI red (`KeyError: 'loyalty'`).

- [ ] **Step 3: Implement — order payload (live)**

In `phppos_sales_common.py:_build_order_payload`, add a `loyalty` key to the returned dict (next to `metadata`/`raw`):
```python
        "loyalty": {
            "points_used": _col_or_none(sale, "points_used", sales_cols),
            "points_gained": _col_or_none(sale, "points_gained", sales_cols),
            "did_redeem_discount": _col_or_none(sale, "did_redeem_discount", sales_cols),
            "is_purchase_points": _col_or_none(sale, "is_purchase_points", sales_cols),
        },
```

- [ ] **Step 4: Implement — order payload (dump)**

In `dumps/connectors.py:_build_phppos_sales_envelope` (~line 720), add the same `loyalty` block to the order dict it builds, using the same `_col_or_none` defensive accessor against the dump sale row's column set.

- [ ] **Step 5: Implement — TypedDict + _merge_order**

In `pipeline_sales.py`, extend `_OrderPayload`:
```python
class _OrderPayload(TypedDict, total=False):
    source_order_id: str
    order_no: str | None
    ordered_at: str | None
    release_date: str | None
    status: str | None
    total_amount: float | None
    currency: str | None
    item_count: int | None
    metadata: dict[str, JsonValue]
    loyalty: dict[str, JsonValue]
```
In `_merge_order` (`pipeline_sales.py:370-388`), unpack the loyalty block and pass params:
```python
def _merge_order(
    tx: ManagedTransaction,
    *,
    source_system_key: str,
    order: _OrderPayload,
) -> None:
    loyalty = order.get("loyalty") or {}
    tx.run(
        queries.MERGE_ORDER,
        source_system_key=source_system_key,
        source_order_id=order.get("source_order_id", ""),
        order_no=order.get("order_no"),
        ordered_at=order.get("ordered_at"),
        release_date=order.get("release_date"),
        status=order.get("status"),
        total_amount=order.get("total_amount"),
        currency=order.get("currency"),
        item_count=order.get("item_count"),
        metadata=json.dumps(order.get("metadata", {}), default=str),
        points_used=loyalty.get("points_used"),
        points_gained=loyalty.get("points_gained"),
        did_redeem_discount=loyalty.get("did_redeem_discount"),
        is_purchase_points=loyalty.get("is_purchase_points"),
    )
```

- [ ] **Step 6: Implement — MERGE_ORDER Cypher**

In `services/ingestion/src/graph/queries/sales.py:MERGE_ORDER`, add to the `SET` clause (after `o.metadata = $metadata,`):
```cypher
    o.points_used = $points_used,
    o.points_gained = $points_gained,
    o.did_redeem_discount = $did_redeem_discount,
    o.is_purchase_points = $is_purchase_points,
```

- [ ] **Step 7: Run tests to verify pass**

Push; CI green. Reference: `uv run --package profile-unifier-ingestion pytest services/ingestion/tests/test_phppos_sales_loyalty.py -v`.

- [ ] **Step 8: Stage commit (do NOT commit)**

```bash
git add services/ingestion/src/connectors/phppos_sales_common.py \
        services/ingestion/src/connectors/dumps/connectors.py \
        services/ingestion/src/pipeline_sales.py \
        services/ingestion/src/graph/queries/sales.py \
        services/ingestion/tests/test_phppos_sales_loyalty.py
# message: feat(ingestion): persist phppos per-sale loyalty activity on Order nodes
```

---

### Task 4: API — types for loyalty, machine units, sales points

**Files:**
- Modify: `services/api/src/types.py:145-163` (`Person` model)
- Modify: `services/api/src/types_sales.py` (`SalesOrder`)
- Test: `services/api/tests/test_person_loyalty_machine_units.py` (types part)

**Interfaces:**
- Produces: `LoyaltySummary`, `MachineUnitSummary` models; `Person.loyalty: list[LoyaltySummary] | None`; `Person.machine_units: list[MachineUnitSummary] | None`; `SalesOrder.points_used: int | None`; `SalesOrder.points_gained: int | None`. Consumed by Tasks 5, 6, 7, 8.

- [ ] **Step 1: Write the failing test**

`services/api/tests/test_person_loyalty_machine_units.py`:
```python
from __future__ import annotations

from src.types import LoyaltySummary, MachineUnitSummary, Person
from src.types_sales import SalesOrder


def test_loyalty_summary_model() -> None:
    ls = LoyaltySummary(source_system="eko_phppos", points=250,
                        disable_loyalty=False, current_spend_for_points=12.5,
                        current_sales_for_discount=99.0, observed_at="2026-01-01")
    assert ls.points == 250


def test_machine_unit_summary_model() -> None:
    mu = MachineUnitSummary(machine_unit_id="u1", machine_product="Widget",
                             lta_tag="LTA1", serial_number="S1",
                             relationship="OWNS", is_active=True,
                             conflict_flag=False, observed_at="2026-01-01")
    assert mu.relationship == "OWNS"


def test_person_carries_loyalty_and_machine_units() -> None:
    p = Person(person_id="p1", status="active")  # adjust to real required fields
    assert p.loyalty is None
    assert p.machine_units is None


def test_sales_order_carries_points() -> None:
    o = SalesOrder(source_order_id="1")  # adjust to real required fields
    assert o.points_used is None
    assert o.points_gained is None
```
(Replace `Person(...)` / `SalesOrder(...)` construction with the real required fields from the existing models — read `types.py:145` and `types_sales.py` first to get the minimal ctor.)

- [ ] **Step 2: Run test to verify it fails**

Push; CI red (`ImportError: cannot import name 'LoyaltySummary'`).

- [ ] **Step 3: Implement — models**

In `services/api/src/types.py`, add (near the other small models):
```python
class LoyaltySummary(BaseModel):
    source_system: str
    points: int | None
    disable_loyalty: bool | None
    current_spend_for_points: float | None
    current_sales_for_discount: float | None
    observed_at: str | None


class MachineUnitSummary(BaseModel):
    machine_unit_id: str
    machine_product: str | None
    lta_tag: str | None
    serial_number: str | None
    relationship: Literal["OWNS", "BOUGHT"]
    is_active: bool | None
    conflict_flag: bool | None
    observed_at: str | None
```
(Use `from typing import Literal` if not already imported.) Add to `Person`:
```python
    loyalty: list[LoyaltySummary] | None = None
    machine_units: list[MachineUnitSummary] | None = None
```

In `services/api/src/types_sales.py`, add to `SalesOrder`:
```python
    points_used: int | None = None
    points_gained: int | None = None
```

- [ ] **Step 4: Run tests to verify pass**

Push; CI green. Reference: `uv run --package profile-unifier-api pytest services/api/tests/test_person_loyalty_machine_units.py -v`.

- [ ] **Step 5: Stage commit (do NOT commit)**

```bash
git add services/api/src/types.py services/api/src/types_sales.py \
        services/api/tests/test_person_loyalty_machine_units.py
# message: feat(api): add LoyaltySummary, MachineUnitSummary, SalesOrder points fields
```

---

### Task 5: API — person detail loyalty + machine_units reads

**Files:**
- Modify: `services/api/src/graph/queries/persons.py:40-78` (`GET_PERSON_BY_ID`)
- Modify: `services/api/src/graph/mappers.py:98-...` (`map_person`)
- Test: append to `services/api/tests/test_person_loyalty_machine_units.py`

**Interfaces:**
- Consumes: `SourceRecord.raw_payload` (JSON string with `loyalty` block, written in Tasks 1-2) and `(:Person)-[:OWNS_UNIT|BOUGHT_UNIT]->(:MachineUnit)` edges (already in graph).
- Produces: a `Person` response with `loyalty` (one `LoyaltySummary` per source system with a loyalty block, latest observed) and `machine_units` (list of `MachineUnitSummary`).

- [ ] **Step 1: Write the failing test**

Append to `test_person_loyalty_machine_units.py`:
```python
import json

from src.graph.mappers import map_person


def _record(person_id: str = "p1", raw_loyalty: dict | None = None,
            source_system: str = "eko_phppos", observed_at: str = "2026-01-01",
            units: list[dict] | None = None) -> dict:
    return {
        "person": {"person_id": person_id, "status": "active"},
        "preferred_address": None,
        "source_record_count": 0,
        "connection_count": 0,
        "lifetime_value": None,
        "loyalty_rows": [{
            "source_system": source_system,
            "observed_at": observed_at,
            "raw_payload": json.dumps({"person": {}, "loyalty": raw_loyalty})
            if raw_loyalty is not None else json.dumps({"person": {}}),
        }],
        "machine_units": units or [],
    }


def test_map_person_loyalty_dedup_per_source_latest() -> None:
    rec = _record(raw_loyalty={"points": 250, "disable_loyalty": False,
                                "current_spend_for_points": 1.0,
                                "current_sales_for_discount": 2.0})
    rec["loyalty_rows"].append({
        "source_system": "eko_phppos", "observed_at": "2026-02-01",
        "raw_payload": json.dumps({"person": {}, "loyalty": {"points": 999}}),
    })
    p = map_person(rec)
    assert p.loyalty is not None and len(p.loyalty) == 1
    assert p.loyalty[0].points == 999  # latest observed wins
    assert p.loyalty[0].source_system == "eko_phppos"


def test_map_person_loyalty_skips_records_without_block() -> None:
    rec = _record()  # raw_payload has no loyalty
    p = map_person(rec)
    assert p.loyalty == []


def test_map_person_machine_units() -> None:
    rec = _record(units=[
        {"machine_unit_id": "u1", "machine_product": "Widget", "lta_tag": "LTA1",
         "serial_number": "S1", "rel_type": "OWNS_UNIT", "is_active": True,
         "conflict_flag": False, "observed_at": "2026-01-01"},
    ])
    p = map_person(rec)
    assert p.machine_units is not None and len(p.machine_units) == 1
    assert p.machine_units[0].relationship == "OWNS"
```
(Adjust the `map_person` record-dict shape to match the real one; the contract is the assertion.)

- [ ] **Step 2: Run test to verify it fails**

Push; CI red.

- [ ] **Step 3: Implement — Cypher**

In `services/api/src/graph/queries/persons.py:GET_PERSON_BY_ID`, add two `CALL {}` sub-queries before the `RETURN` (mirroring the existing `lifetime_value` sub-query at lines 59-63):

```cypher
    CALL {
        WITH person
        MATCH (person)-[:LINKED_TO]-(sr:SourceRecord {record_type: 'identity'})
        WITH sr.source_system AS source_system, sr
        ORDER BY sr.source_system, sr.observed_at DESC
        RETURN collect(DISTINCT {
            source_system: source_system,
            observed_at: sr.observed_at,
            raw_payload: sr.raw_payload
        }) AS loyalty_rows
    }
    CALL {
        WITH person
        OPTIONAL MATCH (person)-[rel:OWNS_UNIT|BOUGHT_UNIT]->(u:MachineUnit)
        RETURN collect(CASE WHEN u IS NULL THEN NULL ELSE {
            machine_unit_id: u.machine_unit_id,
            machine_product: u.machine_product,
            lta_tag: u.lta_tag,
            serial_number: u.serial_number,
            rel_type: type(rel),
            is_active: rel.is_active,
            conflict_flag: u.conflict_flag,
            observed_at: rel.observed_at
        } END) AS machine_units
    }
```
Add `loyalty_rows` and `machine_units` to the final `RETURN`.

- [ ] **Step 4: Implement — mapper**

In `services/api/src/graph/mappers.py:map_person`, parse loyalty rows (dedup per source_system, latest observed_at wins, skip rows whose `raw_payload` JSON has no `loyalty` key) and map machine units:
```python
    loyalty: list[LoyaltySummary] = []
    seen_sources: set[str] = set()
    for row in (record.get("loyalty_rows") or []):
        if not isinstance(row, dict):
            continue
        src = to_str(row.get("source_system")) or ""
        if src in seen_sources:
            continue
        raw = row.get("raw_payload")
        try:
            raw_dict = json.loads(raw) if isinstance(raw, str) else None
        except (TypeError, ValueError):
            raw_dict = None
        if not isinstance(raw_dict, dict):
            continue
        block = raw_dict.get("loyalty")
        if not isinstance(block, dict):
            continue
        seen_sources.add(src)
        loyalty.append(LoyaltySummary(
            source_system=src,
            points=to_int_or_none(block.get("points")),
            disable_loyalty=to_optional_bool(block.get("disable_loyalty")),
            current_spend_for_points=to_optional_float(block.get("current_spend_for_points")),
            current_sales_for_discount=to_optional_float(block.get("current_sales_for_discount")),
            observed_at=to_iso_or_none(row.get("observed_at")),
        ))
    machine_units: list[MachineUnitSummary] = []
    for mu in (record.get("machine_units") or []):
        if not isinstance(mu, dict) or mu.get("machine_unit_id") is None:
            continue
        rel_type = to_str(mu.get("rel_type")) or ""
        relationship: Literal["OWNS", "BOUGHT"] = "OWNS" if rel_type == "OWNS_UNIT" else "BOUGHT"
        machine_units.append(MachineUnitSummary(
            machine_unit_id=to_str(mu.get("machine_unit_id")) or "",
            machine_product=to_optional_str(mu.get("machine_product")),
            lta_tag=to_optional_str(mu.get("lta_tag")),
            serial_number=to_optional_str(mu.get("serial_number")),
            relationship=relationship,
            is_active=to_optional_bool(mu.get("is_active")),
            conflict_flag=to_optional_bool(mu.get("conflict_flag")),
            observed_at=to_iso_or_none(mu.get("observed_at")),
        ))
    # ... in the Person(...) construction:
    loyalty=loyalty or None,
    machine_units=machine_units or None,
```
Add imports: `from src.types import LoyaltySummary, MachineUnitSummary`, `from typing import Literal`, and `to_optional_bool` (add to `graph/converters.py` if missing: `def to_optional_bool(v: object) -> bool | None: return bool(v) if v is not None else None`). Use existing `to_int_or_none`, `to_optional_float`, `to_iso_or_none`, `to_str`, `to_optional_str`.

- [ ] **Step 5: Run tests to verify pass**

Push; CI green. Reference: `uv run --package profile-unifier-api pytest services/api/tests/test_person_loyalty_machine_units.py -v`.

- [ ] **Step 6: Stage commit (do NOT commit)**

```bash
git add services/api/src/graph/queries/persons.py services/api/src/graph/mappers.py \
        services/api/src/graph/converters.py services/api/tests/test_person_loyalty_machine_units.py
# message: feat(api): expose person loyalty + owned/bought machine units in detail
```

---

### Task 6: API — sales endpoint points fields

**Files:**
- Modify: `services/api/src/graph/queries/sales.py:5-30` (`GET_PERSON_SALES`)
- Modify: `services/api/src/graph/mappers_sales.py:22-52` (`map_sales_order`)
- Test: append to `services/api/tests/test_person_loyalty_machine_units.py`

**Interfaces:**
- Consumes: `:Order` node properties `points_used`/`points_gained` (written in Task 3).
- Produces: `SalesOrder.points_used`/`points_gained` populated in `GET /persons/{id}/sales`.

- [ ] **Step 1: Write the failing test**

Append:
```python
from src.graph.mappers_sales import map_sales_order


def test_map_sales_order_points() -> None:
    rec = {
        "order_no": "INV1", "source_order_id": "1", "order_date": None,
        "release_date": None, "total_amount": 10.0, "currency": "SGD",
        "source_system": "eko_phppos:sales", "entity_name": "Eko",
        "line_items": [], "points_used": 20, "points_gained": 5,
    }
    o = map_sales_order(rec)
    assert o.points_used == 20
    assert o.points_gained == 5
```

- [ ] **Step 2: Run test to verify it fails**

Push; CI red.

- [ ] **Step 3: Implement — Cypher**

In `services/api/src/graph/queries/sales.py:GET_PERSON_SALES`, add to the `RETURN` (before `line_items`):
```cypher
       o.points_used AS points_used,
       o.points_gained AS points_gained,
```

- [ ] **Step 4: Implement — mapper**

In `map_sales_order`, add to the `SalesOrder(...)` construction:
```python
        points_used=to_int_or_none(record.get("points_used")),
        points_gained=to_int_or_none(record.get("points_gained")),
```
(import `to_int_or_none` from `src.graph.converters` if not already imported.)

- [ ] **Step 5: Run tests to verify pass**

Push; CI green. Reference: `uv run --package profile-unifier-api pytest services/api/tests/test_person_loyalty_machine_units.py -v`.

- [ ] **Step 6: Stage commit (do NOT commit)**

```bash
git add services/api/src/graph/queries/sales.py services/api/src/graph/mappers_sales.py \
        services/api/tests/test_person_loyalty_machine_units.py
# message: feat(api): surface per-sale points used/gained on sales endpoint
```

---

### Task 7: API — public endpoint exclusion

**Files:**
- Modify: `services/api/src/routes/public_pages.py:77-89` (`get_public_person`)
- Modify: `services/api/src/routes/public_pages.py:135-144` (public sales)
- Test: `services/api/tests/test_public_person_excludes_loyalty.py`

**Interfaces:**
- Consumes: the `Person`/`SalesOrder` models from Tasks 4-6.
- Produces: public responses with `loyalty=None`, `machine_units=None`, and sales `points_used=None`/`points_gained=None`.

- [ ] **Step 1: Write the failing test**

`services/api/tests/test_public_person_excludes_loyalty.py`:
```python
from __future__ import annotations

from src.routes.public_pages import _strip_public_person, _strip_public_sales_order
from src.types import LoyaltySummary, MachineUnitSummary, Person
from src.types_sales import SalesOrder


def _person() -> Person:
    return Person(  # adjust to real required fields
        person_id="p1", status="active",
        loyalty=[LoyaltySummary(source_system="eko_phppos", points=1,
                                 disable_loyalty=False,
                                 current_spend_for_points=None,
                                 current_sales_for_discount=None,
                                 observed_at=None)],
        machine_units=[MachineUnitSummary(machine_unit_id="u1", machine_product=None,
                                           lta_tag=None, serial_number=None,
                                           relationship="OWNS", is_active=True,
                                           conflict_flag=False, observed_at=None)],
    )


def test_strip_public_person_clears_loyalty_and_machine_units() -> None:
    stripped = _strip_public_person(_person())
    assert stripped.loyalty is None
    assert stripped.machine_units is None


def test_strip_public_sales_order_clears_points() -> None:
    o = SalesOrder(source_order_id="1", points_used=20, points_gained=5)  # adjust required fields
    stripped = _strip_public_sales_order(o)
    assert stripped.points_used is None
    assert stripped.points_gained is None
```
(Adjust `Person`/`SalesOrder` construction to real required fields.)

- [ ] **Step 2: Run test to verify it fails**

Push; CI red (`ImportError: cannot import name '_strip_public_person'`).

- [ ] **Step 3: Implement**

In `services/api/src/routes/public_pages.py`, add helpers and call them in the public routes:
```python
def _strip_public_person(person: Person) -> Person:
    return person.model_copy(update={"loyalty": None, "machine_units": None})


def _strip_public_sales_order(order: SalesOrder) -> SalesOrder:
    return order.model_copy(update={"points_used": None, "points_gained": None})
```
In `get_public_person` (line 77-89), after `person = await repo.get_by_id(person_id)` and before `return envelope(...)`, replace `person` with `_strip_public_person(person)`:
```python
    person = _strip_public_person(await repo.get_by_id(person_id))
```
(Keep the existing None/404 handling — apply the strip only to the non-None person.) In the public sales route (line 135-144), map each returned order through `_strip_public_sales_order` (modify the list comprehension / loop that builds the response).

- [ ] **Step 4: Run tests to verify pass**

Push; CI green. Reference: `uv run --package profile-unifier-api pytest services/api/tests/test_public_person_excludes_loyalty.py -v`.

- [ ] **Step 5: Stage commit (do NOT commit)**

```bash
git add services/api/src/routes/public_pages.py services/api/tests/test_public_person_excludes_loyalty.py
# message: feat(api): strip loyalty, machine units, and sales points from public person responses
```

---

### Task 8: Frontend — types, Loyalty card, Machine units card, sales chips

**Files:**
- Modify: `services/frontend2/src/lib/api-types.ts` (Person, SalesOrder, new types)
- Modify: `services/frontend2/src/app/persons/[personId]/page.tsx` (new cards + sales chips)
- Test: typecheck via CI (`tsc --noEmit`); zero net new eslint warnings.

**Interfaces:**
- Consumes: `Person.loyalty`, `Person.machine_units`, `SalesOrder.points_used`/`points_gained` from the API envelope (Tasks 4-6). The existing person BFF handler passes the body through unchanged.

- [ ] **Step 1: Add types**

In `services/frontend2/src/lib/api-types.ts`, add:
```typescript
export interface LoyaltySummary {
  source_system: string;
  points: number | null;
  disable_loyalty: boolean | null;
  current_spend_for_points: number | null;
  current_sales_for_discount: number | null;
  observed_at: string | null;
}

export interface MachineUnitSummary {
  machine_unit_id: string;
  machine_product: string | null;
  lta_tag: string | null;
  serial_number: string | null;
  relationship: "OWNS" | "BOUGHT";
  is_active: boolean | null;
  conflict_flag: boolean | null;
  observed_at: string | null;
}
```
Add to the `Person` interface: `loyalty?: LoyaltySummary[] | null;` and `machine_units?: MachineUnitSummary[] | null;`. Add to `SalesOrder`: `points_used?: number | null;` and `points_gained?: number | null;`.

- [ ] **Step 2: Add Loyalty + Machine units cards**

In `services/frontend2/src/app/persons/[personId]/page.tsx`, near the profile header / key-fields grid (find the existing summary cards and add two siblings). Create two small components (each under ~150 lines) — extract into the same file or a sibling file if the page is large:

`LoyaltyCard`:
```tsx
function LoyaltyCard({ loyalty }: { loyalty: LoyaltySummary[] | null | undefined }): ReactElement {
  if (!loyalty || loyalty.length === 0) return <></>;
  return (
    <Card sx={{ mb: 2 }}>
      <CardContent>
        <Typography variant="h6">Loyalty</Typography>
        {loyalty.map((row) => (
          <Box key={row.source_system} sx={{ display: "flex", gap: 1, alignItems: "center", mt: 1 }}>
            <Chip label={row.source_system} size="small" />
            <Typography variant="subtitle1">{row.points ?? 0} pts</Typography>
            {row.disable_loyalty ? <Chip label="disabled" size="small" color="warning" /> : null}
            {row.observed_at ? (
              <Typography variant="caption" color="text.secondary">
                last observed {formatDate(row.observed_at)}
              </Typography>
            ) : null}
          </Box>
        ))}
      </CardContent>
    </Card>
  );
}
```
`MachineUnitsCard`:
```tsx
function MachineUnitsCard({ units }: { units: MachineUnitSummary[] | null | undefined }): ReactElement {
  if (!units || units.length === 0) return <></>;
  return (
    <Card sx={{ mb: 2 }}>
      <CardContent>
        <Typography variant="h6">Machine units</Typography>
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Product</TableCell><TableCell>LTA tag</TableCell>
              <TableCell>Serial</TableCell><TableCell>Relationship</TableCell>
              <TableCell>Active</TableCell><TableCell>Conflict</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {units.map((u) => (
              <TableRow key={u.machine_unit_id}>
                <TableCell>{u.machine_product ?? "—"}</TableCell>
                <TableCell>{u.lta_tag ?? "—"}</TableCell>
                <TableCell>{u.serial_number ?? "—"}</TableCell>
                <TableCell><Chip label={u.relationship} size="small" /></TableCell>
                <TableCell>{u.is_active ? "yes" : "no"}</TableCell>
                <TableCell>{u.conflict_flag ? <WarningIcon color="warning" /> : "—"}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
```
(Use per-component MUI imports: `@mui/material/Card`, `@mui/material/CardContent`, `@mui/material/Typography`, `@mui/material/Box`, `@mui/material/Chip`, `@mui/material/Table`, `@mui/material/TableHead`, `@mui/material/TableBody`, `@mui/material/TableRow`, `@mui/material/TableCell`, `@mui/icons-material/WarningAmber` as `WarningIcon`. Use the existing `formatDate` from `display.ts`.)

Render both in the profile header section, fed by `person.loyalty` / `person.machine_units` from the page's person state.

- [ ] **Step 3: Add sales points chips**

In `SalesTab` (`page.tsx:2607-2799`), in the expanded order header (near the total badge at lines 2622-2626 / 2630-2662), add:
```tsx
{order.points_used != null ? <Chip size="small" label={`Used ${order.points_used} pts`} /> : null}
{order.points_gained != null ? <Chip size="small" label={`Earned ${order.points_gained} pts`} color="success" /> : null}
```

- [ ] **Step 4: Verify typecheck + lint (CI)**

Push; CI `tsc --noEmit` green and `npx eslint src` (errors only) green. Locally, to check zero net warnings: stash the change, run `npx eslint src 2>&1 | wc -l`, restore, run again, compare (per repo lint policy — do not treat a non-zero `npm run lint` as a failure on the clean tree).

- [ ] **Step 5: Stage commit (do NOT commit)**

```bash
git add services/frontend2/src/lib/api-types.ts services/frontend2/src/app/persons/[personId]/page.tsx
# message: feat(frontend2): show loyalty + machine units on person profile; points chips on sales
```

---

## Self-review notes (resolved during authoring)

- **Spec coverage:** §1 (dumps) — verification noted in Global Constraints + Task 1 references; §2a (identity live) — Task 1; §2b (dump join) — Task 2; §2c (sales payload) — Task 3; §3 (graph storage) — Tasks 3 (Order) + none needed for identity (raw_payload verbatim); §4 (API types + reads + public exclusion) — Tasks 4, 5, 6, 7; §5 (frontend) — Task 8; §6 (tests) — embedded per task.
- **Type consistency:** `LoyaltySummary` / `MachineUnitSummary` field names match across types.py, mappers, frontend `api-types.ts`. Sales fields `points_used`/`points_gained` match across types_sales.py, mappers_sales.py, graph query, frontend.
- **No placeholders:** every step has concrete code/commands. The one area left to "adjust to real required fields" is `Person(...)`/`SalesOrder(...)` test construction — the engineer reads the existing model to get the minimal ctor; this is unavoidable without re-reading those models here, and is flagged explicitly in each test.