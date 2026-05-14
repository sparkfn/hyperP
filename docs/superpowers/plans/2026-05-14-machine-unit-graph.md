# Machine Unit Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add first-class MachineUnit graph support with normalization, Cypher writes, targeted indexes, and review-only matching hooks.

**Architecture:** Keep MachineUnit normalization and observation typing in a focused ingestion module. Keep Cypher in a dedicated graph query module and re-export through the existing `src.graph.queries` facade. Schema changes are limited to Neo4j constraints/indexes and do not change name modeling.

**Tech Stack:** Python 3.12, Pydantic, Neo4j Cypher, uv, pytest, ruff, mypy strict.

---

## File structure

- Create `services/ingestion/src/machine_units.py` — typed `MachineUnitObservation`, normalization helpers, and validation helpers.
- Create `services/ingestion/src/graph/queries/machine_units.py` — MachineUnit upsert/link/conflict Cypher constants.
- Modify `services/ingestion/src/graph/queries/__init__.py` — re-export MachineUnit queries.
- Modify `infra/neo4j/init.cypher` — add MachineUnit constraint and lookup indexes.
- Create `services/ingestion/tests/test_machine_units.py` — unit tests for normalization and observation validation.
- Create `services/ingestion/tests/test_machine_unit_queries.py` — query export and shape tests.

Do not model names as identifiers in this plan.

### Task 1: MachineUnit normalization helpers

**Files:**
- Create: `services/ingestion/src/machine_units.py`
- Test: `services/ingestion/tests/test_machine_units.py`

- [ ] **Step 1: Write failing normalization tests**

```python
from __future__ import annotations

from src.machine_units import (
    MachineUnitObservation,
    normalize_lta_tag,
    normalize_serial_number,
    valid_machine_unit_observation,
)
from src.models import QualityFlag


def test_normalize_lta_tag_uppercases_and_removes_separators() -> None:
    assert normalize_lta_tag(" lta-123 45 ") == "LTA12345"


def test_normalize_serial_number_preserves_meaningful_punctuation() -> None:
    assert normalize_serial_number(" sn-09/a ") == "SN-09/A"


def test_placeholder_unit_values_are_rejected() -> None:
    obs = MachineUnitObservation(
        lta_tag="n/a",
        serial_number=None,
        machine_product=None,
        unit_label=None,
        source_kind="sales",
        source_system_key="fundbox_consumer_backend",
        source_record_id="order-1",
        observed_at="2026-05-14T00:00:00",
        confidence=1.0,
        quality_flag=QualityFlag.VALID,
        raw_context="line-1",
    )

    assert valid_machine_unit_observation(obs) is False


def test_observation_is_valid_when_one_identifier_normalizes() -> None:
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

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_machine_units.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'src.machine_units'`.

- [ ] **Step 3: Implement normalization module**

```python
"""Machine unit observation types and normalization helpers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from src.models import QualityFlag

MachineUnitSourceKind = Literal["sales", "chat_inquiry", "explicit_ownership_claim"]

_PLACEHOLDERS: frozenset[str] = frozenset(
    {"", "-", "--", "N/A", "NA", "NONE", "NULL", "UNKNOWN", "NIL"}
)


class MachineUnitObservation(BaseModel):
    lta_tag: str | None = None
    serial_number: str | None = None
    machine_product: str | None = None
    unit_label: str | None = None
    source_kind: MachineUnitSourceKind
    source_system_key: str
    source_record_id: str
    observed_at: str | None = None
    confidence: float | None = None
    quality_flag: QualityFlag = QualityFlag.VALID
    raw_context: str | None = None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().upper()
    if cleaned in _PLACEHOLDERS:
        return None
    return cleaned


def normalize_lta_tag(value: str | None) -> str | None:
    cleaned = _clean(value)
    if cleaned is None:
        return None
    normalized = "".join(ch for ch in cleaned if ch.isalnum())
    return normalized if normalized and normalized not in _PLACEHOLDERS else None


def normalize_serial_number(value: str | None) -> str | None:
    cleaned = _clean(value)
    if cleaned is None:
        return None
    normalized = " ".join(cleaned.split())
    return normalized if normalized and normalized not in _PLACEHOLDERS else None


def valid_machine_unit_observation(observation: MachineUnitObservation) -> bool:
    return (
        normalize_lta_tag(observation.lta_tag) is not None
        or normalize_serial_number(observation.serial_number) is not None
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest services/ingestion/tests/test_machine_units.py -v`

