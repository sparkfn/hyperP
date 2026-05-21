# MachineUnit Product Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make MachineUnit identity product-scoped by requiring product/variant/model name plus either LTA tag or serial number.

**Architecture:** Keep extraction unchanged except for validation semantics. Add product normalization in `src.machine_units`, pass product values through existing sales/chat pipeline upsert calls, and scope the MachineUnit Cypher match by normalized product plus unit identifier.

**Tech Stack:** Python 3.12, dataclasses, Neo4j Cypher query constants, uv, pytest, ruff, mypy strict.

---

## File Structure

- Modify `services/ingestion/src/machine_units.py`
  - Owns machine-unit observation type, LTA/serial/product normalization, and validation.
- Modify `services/ingestion/src/pipeline.py`
  - Chat conversation machine-unit writer; must pass product parameters into `UPSERT_MACHINE_UNIT`.
- Modify `services/ingestion/src/pipeline_sales.py`
  - Sales machine-unit writer; must pass product parameters into `UPSERT_MACHINE_UNIT`.
- Modify `services/ingestion/src/graph/queries/machine_units.py`
  - Owns product-scoped MachineUnit upsert query and persisted MachineUnit properties.
- Modify `services/ingestion/tests/test_machine_units.py`
  - Unit tests for product normalization and product-required validation.
- Modify `services/ingestion/tests/test_machine_unit_queries.py`
  - Query-shape tests for product-scoped matching and persisted properties.

---

### Task 1: Add product normalization and validation tests

**Files:**
- Modify: `services/ingestion/tests/test_machine_units.py`
- Modify: `services/ingestion/src/machine_units.py`

- [ ] **Step 1: Write failing tests for product normalization and product-required validation**

Update the import block in `services/ingestion/tests/test_machine_units.py` to include `normalize_machine_product`:

```python
from src.machine_units import (
    MachineUnitObservation,
    normalize_lta_tag,
    normalize_machine_product,
    normalize_serial_number,
    valid_machine_unit_observation,
)
```

Add these tests after `test_normalize_serial_number_preserves_meaningful_punctuation`:

```python
def test_normalize_machine_product_uppercases_and_collapses_spaces() -> None:
    assert normalize_machine_product("  Forklift   X / variant 2  ") == "FORKLIFT X / VARIANT 2"


def test_normalize_machine_product_rejects_placeholders() -> None:
    assert normalize_machine_product("n/a") is None
```

Replace `test_observation_is_valid_when_one_identifier_normalizes` with these two tests:

```python
def test_observation_without_product_is_invalid_even_with_identifier() -> None:
    obs = MachineUnitObservation(
        lta_tag=None,
        serial_number=" sn-09 ",
        machine_product=None,
        unit_label="Unit 7",
        source_kind="sales",
        source_system_key="speedzone_phppos",
        source_record_id="sale-1",
        observed_at="2026-05-14T00:00:00",
        confidence=1.0,
        quality_flag=QualityFlag.VALID,
        raw_context="line-1",
    )

    assert valid_machine_unit_observation(obs) is False


def test_observation_is_valid_with_product_and_one_identifier() -> None:
    obs = MachineUnitObservation(
        lta_tag=None,
        serial_number=" sn-09 ",
        machine_product="Model A",
        unit_label="Unit 7",
        source_kind="sales",
        source_system_key="speedzone_phppos",
        source_record_id="sale-1",
        observed_at="2026-05-14T00:00:00",
        confidence=1.0,
        quality_flag=QualityFlag.VALID,
        raw_context="line-1",
    )

    assert valid_machine_unit_observation(obs) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest services/ingestion/tests/test_machine_units.py -v
```

Expected: FAIL because `normalize_machine_product` is not defined/importable.

- [ ] **Step 3: Implement product normalization and validation**

In `services/ingestion/src/machine_units.py`, add this function after `normalize_serial_number`:

```python
def normalize_machine_product(value: str | None) -> str | None:
    cleaned = _clean(value)
    if cleaned is None:
        return None
    normalized = " ".join(cleaned.split())
    return normalized if normalized and normalized not in _PLACEHOLDERS else None
```

Replace `valid_machine_unit_observation` with:

```python
def valid_machine_unit_observation(observation: MachineUnitObservation) -> bool:
    has_product = normalize_machine_product(observation.machine_product) is not None
    has_identifier = (
        normalize_lta_tag(observation.lta_tag) is not None
        or normalize_serial_number(observation.serial_number) is not None
    )
    return has_product and has_identifier
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest services/ingestion/tests/test_machine_units.py -v
```

