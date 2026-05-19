# Machine Unit Exclusions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow ingestion to skip noisy machine-unit evidence by stable normalized product+identifier pairs, and add the Servicing Labour / 1186#1506 exclusion to the local config.

**Architecture:** Extend the existing JSON-backed hard-exclusion config with `machine_unit_identifiers`, normalize those values into `ExclusionContext`, and filter `MachineUnitObservation` before graph writes. Sales ingestion must skip creating/linking excluded units; chat ingestion must skip resolving/linking excluded units.

**Tech Stack:** Python 3.12, dataclasses, pytest, Neo4j query wrappers, existing ingestion exclusion helpers.

---

## File Structure

- Modify `services/ingestion/src/exclusion_config.py` to load a typed list of machine-unit exclusion pairs from JSON.
- Modify `services/ingestion/src/exclusions.py` to normalize configured machine-unit pairs and expose `is_excluded_machine_unit_observation()`.
- Modify `services/ingestion/src/pipeline_sales.py` to accept `ExclusionContext` in `_write_machine_unit_observations()` and skip excluded sales observations before `UPSERT_MACHINE_UNIT`.
- Modify `services/ingestion/src/pipeline.py` to load the same exclusion context for chat machine-unit mentions and skip excluded chat observations before `RESOLVE_EXISTING_MACHINE_UNIT_FOR_CHAT`.
- Modify `config/ingestion-exclusions.local.json` to add the requested product+serial exclusion.
- Modify `config/ingestion-exclusions.example.json` to document the new key with an empty list.
- Test in `services/ingestion/tests/test_exclusion_config.py`, `services/ingestion/tests/test_exclusions.py`, and `services/ingestion/tests/test_machine_unit_queries.py`.

---

### Task 1: Load machine-unit identifier exclusions from JSON

**Files:**
- Modify: `services/ingestion/src/exclusion_config.py`
- Test: `services/ingestion/tests/test_exclusion_config.py`

- [ ] **Step 1: Write the failing config loader test**

Add a `machine_unit_identifiers` array to `test_load_exclusion_file_returns_arrays()` and assert it loads unchanged:

```python
path.write_text(
    "{"
    '"phones":["+6512345678"],'
    '"emails":["ops@example.com"],'
    '"email_domains":["ada.asia"],'
    '"names":["Ada Ops"],'
    '"source_ids":["staff-1"],'
    '"machine_unit_identifiers":[{"machine_product":"Servicing Labour","serial_number":"1186#1506"}]'
    "}",
    encoding="utf-8",
)

loaded = load_exclusion_file(str(path))

assert loaded.phones == ["+6512345678"]
assert loaded.emails == ["ops@example.com"]
assert loaded.email_domains == ["ada.asia"]
assert loaded.names == ["Ada Ops"]
assert loaded.source_ids == ["staff-1"]
assert loaded.machine_unit_identifiers == [
    {"machine_product": "Servicing Labour", "serial_number": "1186#1506"}
]
```

Also add this assertion to `test_load_exclusion_file_blank_path_returns_empty()`:

```python
assert loaded.machine_unit_identifiers == []
```

- [ ] **Step 2: Run the focused failing test**

Run: `uv run pytest services/ingestion/tests/test_exclusion_config.py::test_load_exclusion_file_returns_arrays -v`

Expected: FAIL with `AttributeError: 'ExclusionFile' object has no attribute 'machine_unit_identifiers'`.

- [ ] **Step 3: Implement typed JSON loading**

In `services/ingestion/src/exclusion_config.py`, add these definitions after imports:

```python
class MachineUnitIdentifierExclusion(TypedDict, total=False):
    machine_product: str
    lta_tag: str
    serial_number: str
```

Update imports:

```python
from typing import TypedDict, cast
```

Add the field to `ExclusionFile`:

```python
machine_unit_identifiers: list[MachineUnitIdentifierExclusion] = field(default_factory=list)
```

Add this helper after `_str_list()`:

```python
def _machine_unit_identifier_list(
    raw: JsonValue,
    *,
    path: Path,
) -> list[MachineUnitIdentifierExclusion]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"Invalid ingestion exclusions JSON: {path}")
    values: list[MachineUnitIdentifierExclusion] = []
    for value in raw:
        if not isinstance(value, dict):
            raise ValueError(f"Invalid ingestion exclusions JSON: {path}")
        item: MachineUnitIdentifierExclusion = {}
        for key in ("machine_product", "lta_tag", "serial_number"):
            raw_field = value.get(key)
            if raw_field is None:
                continue
            if not isinstance(raw_field, str):
                raise ValueError(f"Invalid ingestion exclusions JSON: {path}")
            item[key] = raw_field
        values.append(item)
    return values
```