Expected: PASS.

### Task 2: MachineUnit graph queries

**Files:**
- Create: `services/ingestion/src/graph/queries/machine_units.py`
- Modify: `services/ingestion/src/graph/queries/__init__.py`
- Test: `services/ingestion/tests/test_machine_unit_queries.py`

- [ ] **Step 1: Write failing query export tests**

```python
from __future__ import annotations

from src.graph import queries


def test_machine_unit_queries_are_exported() -> None:
    assert "MachineUnit" in queries.UPSERT_MACHINE_UNIT
    assert "INVOLVES_UNIT" in queries.LINK_ORDER_INVOLVES_UNIT
    assert "BOUGHT_UNIT" in queries.LINK_PERSON_BOUGHT_UNIT
    assert "OWNS_UNIT" in queries.LINK_PERSON_OWNS_UNIT
    assert "conflict_flag" in queries.FLAG_MACHINE_UNIT_OWNER_CONFLICTS


def test_machine_unit_upsert_handles_lta_or_serial_matches() -> None:
    query = queries.UPSERT_MACHINE_UNIT

    assert "normalized_lta_tag" in query
    assert "normalized_serial_number" in query
    assert "machine_unit_identifier_conflict" in query
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_machine_unit_queries.py -v`

Expected: FAIL with `AttributeError` for missing query exports.

- [ ] **Step 3: Add MachineUnit query constants**

Create `services/ingestion/src/graph/queries/machine_units.py`:

```python
"""Cypher constants for MachineUnit nodes and relationships."""

from __future__ import annotations

UPSERT_MACHINE_UNIT = """
OPTIONAL MATCH (lta_unit:MachineUnit {normalized_lta_tag: $normalized_lta_tag})
  WHERE $normalized_lta_tag IS NOT NULL
OPTIONAL MATCH (serial_unit:MachineUnit {normalized_serial_number: $normalized_serial_number})
  WHERE $normalized_serial_number IS NOT NULL
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
  SET matched.lta_tag = coalesce(matched.lta_tag, $lta_tag),
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

LINK_ORDER_INVOLVES_UNIT = """
MATCH (o:Order {source_system_key: $source_system_key, source_order_id: $source_order_id})
MATCH (u:MachineUnit {machine_unit_id: $machine_unit_id})
MERGE (o)-[rel:INVOLVES_UNIT {
    source_system_key: $source_system_key,
    source_record_pk:  $source_record_pk,
    raw_context:       $raw_context
}]->(u)
ON CREATE SET rel.created_at = datetime()
SET rel.observed_at = $observed_at,
    rel.confidence = $confidence,
    rel.quality_flag = $quality_flag,
    rel.updated_at = datetime()
"""

LINK_PERSON_BOUGHT_UNIT = """
MATCH (p:Person {person_id: $person_id})
MATCH (u:MachineUnit {machine_unit_id: $machine_unit_id})
MERGE (p)-[rel:BOUGHT_UNIT {
    source_system_key: $source_system_key,
    source_record_pk:  $source_record_pk,
    source_order_id:   $source_order_id,
    raw_context:       $raw_context
}]->(u)
ON CREATE SET rel.created_at = datetime(),
              rel.first_seen_at = datetime()
SET rel.observed_at = $observed_at,
    rel.confidence = $confidence,
    rel.quality_flag = $quality_flag,
    rel.last_seen_at = datetime(),
    rel.last_confirmed_at = datetime(),
    rel.updated_at = datetime()
"""

LINK_PERSON_OWNS_UNIT = """
MATCH (p:Person {person_id: $person_id})
MATCH (u:MachineUnit {machine_unit_id: $machine_unit_id})
MERGE (p)-[rel:OWNS_UNIT {
    source_system_key: $source_system_key,
    source_record_pk:  $source_record_pk,
    raw_context:       $raw_context
}]->(u)
ON CREATE SET rel.created_at = datetime(),
              rel.first_seen_at = datetime(),
              rel.is_active = true
SET rel.observed_at = $observed_at,
    rel.confidence = $confidence,
    rel.quality_flag = $quality_flag,
    rel.last_seen_at = datetime(),
    rel.last_confirmed_at = datetime(),
    rel.updated_at = datetime()
"""

FLAG_MACHINE_UNIT_OWNER_CONFLICTS = """
MATCH (u:MachineUnit)<-[rel:OWNS_UNIT {is_active: true}]-(p:Person {status: 'active'})
WITH u, collect(DISTINCT p.person_id) AS owner_ids, collect(rel) AS rels
WHERE size(owner_ids) > 1
SET u.conflict_flag = true,
    u.conflict_reason = 'multiple_active_owners',
    u.updated_at = datetime()
FOREACH (rel IN rels | SET rel.conflict_flag = true, rel.updated_at = datetime())
RETURN u.machine_unit_id AS machine_unit_id, owner_ids AS owner_ids
"""
```