Expected: PASS.

---

### Task 2: Make the MachineUnit upsert query product-scoped

**Files:**
- Modify: `services/ingestion/tests/test_machine_unit_queries.py`
- Modify: `services/ingestion/src/graph/queries/machine_units.py`

- [ ] **Step 1: Write failing query-shape tests**

Replace `test_machine_unit_upsert_handles_lta_or_serial_matches` in `services/ingestion/tests/test_machine_unit_queries.py` with:

```python
def test_machine_unit_upsert_scopes_lta_and_serial_matches_by_product() -> None:
    query = queries.UPSERT_MACHINE_UNIT

    assert "normalized_machine_product: $normalized_machine_product" in query
    assert "normalized_lta_tag: $normalized_lta_tag" in query
    assert "normalized_serial_number: $normalized_serial_number" in query
    assert "machine_product: $machine_product" in query
    assert "machine_unit_identifier_conflict" in query
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest services/ingestion/tests/test_machine_unit_queries.py::test_machine_unit_upsert_scopes_lta_and_serial_matches_by_product -v
```

Expected: FAIL because the query does not yet include `normalized_machine_product` or `machine_product`.

- [ ] **Step 3: Update `UPSERT_MACHINE_UNIT`**

Replace the `UPSERT_MACHINE_UNIT` string in `services/ingestion/src/graph/queries/machine_units.py` with:

```python
UPSERT_MACHINE_UNIT = """
OPTIONAL MATCH (lta_unit:MachineUnit {
    normalized_machine_product: $normalized_machine_product,
    normalized_lta_tag: $normalized_lta_tag
})
  WHERE $normalized_machine_product IS NOT NULL AND $normalized_lta_tag IS NOT NULL
OPTIONAL MATCH (serial_unit:MachineUnit {
    normalized_machine_product: $normalized_machine_product,
    normalized_serial_number: $normalized_serial_number
})
  WHERE $normalized_machine_product IS NOT NULL AND $normalized_serial_number IS NOT NULL
WITH lta_unit, serial_unit
CALL {
  WITH lta_unit, serial_unit
  WITH lta_unit, serial_unit
  WHERE lta_unit IS NOT NULL AND serial_unit IS NOT NULL AND lta_unit <> serial_unit
  SET lta_unit.conflict_flag = true,
      serial_unit.conflict_flag = true,
      lta_unit.conflict_reason = 'machine_unit_identifier_conflict',
      serial_unit.conflict_reason = 'machine_unit_identifier_conflict',
      lta_unit.updated_at = datetime(),
      serial_unit.updated_at = datetime()
  RETURN lta_unit AS unit, true AS conflict
  UNION
  WITH lta_unit, serial_unit
  WITH coalesce(lta_unit, serial_unit) AS matched
  WHERE matched IS NOT NULL
  SET matched.machine_product = coalesce(matched.machine_product, $machine_product),
      matched.normalized_machine_product = coalesce(matched.normalized_machine_product, $normalized_machine_product),
      matched.lta_tag = coalesce(matched.lta_tag, $lta_tag),
      matched.normalized_lta_tag = coalesce(matched.normalized_lta_tag, $normalized_lta_tag),
      matched.serial_number = coalesce(matched.serial_number, $serial_number),
      matched.normalized_serial_number = coalesce(matched.normalized_serial_number, $normalized_serial_number),
      matched.updated_at = datetime()
  RETURN matched AS unit, false AS conflict
  UNION
  WITH lta_unit, serial_unit
  WITH lta_unit, serial_unit
  WHERE lta_unit IS NULL AND serial_unit IS NULL
  CREATE (created:MachineUnit {
      machine_unit_id: randomUUID(),
      machine_product: $machine_product,
      normalized_machine_product: $normalized_machine_product,
      lta_tag: $lta_tag,
      normalized_lta_tag: $normalized_lta_tag,
      serial_number: $serial_number,
      normalized_serial_number: $normalized_serial_number,
      conflict_flag: false,
      created_at: datetime(),
      updated_at: datetime()
  })
  RETURN created AS unit, false AS conflict
}
RETURN unit.machine_unit_id AS machine_unit_id,
       conflict AS conflict
LIMIT 1
"""
```

- [ ] **Step 4: Run query tests**

Run:

```bash
uv run pytest services/ingestion/tests/test_machine_unit_queries.py -v
```

Expected: PASS.

---

### Task 3: Pass product parameters from sales and chat pipelines