Update `load_exclusion_file()` return construction:

```python
machine_unit_identifiers=_machine_unit_identifier_list(
    payload.get("machine_unit_identifiers"),
    path=path,
),
```

- [ ] **Step 4: Verify config loader tests pass**

Run: `uv run pytest services/ingestion/tests/test_exclusion_config.py -v`

Expected: PASS.

---

### Task 2: Normalize and match machine-unit exclusions

**Files:**
- Modify: `services/ingestion/src/exclusions.py`
- Test: `services/ingestion/tests/test_exclusions.py`

- [ ] **Step 1: Write failing exclusion behavior tests**

Add imports in `test_exclusions.py`:

```python
from src.machine_units import MachineUnitObservation
from src.models import QualityFlag
```

Add `is_excluded_machine_unit_observation` to the `from src.exclusions import (...)` import list.

Add this test:

```python
def test_build_exclusion_context_normalizes_machine_unit_identifiers() -> None:
    context = build_exclusion_context(
        company_mobile_numbers=[],
        company_email_addresses=[],
        internal_person_names=[],
        file_exclusions=ExclusionFile(
            machine_unit_identifiers=[
                {"machine_product": " Servicing   Labour ", "serial_number": "1186#1506"}
            ]
        ),
    )

    observation = MachineUnitObservation(
        source_kind="sales",
        source_system_key="speedzone_phppos:sales",
        source_record_id="sale-1",
        serial_number=" 1186#1506 ",
        machine_product="servicing labour",
        confidence=1.0,
        quality_flag=QualityFlag.VALID,
    )

    assert is_excluded_machine_unit_observation(observation, context)
```

Add this test:

```python
def test_machine_unit_exclusion_requires_same_product_and_identifier() -> None:
    context = build_exclusion_context(
        company_mobile_numbers=[],
        company_email_addresses=[],
        internal_person_names=[],
        file_exclusions=ExclusionFile(
            machine_unit_identifiers=[
                {"machine_product": "Servicing Labour", "serial_number": "1186#1506"}
            ]
        ),
    )
    different_serial = MachineUnitObservation(
        source_kind="sales",
        source_system_key="speedzone_phppos:sales",
        source_record_id="sale-2",
        serial_number="1186#9999",
        machine_product="Servicing Labour",
        confidence=1.0,
        quality_flag=QualityFlag.VALID,
    )
    different_product = MachineUnitObservation(
        source_kind="sales",
        source_system_key="speedzone_phppos:sales",
        source_record_id="sale-3",
        serial_number="1186#1506",
        machine_product="Useful Product",
        confidence=1.0,
        quality_flag=QualityFlag.VALID,
    )

    assert not is_excluded_machine_unit_observation(different_serial, context)
    assert not is_excluded_machine_unit_observation(different_product, context)
```

- [ ] **Step 2: Run the focused failing tests**

Run: `uv run pytest services/ingestion/tests/test_exclusions.py::test_build_exclusion_context_normalizes_machine_unit_identifiers services/ingestion/tests/test_exclusions.py::test_machine_unit_exclusion_requires_same_product_and_identifier -v`

Expected: FAIL because `is_excluded_machine_unit_observation` does not exist.

- [ ] **Step 3: Implement normalized exclusion matching**

In `services/ingestion/src/exclusions.py`, update imports:

```python
from src.machine_units import (
    MachineUnitObservation,
    normalize_lta_tag,
    normalize_machine_product,
    normalize_serial_number,
)
```

Add this frozen dataclass before `ExclusionContext`:

```python
@dataclass(frozen=True)
class MachineUnitIdentifierKey:
    machine_product: str
    lta_tag: str | None = None
    serial_number: str | None = None
```

Add this field to `ExclusionContext`:

```python
machine_unit_identifiers: frozenset[MachineUnitIdentifierKey] = field(default_factory=frozenset)
```

Add this helper before `build_exclusion_context()`:

```python
def normalized_machine_unit_identifier_set(
    values: list[MachineUnitIdentifierExclusion],
) -> frozenset[MachineUnitIdentifierKey]:
    keys: set[MachineUnitIdentifierKey] = set()
    for value in values:
        machine_product = normalize_machine_product(value.get("machine_product"))
        if machine_product is None:
            continue
        lta_tag = normalize_lta_tag(value.get("lta_tag"))
        serial_number = normalize_serial_number(value.get("serial_number"))
        if lta_tag is None and serial_number is None:
            continue
        keys.add(
            MachineUnitIdentifierKey(
                machine_product=machine_product,
                lta_tag=lta_tag,
                serial_number=serial_number,
            )
        )
    return frozenset(keys)
```