- [ ] **Step 4: Re-export query constants**

Modify `services/ingestion/src/graph/queries/__init__.py` to import and export:

```python
from src.graph.queries.machine_units import (
    FLAG_MACHINE_UNIT_OWNER_CONFLICTS,
    LINK_ORDER_INVOLVES_UNIT,
    LINK_PERSON_BOUGHT_UNIT,
    LINK_PERSON_OWNS_UNIT,
    UPSERT_MACHINE_UNIT,
)
```

Add these names to `__all__`:

```python
"FLAG_MACHINE_UNIT_OWNER_CONFLICTS",
"LINK_ORDER_INVOLVES_UNIT",
"LINK_PERSON_BOUGHT_UNIT",
"LINK_PERSON_OWNS_UNIT",
"UPSERT_MACHINE_UNIT",
```

- [ ] **Step 5: Run query tests**

Run: `uv run pytest services/ingestion/tests/test_machine_unit_queries.py -v`

Expected: PASS.

### Task 3: Neo4j schema indexes

**Files:**
- Modify: `infra/neo4j/init.cypher`

- [ ] **Step 1: Add MachineUnit schema statements**

Append this block after existing uniqueness constraints and lookup indexes in `infra/neo4j/init.cypher`:

```cypher
// Machine unit lookups
CREATE CONSTRAINT machine_unit_id_unique IF NOT EXISTS
  FOR (mu:MachineUnit) REQUIRE mu.machine_unit_id IS UNIQUE;

CREATE INDEX idx_machine_unit_lta_tag IF NOT EXISTS
  FOR (mu:MachineUnit) ON (mu.normalized_lta_tag);

CREATE INDEX idx_machine_unit_serial_number IF NOT EXISTS
  FOR (mu:MachineUnit) ON (mu.normalized_serial_number);
```

- [ ] **Step 2: Verify schema file contains expected statements**

Run: `uv run python -c "from pathlib import Path; text=Path('infra/neo4j/init.cypher').read_text(); assert 'machine_unit_id_unique' in text; assert 'idx_machine_unit_lta_tag' in text; assert 'idx_machine_unit_serial_number' in text"`

Expected: PASS with no output.

### Task 4: MachineUnit graph plan verification

**Files:**
- None

- [ ] **Step 1: Run MachineUnit tests**

Run: `uv run pytest services/ingestion/tests/test_machine_units.py services/ingestion/tests/test_machine_unit_queries.py -v`

Expected: PASS.

- [ ] **Step 2: Run ingestion lint**

Run: `uv run --package profile-unifier-ingestion ruff check services/ingestion/src services/ingestion/tests`

Expected: PASS.

- [ ] **Step 3: Run ingestion type check**

Run: `uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src`

Expected: PASS or only documented pre-existing failures outside changed files.

## Self-review

- Spec coverage: normalization, MachineUnit node queries, order/person/ownership relationships, conflict flagging, and Neo4j indexes are covered.
- Placeholder scan: no placeholder tasks remain.
- Type consistency: `MachineUnitObservation`, query names, and relationship names match the approved spec.