**Files:**
- Modify: `services/ingestion/src/pipeline.py`
- Modify: `services/ingestion/src/pipeline_sales.py`

- [ ] **Step 1: Update imports in chat pipeline**

In `services/ingestion/src/pipeline.py`, replace:

```python
from src.machine_units import normalize_lta_tag, normalize_serial_number
```

with:

```python
from src.machine_units import (
    normalize_lta_tag,
    normalize_machine_product,
    normalize_serial_number,
)
```

- [ ] **Step 2: Update chat upsert parameters**

In `services/ingestion/src/pipeline.py`, inside `_write_chat_machine_unit_observations`, update the `tx.run(queries.UPSERT_MACHINE_UNIT, ...)` call to include product values:

```python
row = tx.run(
    queries.UPSERT_MACHINE_UNIT,
    machine_product=observation.machine_product,
    normalized_machine_product=normalize_machine_product(observation.machine_product),
    lta_tag=observation.lta_tag,
    normalized_lta_tag=normalize_lta_tag(observation.lta_tag),
    serial_number=observation.serial_number,
    normalized_serial_number=normalize_serial_number(observation.serial_number),
).single()
```

- [ ] **Step 3: Update imports in sales pipeline**

In `services/ingestion/src/pipeline_sales.py`, replace:

```python
from src.machine_units import normalize_lta_tag, normalize_serial_number
```

with:

```python
from src.machine_units import (
    normalize_lta_tag,
    normalize_machine_product,
    normalize_serial_number,
)
```

- [ ] **Step 4: Update sales upsert parameters**

In `services/ingestion/src/pipeline_sales.py`, inside `_write_machine_unit_observations`, update the `tx.run(queries.UPSERT_MACHINE_UNIT, ...)` call to include product values:

```python
row = tx.run(
    queries.UPSERT_MACHINE_UNIT,
    machine_product=observation.machine_product,
    normalized_machine_product=normalize_machine_product(observation.machine_product),
    lta_tag=observation.lta_tag,
    normalized_lta_tag=normalize_lta_tag(observation.lta_tag),
    serial_number=observation.serial_number,
    normalized_serial_number=normalize_serial_number(observation.serial_number),
).single()
```

- [ ] **Step 5: Run focused machine-unit tests**

Run:

```bash
uv run pytest services/ingestion/tests/test_machine_units.py services/ingestion/tests/test_machine_unit_extraction.py services/ingestion/tests/test_machine_unit_queries.py -v
```

Expected: PASS.

---

### Task 4: Run quality checks for ingestion changes

**Files:**
- Verify only; no expected code changes.

- [ ] **Step 1: Run formatter on changed ingestion files**

Run:

```bash
uv run --package profile-unifier-ingestion ruff format services/ingestion/src/machine_units.py services/ingestion/src/pipeline.py services/ingestion/src/pipeline_sales.py services/ingestion/src/graph/queries/machine_units.py services/ingestion/tests/test_machine_units.py services/ingestion/tests/test_machine_unit_queries.py
```

Expected: command exits 0. If it reformats files, keep the formatting changes.

- [ ] **Step 2: Run lint on changed ingestion files**

Run:

```bash
uv run --package profile-unifier-ingestion ruff check services/ingestion/src/machine_units.py services/ingestion/src/pipeline.py services/ingestion/src/pipeline_sales.py services/ingestion/src/graph/queries/machine_units.py services/ingestion/tests/test_machine_units.py services/ingestion/tests/test_machine_unit_queries.py
```

Expected: PASS.

- [ ] **Step 3: Run focused tests**

Run:

```bash
uv run pytest services/ingestion/tests/test_machine_units.py services/ingestion/tests/test_machine_unit_extraction.py services/ingestion/tests/test_machine_unit_queries.py -v
```

Expected: PASS.

- [ ] **Step 4: Run ingestion mypy strict**

Run:

```bash
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src
```

Expected: PASS, ignoring only documented pre-existing failures if they appear outside the changed files.

---

## Self-Review

- Spec coverage: product normalization is covered in Task 1; product-required validation is covered in Task 1; product-scoped Cypher matching and persisted properties are covered in Task 2; sales/chat upsert parameters are covered in Task 3; focused verification is covered in Task 4.
- Placeholder scan: no TBD/TODO/fill-in-later placeholders remain.
- Type consistency: `normalize_machine_product`, `machine_product`, and `normalized_machine_product` are named consistently across tests, pipelines, and query parameters.