Update the `src.exclusion_config` import:

```python
from src.exclusion_config import ExclusionFile, MachineUnitIdentifierExclusion
```

Update `build_exclusion_context()`:

```python
machine_unit_identifiers=normalized_machine_unit_identifier_set(
    file_exclusions.machine_unit_identifiers
),
```

Add this function after `is_excluded_source_id()`:

```python
def is_excluded_machine_unit_observation(
    observation: MachineUnitObservation,
    context: ExclusionContext,
) -> bool:
    machine_product = normalize_machine_product(observation.machine_product)
    if machine_product is None:
        return False
    lta_tag = normalize_lta_tag(observation.lta_tag)
    serial_number = normalize_serial_number(observation.serial_number)
    return any(
        excluded.machine_product == machine_product
        and (
            (excluded.lta_tag is not None and excluded.lta_tag == lta_tag)
            or (
                excluded.serial_number is not None
                and excluded.serial_number == serial_number
            )
        )
        for excluded in context.machine_unit_identifiers
    )
```

- [ ] **Step 4: Verify exclusion tests pass**

Run: `uv run pytest services/ingestion/tests/test_exclusions.py -v`

Expected: PASS.

---

### Task 3: Skip excluded machine units in sales and chat graph writes

**Files:**
- Modify: `services/ingestion/src/pipeline_sales.py`
- Modify: `services/ingestion/src/pipeline.py`
- Test: `services/ingestion/tests/test_machine_unit_queries.py`

- [ ] **Step 1: Write failing sales and chat tests**

In `services/ingestion/tests/test_machine_unit_queries.py`, add imports:

```python
from src.exclusion_config import ExclusionFile
from src.exclusions import build_exclusion_context
from src.pipeline_sales import _write_machine_unit_observations
```

Add this transaction test double after `_ChatMachineUnitTx`:

```python
class _SalesMachineUnitTx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **kwargs: object) -> _Result:
        self.calls.append((query, kwargs))
        if query == queries.UPSERT_MACHINE_UNIT:
            return _Result({"machine_unit_id": "unit-1"})
        return _Result()
```

Add this helper:

```python
def _machine_unit_exclusion_context() -> object:
    return build_exclusion_context(
        company_mobile_numbers=[],
        company_email_addresses=[],
        internal_person_names=[],
        file_exclusions=ExclusionFile(
            machine_unit_identifiers=[
                {"machine_product": "Servicing Labour", "serial_number": "1186#1506"}
            ]
        ),
    )
```

Add this sales test:

```python
def test_sales_machine_unit_observation_skips_excluded_product_serial_pair() -> None:
    tx = _SalesMachineUnitTx()

    _write_machine_unit_observations(
        cast(ManagedTransaction, tx),
        source_system_key="speedzone_phppos:sales",
        source_record_pk="sr-1",
        source_record_id="sale-1",
        source_order_id="order-1",
        observed_at="2026-05-18T10:00:00+00:00",
        line_items=[
            {
                "source_line_id": "line-1",
                "serial_number": "1186#1506",
                "product": {"display_name": "Servicing Labour"},
            }
        ],
        person_id="person-1",
        exclusion_context=_machine_unit_exclusion_context(),
    )

    assert tx.calls == []
```

Add this chat test:

```python
def test_chat_machine_unit_observation_skips_excluded_product_serial_pair() -> None:
    tx = _ChatMachineUnitTx(["unit-1"])
    pipeline = IngestPipeline(cast(Neo4jClient, object()))
    envelope = SourceRecordEnvelope(
        source_system="eko",
        source_record_id="chat-1",
        record_type=RecordType.CONVERSATION,
        observed_at="2026-05-18T10:00:00+00:00",
        record_hash="sha256:chat-1",
        raw_payload={
            "inquiries": [
                {
                    "machine_product": "Servicing Labour",
                    "serial_number": "1186#1506",
                    "notes": "noisy unit",
                }
            ]
        },
        extraction_confidence=0.82,
        extraction_method="test_fixture",
        conversation_ref={"thread_id": "thread-1"},
    )

    pipeline._write_chat_machine_unit_observations(  # noqa: SLF001
        cast(ManagedTransaction, tx),
        envelope=envelope,
        source_record_pk="sr-1",
        exclusion_context=_machine_unit_exclusion_context(),
    )

    assert tx.calls == []
```

- [ ] **Step 2: Run the focused failing tests**

Run: `uv run pytest services/ingestion/tests/test_machine_unit_queries.py::test_sales_machine_unit_observation_skips_excluded_product_serial_pair services/ingestion/tests/test_machine_unit_queries.py::test_chat_machine_unit_observation_skips_excluded_product_serial_pair -v`

Expected: FAIL because the functions do not accept `exclusion_context` and do not skip the observation.

- [ ] **Step 3: Implement sales filtering**

In `services/ingestion/src/pipeline_sales.py`, update imports:

```python
from src.exclusions import ExclusionContext, is_excluded_machine_unit_observation
```

Add this parameter to `_write_machine_unit_observations()`:

```python
exclusion_context: ExclusionContext,
```

Add this at the top of the observation loop:

```python
if is_excluded_machine_unit_observation(observation, exclusion_context):
    continue
```

Update the existing call in `ingest_sales_record()` to build/pass the context:

```python
settings = get_settings()
exclusion_context = build_exclusion_context(
    company_mobile_numbers=settings.company_mobile_numbers,
    company_email_addresses=settings.company_email_addresses,
    internal_person_names=settings.internal_person_names,
    file_exclusions=load_exclusion_file(settings.ingestion_exclusions_file),
)
```

Pass `exclusion_context=exclusion_context` to `_write_machine_unit_observations()`.

Update `_drain_one_pending_sale()` to accept `exclusion_context: ExclusionContext`, pass it to `_write_machine_unit_observations()`, and update `drain_pending_customer_sales()` to build the same context once before `_work()` and pass it into `_drain_one_pending_sale()`.

- [ ] **Step 4: Implement chat filtering**

In `services/ingestion/src/pipeline.py`, update imports:

```python
from src.config import get_settings
from src.exclusion_config import load_exclusion_file
from src.exclusions import build_exclusion_context, ExclusionContext, is_excluded_machine_unit_observation
```

Add this parameter to `_write_chat_machine_unit_observations()`:

```python
exclusion_context: ExclusionContext,
```

Add this at the top of the observation loop:

```python
if is_excluded_machine_unit_observation(observation, exclusion_context):
    continue
```

At the call site in `ingest()`, build the context before calling `_write_chat_machine_unit_observations()`:

```python
settings = get_settings()
exclusion_context = build_exclusion_context(
    company_mobile_numbers=settings.company_mobile_numbers,
    company_email_addresses=settings.company_email_addresses,
    internal_person_names=settings.internal_person_names,
    file_exclusions=load_exclusion_file(settings.ingestion_exclusions_file),
)
```

Pass `exclusion_context=exclusion_context` into `_write_chat_machine_unit_observations()`.

Update the existing test helper `_write_chat_machine_unit_observations()` in `test_machine_unit_queries.py` to pass an empty context:

```python
exclusion_context=build_exclusion_context(
    company_mobile_numbers=[],
    company_email_addresses=[],
    internal_person_names=[],
    file_exclusions=ExclusionFile(),
),
```

- [ ] **Step 5: Verify machine-unit tests pass**

Run: `uv run pytest services/ingestion/tests/test_machine_unit_queries.py -v`

Expected: PASS.

---

### Task 4: Add the requested local exclusion and verify targeted suite

**Files:**
- Modify: `config/ingestion-exclusions.local.json`
- Modify: `config/ingestion-exclusions.example.json`

- [ ] **Step 1: Update the local config**

Change `config/ingestion-exclusions.local.json` to include:

```json
"machine_unit_identifiers": [
  {
    "machine_product": "Servicing Labour",
    "serial_number": "1186#1506"
  }
]
```

Keep the existing phone/domain/name/source-id values unchanged.

- [ ] **Step 2: Update the example config**

Change `config/ingestion-exclusions.example.json` to include:

```json
"machine_unit_identifiers": []
```

- [ ] **Step 3: Run targeted tests**

Run: `uv run pytest services/ingestion/tests/test_exclusion_config.py services/ingestion/tests/test_exclusions.py services/ingestion/tests/test_machine_unit_queries.py -v`

Expected: PASS.

- [ ] **Step 4: Run lint for touched ingestion code**

Run: `uv run --package profile-unifier-ingestion ruff check services/ingestion/src/exclusion_config.py services/ingestion/src/exclusions.py services/ingestion/src/pipeline.py services/ingestion/src/pipeline_sales.py services/ingestion/tests/test_exclusion_config.py services/ingestion/tests/test_exclusions.py services/ingestion/tests/test_machine_unit_queries.py`

Expected: PASS.

---

## Self-Review

- Spec coverage: The plan adds stable product+identifier config support, skips excluded machine-unit writes in sales and chat paths, and adds the requested `Servicing Labour` / `1186#1506` local exclusion.
- Placeholder scan: No TBD/TODO/fill-in steps remain.
- Type consistency: The config key is consistently `machine_unit_identifiers`; the internal context stores `MachineUnitIdentifierKey`; all call sites pass `ExclusionContext`.
