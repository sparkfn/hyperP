# SG Rental Flats Address Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `sgrentalflats` dispatchable and ingest rental-flat rows as source records linked to shared Address nodes, matching primarily on Singapore postal code and creating an Address when no match exists.

**Architecture:** Add a rental-flats connector that parses the existing SG Gov PostgreSQL dump tables (`flats`, `towns`) and emits one system source record per flat. Route `sgrentalflats` records through a dedicated address-ingestion path instead of the person pipeline, persisting the SourceRecord and upserting/linking an Address by `country_code + postal_code`. Reuse the same postal-code-first address upsert helper from the normal person-address write path so other address-bearing ingestions benefit from rental-flat Address nodes.

**Tech Stack:** Python 3.12, Pydantic v2, Neo4j Cypher, Celery, pytest, ruff, mypy strict, existing SG Gov dump parser.

---

## File Structure

- Create `services/ingestion/src/connectors/sggov/rental_flats.py`
  - Owns parsing `.sql` dump rows for `flats` and `towns`.
  - Exposes `SGGovernmentRentalFlatsConnector` as a `SourceConnector`.
  - Reads default dump from `Path(os.environ["DUMPS_ROOT"]) / "sgrentalflats_2026-05-11.sql"` when `DUMPS_ROOT` is set, otherwise `.dumps/sgrentalflats_2026-05-11.sql`.
  - Emits SourceRecord envelope dictionaries with address fields in `attributes` and joined `flat`/`town` rows in `raw_payload`.

- Modify `services/ingestion/src/connectors/sggov/__init__.py`
  - Export both SG Gov connectors.

- Create `services/ingestion/tests/test_sggov_rental_flats_connector.py`
  - Covers dump parsing, town join, envelope shape, `DUMPS_ROOT`, record hash, and connector registration.

- Modify `services/ingestion/src/graph/queries/persons.py`
  - Change `UPSERT_ADDRESS` from full-address identity to postal-code-first identity.
  - Add a `DESCRIBE_ADDRESS_FROM_SOURCE_RECORD` query.

- Modify `services/ingestion/src/graph/queries/__init__.py`
  - Export the new address-source query.

- Modify `services/ingestion/src/pipeline_writes.py`
  - Make address upsert postal-code-first and update missing/conservative display fields.
  - Add `link_source_record_to_address(...)` for address-only ingestion.

- Create `services/ingestion/src/pipeline_addresses.py`
  - Owns address-only ingestion for sources like `sgrentalflats`.
  - Performs idempotency, persists SourceRecord, upserts Address, links SourceRecord to Address, and links record to IngestRun.
  - Does not create Person, MatchDecision, ReviewCase, or GoldenProfile.

- Create `services/ingestion/tests/test_rental_flats_address_pipeline.py`
  - Uses a fake transaction/session to verify the address-only pipeline calls the right query parameters.

- Modify `services/ingestion/src/main.py`
  - Import/register `SGGovernmentRentalFlatsConnector` as `sgrentalflats`.
  - Route `sgrentalflats` through `ingest_address_record(...)` instead of `IngestPipeline.ingest(...)`.
  - Adjust logging so address-only results do not render misleading `person=None` output.

- Modify `infra/neo4j/init.cypher`
  - Add an idempotent uniqueness constraint for Address postal-code identity if not already present.
  - Add a relationship/index statement only if Neo4j supports it cleanly in the existing style; otherwise the query can work without schema changes.

- Modify `.docker/staging/docker-compose.yml` only if root `docker-compose.yml` changes further. Current root/staging dump-mount sync was already fixed in the previous production-test step.

---

### Task 1: Add Rental Flats Connector

**Files:**
- Create: `services/ingestion/src/connectors/sggov/rental_flats.py`
- Modify: `services/ingestion/src/connectors/sggov/__init__.py`
- Modify: `services/ingestion/src/main.py`
- Test: `services/ingestion/tests/test_sggov_rental_flats_connector.py`

- [ ] **Step 1: Write the failing connector test**

Create `services/ingestion/tests/test_sggov_rental_flats_connector.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from src.connectors.sggov.rental_flats import SGGovernmentRentalFlatsConnector
from src.main import get_connector


def _line(values: list[str]) -> str:
    return "\t".join(values) + "\n"


def _write_dump(path: Path) -> None:
    path.write_text(
        "COPY public.flats "
        "(id, town_id, block_no, street_name, postal_code, flat_type, first_seen_at, last_seen_at, is_active) FROM stdin;\n"
        + _line(
            [
                "33",
                "9",
                "165A",
                "Teck Whye Cres",
                "681165",
                "1-room & 2-room",
                "2026-05-08 08:24:42.13248+00",
                "2026-05-08 09:47:25.177976+00",
                "t",
            ]
        )
        + "\\.\n"
        + "COPY public.towns (id, name, map_id, map_zone) FROM stdin;\n"
        + _line(["9", "Choa Chu Kang Town", "choa_chu_kang", "CCK"])
        + "\\.\n",
        encoding="utf-8",
    )


def test_rental_flats_connector_yields_address_envelope(tmp_path: Path) -> None:
    dump = tmp_path / "rental.sql"
    _write_dump(dump)

    records = list(SGGovernmentRentalFlatsConnector(dump_path=dump).fetch_records())

    assert len(records) == 1
    record = records[0]
    assert record["source_record_id"] == "rental_flat:33"
    assert record["record_type"] == "system"
    assert record["observed_at"] == "2026-05-08T09:47:25.177976+00:00"
    assert record["identifiers"] == []
    assert record["attributes"] == {
        "country_code": "SG",
        "postal_code": "681165",
        "block_no": "165A",
        "street_name": "Teck Whye Cres",
        "flat_type": "1-room & 2-room",
        "town_id": "9",
        "town_name": "Choa Chu Kang Town",
        "town_map_id": "choa_chu_kang",
        "town_map_zone": "CCK",
        "is_active": True,
    }
    assert isinstance(record["raw_payload"], dict)
    raw_payload = record["raw_payload"]
    assert isinstance(raw_payload["flat"], dict)
    assert isinstance(raw_payload["town"], dict)
    assert raw_payload["flat"]["postal_code"] == "681165"
    assert raw_payload["town"]["name"] == "Choa Chu Kang Town"
    assert isinstance(record["record_hash"], str)
    assert record["record_hash"].startswith("sha256:")


def test_rental_flats_connector_uses_dumps_root_env_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dump = tmp_path / "sgrentalflats_2026-05-11.sql"
    _write_dump(dump)
    monkeypatch.setenv("DUMPS_ROOT", str(tmp_path))

    records = list(SGGovernmentRentalFlatsConnector().fetch_records())

    assert len(records) == 1
    assert records[0]["source_record_id"] == "rental_flat:33"


def test_rental_flats_connector_is_registered() -> None:
    assert isinstance(get_connector("sgrentalflats"), SGGovernmentRentalFlatsConnector)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest services/ingestion/tests/test_sggov_rental_flats_connector.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'src.connectors.sggov.rental_flats'` or registration failure.

- [ ] **Step 3: Implement the connector**

Create `services/ingestion/src/connectors/sggov/rental_flats.py`:

```python
"""Connector for SG Rental Flats dump records."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from src.connectors.base import SourceConnector
from src.connectors.fundbox.builders import build_envelope
from src.connectors.sggov.dump import CopyRow, parse_copy_tables
from src.models import JsonValue

_DUMP_FILENAME = "sgrentalflats_2026-05-11.sql"
_DEFAULT_DUMP_PATH = Path(".dumps") / _DUMP_FILENAME


def _default_dump_path() -> Path:
    dumps_root = os.environ.get("DUMPS_ROOT")
    if dumps_root:
        return Path(dumps_root) / _DUMP_FILENAME
    return _DEFAULT_DUMP_PATH


def _str_value(row: CopyRow, key: str) -> str:
    value = row.get(key)
    return value.strip() if isinstance(value, str) else ""


def _bool_value(row: CopyRow, key: str) -> bool:
    return _str_value(row, key).lower() in {"t", "true", "1", "yes"}


def _iso_datetime(value: JsonValue) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace(" ", "T", 1)
    if normalized.endswith("+00"):
        normalized = f"{normalized}:00"
    datetime.fromisoformat(normalized)
    return normalized


class SGGovernmentRentalFlatsConnector(SourceConnector):
    """Read rental-flat address inventory from an SG government SQL dump."""

    def __init__(self, dump_path: Path | None = None) -> None:
        self._dump_path = dump_path or _default_dump_path()

    def get_source_key(self) -> str:
        return "sgrentalflats"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        tables = parse_copy_tables(self._dump_path, {"flats", "towns"})
        towns = {_str_value(town, "id"): town for town in tables.get("towns", [])}

        for flat in tables.get("flats", []):
            flat_id = _str_value(flat, "id")
            town_id = _str_value(flat, "town_id")
            town = towns.get(town_id, {})
            observed_at = _iso_datetime(flat.get("last_seen_at")) or datetime.utcnow().isoformat()
            raw_payload: dict[str, JsonValue] = {"flat": flat, "town": town}
            yield build_envelope(
                source_record_id=f"rental_flat:{flat_id}",
                observed_at=observed_at,
                identifiers=[],
                attributes={
                    "country_code": "SG",
                    "postal_code": _str_value(flat, "postal_code"),
                    "block_no": _str_value(flat, "block_no"),
                    "street_name": _str_value(flat, "street_name"),
                    "flat_type": _str_value(flat, "flat_type"),
                    "town_id": town_id,
                    "town_name": _str_value(town, "name"),
                    "town_map_id": _str_value(town, "map_id"),
                    "town_map_zone": _str_value(town, "map_zone"),
                    "is_active": _bool_value(flat, "is_active"),
                },
                raw_payload=raw_payload,
            )
```

- [ ] **Step 4: Export and register connector**

Modify `services/ingestion/src/connectors/sggov/__init__.py` so it contains:

```python
"""SG government source connectors."""

from src.connectors.sggov.bankruptcy import SGGovernmentBankruptcyConnector
from src.connectors.sggov.rental_flats import SGGovernmentRentalFlatsConnector

__all__ = ["SGGovernmentBankruptcyConnector", "SGGovernmentRentalFlatsConnector"]
```

Modify `services/ingestion/src/main.py` import near the existing SG Gov import:

```python
from src.connectors.sggov import (
    SGGovernmentBankruptcyConnector,
    SGGovernmentRentalFlatsConnector,
)
```

Add the registry entry below `sgbankruptcy`:

```python
    "sgbankruptcy": SGGovernmentBankruptcyConnector,
    "sgrentalflats": SGGovernmentRentalFlatsConnector,
```

- [ ] **Step 5: Run test to verify it passes**

Run:

```bash
uv run pytest services/ingestion/tests/test_sggov_rental_flats_connector.py -q
```

Expected: `3 passed`.

- [ ] **Step 6: Commit**

Run:

```bash
git add services/ingestion/src/connectors/sggov/rental_flats.py services/ingestion/src/connectors/sggov/__init__.py services/ingestion/src/main.py services/ingestion/tests/test_sggov_rental_flats_connector.py
git commit -m "Add SG rental flats connector"
```

---

### Task 2: Add Postal-Code-First Address Upsert Queries

**Files:**
- Modify: `services/ingestion/src/graph/queries/persons.py`
- Modify: `services/ingestion/src/graph/queries/__init__.py`
- Test: `services/ingestion/tests/test_rental_flats_address_pipeline.py`

- [ ] **Step 1: Write failing query-shape test**

Create `services/ingestion/tests/test_rental_flats_address_pipeline.py` with only this first test:

```python
from __future__ import annotations

from src.graph import queries


def test_address_upsert_matches_by_country_and_postal_code_only() -> None:
    merge_header = "MERGE (addr:Address {\n    country_code: $country_code,\n    postal_code:  $postal_code\n})"

    assert merge_header in queries.UPSERT_ADDRESS
    assert "street_name:" not in queries.UPSERT_ADDRESS.split("ON CREATE SET", maxsplit=1)[0]
    assert "street_number:" not in queries.UPSERT_ADDRESS.split("ON CREATE SET", maxsplit=1)[0]
    assert "unit_number:" not in queries.UPSERT_ADDRESS.split("ON CREATE SET", maxsplit=1)[0]


def test_describe_address_query_is_exported() -> None:
    assert "DESCRIBES_ADDRESS" in queries.LINK_SOURCE_RECORD_TO_ADDRESS
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest services/ingestion/tests/test_rental_flats_address_pipeline.py -q
```

Expected: FAIL because `UPSERT_ADDRESS` currently merges on full address and `LINK_SOURCE_RECORD_TO_ADDRESS` does not exist.

- [ ] **Step 3: Modify address queries**

In `services/ingestion/src/graph/queries/persons.py`, replace `UPSERT_ADDRESS` with:

```python
UPSERT_ADDRESS = """
MERGE (addr:Address {
    country_code: $country_code,
    postal_code:  $postal_code
})
ON CREATE SET
    addr.address_id      = randomUUID(),
    addr.street_name     = $street_name,
    addr.street_number   = $street_number,
    addr.unit_number     = $unit_number,
    addr.building_name   = $building_name,
    addr.city            = $city,
    addr.state_province  = $state_province,
    addr.normalized_full = $normalized_full,
    addr.created_at      = datetime()
ON MATCH SET
    addr.street_name = CASE WHEN coalesce(addr.street_name, '') = '' THEN $street_name ELSE addr.street_name END,
    addr.street_number = CASE WHEN coalesce(addr.street_number, '') = '' THEN $street_number ELSE addr.street_number END,
    addr.unit_number = CASE WHEN coalesce(addr.unit_number, '') = '' THEN $unit_number ELSE addr.unit_number END,
    addr.building_name = CASE WHEN coalesce(addr.building_name, '') = '' THEN $building_name ELSE addr.building_name END,
    addr.city = CASE WHEN coalesce(addr.city, '') = '' THEN $city ELSE addr.city END,
    addr.state_province = CASE WHEN coalesce(addr.state_province, '') = '' THEN $state_province ELSE addr.state_province END,
    addr.normalized_full = CASE WHEN coalesce(addr.normalized_full, '') = '' THEN $normalized_full ELSE addr.normalized_full END
RETURN addr.address_id AS address_id
"""
```

Add below `LINK_PERSON_TO_ADDRESS`:

```python
LINK_SOURCE_RECORD_TO_ADDRESS = """
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
MATCH (addr:Address {country_code: $country_code, postal_code: $postal_code})
MERGE (sr)-[rel:DESCRIBES_ADDRESS]->(addr)
ON CREATE SET
    rel.linked_at = datetime(),
    rel.source_system_key = $source_system_key,
    rel.flat_type = $flat_type,
    rel.is_active = $is_active
ON MATCH SET
    rel.linked_at = datetime(),
    rel.flat_type = $flat_type,
    rel.is_active = $is_active
"""
```

- [ ] **Step 4: Export the new query**

Modify `services/ingestion/src/graph/queries/__init__.py`:

Add `LINK_SOURCE_RECORD_TO_ADDRESS` to the `from src.graph.queries.persons import (...)` import list.

Add this string to `__all__`:

```python
    "LINK_SOURCE_RECORD_TO_ADDRESS",
```

- [ ] **Step 5: Run test to verify it passes**

Run:

```bash
uv run pytest services/ingestion/tests/test_rental_flats_address_pipeline.py -q
```

Expected: `2 passed`.

- [ ] **Step 6: Commit**

Run:

```bash
git add services/ingestion/src/graph/queries/persons.py services/ingestion/src/graph/queries/__init__.py services/ingestion/tests/test_rental_flats_address_pipeline.py
git commit -m "Match addresses by postal code during ingestion"
```

---

### Task 3: Add Address-Only Pipeline

**Files:**
- Create: `services/ingestion/src/pipeline_addresses.py`
- Modify: `services/ingestion/src/pipeline_writes.py`
- Test: `services/ingestion/tests/test_rental_flats_address_pipeline.py`

- [ ] **Step 1: Add failing pipeline tests**

Append to `services/ingestion/tests/test_rental_flats_address_pipeline.py`:

```python
from typing import cast

from neo4j import ManagedTransaction

from src.models import IngestResult, SourceRecordEnvelope
from src.pipeline_addresses import ingest_address_record
from src.pipeline_writes import link_source_record_to_address


class _Tx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **kwargs: object) -> object:
        self.calls.append((query, kwargs))
        return object()


class _Session:
    def __init__(self) -> None:
        self.tx = _Tx()

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def execute_write(self, callback: object) -> object:
        return callback(cast(ManagedTransaction, self.tx))  # type: ignore[operator]


class _Client:
    def __init__(self) -> None:
        self.session_obj = _Session()

    def session(self) -> _Session:
        return self.session_obj


def _envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="sgrentalflats",
        source_record_id="rental_flat:33",
        observed_at="2026-05-08T09:47:25.177976+00:00",
        record_hash="sha256:abc",
        identifiers=[],
        attributes={
            "country_code": "SG",
            "postal_code": "681165",
            "block_no": "165A",
            "street_name": "Teck Whye Cres",
            "flat_type": "1-room & 2-room",
            "town_name": "Choa Chu Kang Town",
            "is_active": True,
        },
        raw_payload={"flat": {"id": "33"}, "town": {"id": "9"}},
    )


def test_link_source_record_to_address_uses_postal_code_parameters() -> None:
    tx = _Tx()

    link_source_record_to_address(
        cast(ManagedTransaction, tx),
        envelope=_envelope(),
        source_record_pk="sr-1",
    )

    assert len(tx.calls) == 2
    upsert_params = tx.calls[0][1]
    assert upsert_params["country_code"] == "SG"
    assert upsert_params["postal_code"] == "681165"
    assert upsert_params["street_number"] == "165A"
    assert upsert_params["street_name"] == "Teck Whye Cres"
    link_params = tx.calls[1][1]
    assert link_params["source_record_pk"] == "sr-1"
    assert link_params["source_system_key"] == "sgrentalflats"
    assert link_params["flat_type"] == "1-room & 2-room"
    assert link_params["is_active"] is True


def test_ingest_address_record_returns_address_result() -> None:
    client = _Client()

    result = ingest_address_record(
        cast(object, client),
        _envelope(),
        ingest_run_id="run-1",
    )

    assert isinstance(result, IngestResult)
    assert result.source_record_id == "rental_flat:33"
    assert result.ingest_run_id == "run-1"
    assert result.person_id is None
    assert result.match_decision is None
    assert result.candidate_count == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest services/ingestion/tests/test_rental_flats_address_pipeline.py -q
```

Expected: FAIL because `src.pipeline_addresses` and `link_source_record_to_address` do not exist.

- [ ] **Step 3: Add address linking helper**

In `services/ingestion/src/pipeline_writes.py`, add this helper after `_link_address(...)`:

```python
def _attribute_str(envelope: SourceRecordEnvelope, key: str) -> str:
    value = envelope.attributes.get(key)
    return value if isinstance(value, str) else ""


def _attribute_bool(envelope: SourceRecordEnvelope, key: str) -> bool:
    value = envelope.attributes.get(key)
    return value if isinstance(value, bool) else False


def link_source_record_to_address(
    tx: ManagedTransaction,
    *,
    envelope: SourceRecordEnvelope,
    source_record_pk: str,
) -> None:
    """Persist an address-only source record onto the shared Address graph."""
    postal_code = _attribute_str(envelope, "postal_code")
    country_code = _attribute_str(envelope, "country_code") or "SG"
    block_no = _attribute_str(envelope, "block_no")
    street_name = _attribute_str(envelope, "street_name")
    town_name = _attribute_str(envelope, "town_name")
    normalized_full = " ".join(
        part for part in [block_no, street_name, postal_code, country_code] if part
    )

    tx.run(
        queries.UPSERT_ADDRESS,
        country_code=country_code,
        postal_code=postal_code,
        street_name=street_name,
        street_number=block_no,
        unit_number="",
        building_name=None,
        city=town_name,
        state_province=None,
        normalized_full=normalized_full,
    )
    tx.run(
        queries.LINK_SOURCE_RECORD_TO_ADDRESS,
        source_record_pk=source_record_pk,
        country_code=country_code,
        postal_code=postal_code,
        source_system_key=envelope.source_system,
        flat_type=_attribute_str(envelope, "flat_type"),
        is_active=_attribute_bool(envelope, "is_active"),
    )
```

- [ ] **Step 4: Add address-only pipeline**

Create `services/ingestion/src/pipeline_addresses.py`:

```python
"""Address-only ingestion for source records that do not represent people."""

from __future__ import annotations

import logging

from src.graph.client import Neo4jClient
from src.models import IngestResult, SourceRecordEnvelope
from src.pipeline import IngestPipeline
from src.pipeline_normalization import normalize_envelope_attributes
from src.pipeline_writes import (
    link_source_record_to_address,
    link_source_record_to_run,
    persist_source_record,
)

logger = logging.getLogger(__name__)


def ingest_address_record(
    client: Neo4jClient,
    envelope: SourceRecordEnvelope,
    ingest_run_id: str | None = None,
) -> IngestResult:
    """Persist a source record and attach it to a shared Address node."""
    existing_pk = IngestPipeline(client)._check_idempotency(envelope)
    if existing_pk is not None:
        logger.info(
            "Duplicate address source record %s (hash=%s) — skipping",
            envelope.source_record_id,
            envelope.record_hash,
        )
        return IngestResult(
            source_record_id=envelope.source_record_id,
            source_record_pk=existing_pk,
            skipped_duplicate=True,
            ingest_run_id=ingest_run_id,
        )

    attributes = normalize_envelope_attributes(envelope)

    def _tx(tx: object) -> IngestResult:
        source_record_pk = persist_source_record(tx, envelope, attributes)
        if ingest_run_id is not None:
            link_source_record_to_run(tx, source_record_pk, ingest_run_id)
        link_source_record_to_address(
            tx,
            envelope=envelope,
            source_record_pk=source_record_pk,
        )
        logger.info(
            "Ingested %s -> address postal_code=%s",
            envelope.source_record_id,
            envelope.attributes.get("postal_code"),
        )
        return IngestResult(
            source_record_id=envelope.source_record_id,
            source_record_pk=source_record_pk,
            ingest_run_id=ingest_run_id,
        )

    with client.session() as session:
        return session.execute_write(_tx)
```

Important: If mypy rejects `tx: object` because `persist_source_record` expects `ManagedTransaction`, replace the import block with `from neo4j import ManagedTransaction` and type `_tx(tx: ManagedTransaction) -> IngestResult`.

- [ ] **Step 5: Run test to verify it passes or exposes fake result mismatch**

Run:

```bash
uv run pytest services/ingestion/tests/test_rental_flats_address_pipeline.py -q
```

Expected: Tests may fail because `_Tx.run(...)` does not fake `persist_source_record` return shape. If so, update only the fake `_Tx.run` in the test to return an object with `.single()` for `queries.CREATE_SOURCE_RECORD`. Use this exact helper:

```python
class _Result:
    def __init__(self, row: dict[str, object] | None = None) -> None:
        self._row = row

    def single(self) -> dict[str, object] | None:
        return self._row
```

Then modify `_Tx.run(...)`:

```python
    def run(self, query: str, **kwargs: object) -> object:
        self.calls.append((query, kwargs))
        if "RETURN sr.source_record_pk AS source_record_pk" in query:
            return _Result({"source_record_pk": "sr-1"})
        return _Result()
```

Rerun until expected output is: all tests in this file pass.

- [ ] **Step 6: Commit**

Run:

```bash
git add services/ingestion/src/pipeline_addresses.py services/ingestion/src/pipeline_writes.py services/ingestion/tests/test_rental_flats_address_pipeline.py
git commit -m "Add address-only ingestion pipeline"
```

---

### Task 4: Route Rental Flats Through Address Pipeline

**Files:**
- Modify: `services/ingestion/src/main.py`
- Test: `services/ingestion/tests/test_rental_flats_address_pipeline.py`

- [ ] **Step 1: Add failing routing test**

Append to `services/ingestion/tests/test_rental_flats_address_pipeline.py`:

```python
from src.main import _is_address_only_source


def test_rental_flats_is_address_only_source() -> None:
    assert _is_address_only_source("sgrentalflats") is True
    assert _is_address_only_source("sgbankruptcy") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest services/ingestion/tests/test_rental_flats_address_pipeline.py::test_rental_flats_is_address_only_source -q
```

Expected: FAIL because `_is_address_only_source` does not exist.

- [ ] **Step 3: Implement routing**

Modify `services/ingestion/src/main.py`:

Add import near pipeline imports:

```python
from src.pipeline_addresses import ingest_address_record
```

Add helper near the connector registry:

```python
_ADDRESS_ONLY_SOURCES = frozenset({"sgrentalflats"})


def _is_address_only_source(source_key: str) -> bool:
    return source_key in _ADDRESS_ONLY_SOURCES
```

Replace `_process_record(...)` with:

```python
def _process_record(
    client: Neo4jClient,
    pipeline: IngestPipeline,
    envelope: SourceRecordEnvelope,
    ingest_run_id: str,
) -> IngestResult:
    """Route a single envelope to the correct ingestion pipeline."""
    if envelope.record_type == RecordType.SALES:
        return ingest_sales_record(client, envelope, ingest_run_id=ingest_run_id)
    if _is_address_only_source(envelope.source_system):
        return ingest_address_record(client, envelope, ingest_run_id=ingest_run_id)
    return pipeline.ingest(envelope, ingest_run_id=ingest_run_id)
```

Replace the `logger.info(...)` block inside `_ingest_all_records(...)` with:

```python
        if result.person_id is None:
            logger.info(
                "  %s -> address-only%s",
                result.source_record_id,
                " (DUPLICATE)" if result.skipped_duplicate else "",
            )
        else:
            logger.info(
                "  %s -> person=%s new=%s decision=%s candidates=%d%s",
                result.source_record_id,
                result.person_id,
                result.is_new_person,
                result.match_decision,
                result.candidate_count,
                " (DUPLICATE)" if result.skipped_duplicate else "",
            )
```

- [ ] **Step 4: Run routing test**

Run:

```bash
uv run pytest services/ingestion/tests/test_rental_flats_address_pipeline.py::test_rental_flats_is_address_only_source -q
```

Expected: PASS.

- [ ] **Step 5: Run all rental-flats tests**

Run:

```bash
uv run pytest services/ingestion/tests/test_sggov_rental_flats_connector.py services/ingestion/tests/test_rental_flats_address_pipeline.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

Run:

```bash
git add services/ingestion/src/main.py services/ingestion/tests/test_rental_flats_address_pipeline.py
git commit -m "Route rental flats to address ingestion"
```

---

### Task 5: Schema and End-to-End Verification

**Files:**
- Modify: `infra/neo4j/init.cypher`
- Test manually through Docker Compose and Celery.

- [ ] **Step 1: Add schema support if missing**

Open `infra/neo4j/init.cypher`. If there is no Address postal-code uniqueness constraint, add:

```cypher
CREATE CONSTRAINT address_country_postal_unique IF NOT EXISTS
FOR (a:Address)
REQUIRE (a.country_code, a.postal_code) IS UNIQUE;
```

Do not remove existing full-address constraints in this task unless Neo4j rejects conflicting constraints during verification. If a conflict appears, stop and inspect the actual error before changing constraints.

- [ ] **Step 2: Run focused tests**

Run:

```bash
uv run pytest services/ingestion/tests/test_sggov_dump.py services/ingestion/tests/test_sggov_bankruptcy_connector.py services/ingestion/tests/test_sggov_rental_flats_connector.py services/ingestion/tests/test_rental_flats_address_pipeline.py -q
```

Expected: all selected tests pass.

- [ ] **Step 3: Run lint and type checks**

Run:

```bash
uv run --package profile-unifier-ingestion ruff check services/ingestion/src services/ingestion/tests/test_sggov_rental_flats_connector.py services/ingestion/tests/test_rental_flats_address_pipeline.py
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src
```

Expected: ruff exits 0; mypy exits 0. Existing `pyproject.toml: note: unused section(s)` output is acceptable.

- [ ] **Step 4: Rebuild and start containers**

Run:

```bash
docker compose build --no-cache api frontend worker beat
docker compose up -d
```

Expected: all services start healthy. If Neo4j port conflicts with another Compose project, stop only the conflicting project after user approval or retry if the user says to retry.

- [ ] **Step 5: Verify worker sees both mounted dumps**

Run:

```bash
MSYS_NO_PATHCONV=1 docker compose exec -T worker /app/.venv/bin/python - <<'PY'
from pathlib import Path
for name in ['sgbankruptcy_2026-05-11.sql', 'sgrentalflats_2026-05-11.sql']:
    path = Path('/app/dumps') / name
    print(name, path.exists(), path.stat().st_size if path.exists() else 0)
PY
```

Expected: both files print `True` and non-zero sizes.

- [ ] **Step 6: Dispatch rental-flats ingestion**

Run:

```bash
MSYS_NO_PATHCONV=1 docker compose exec -T worker /app/.venv/bin/python - <<'PY'
from src.tasks import run_ingestion_task
result = run_ingestion_task.delay('sgrentalflats', mode='batch')
print(result.id)
PY
```

Save the printed Celery task ID.

- [ ] **Step 7: Monitor for terminal result**

Replace `<TASK_ID>` and run:

```bash
MSYS_NO_PATHCONV=1 docker compose exec -T worker /app/.venv/bin/python - <<'PY'
from celery.result import AsyncResult
from src.celery_app import celery_app
rid = '<TASK_ID>'
res = AsyncResult(rid, app=celery_app)
print('state', res.state)
print('ready', res.ready())
print('result', res.result if res.ready() else None)
PY

docker compose logs --tail=200 worker | grep -E 'ERROR|Traceback|Ingestion complete|Task src.tasks.run_ingestion_task|failed' || true
```

Expected: final result contains `source_key: 'sgrentalflats'`, `status: 'completed'`, `errors: 0`, and `succeeded: 280` for the current dump.

- [ ] **Step 8: Verify graph shape**

Run:

```bash
MSYS_NO_PATHCONV=1 docker compose exec -T neo4j /var/lib/neo4j/bin/cypher-shell -u neo4j -p hyperP_dev_2026 "
MATCH (sr:SourceRecord)-[:FROM_SOURCE]->(:SourceSystem {source_key: 'sgrentalflats'})
OPTIONAL MATCH (sr)-[:DESCRIBES_ADDRESS]->(addr:Address)
RETURN count(sr) AS source_records, count(addr) AS linked_addresses, count(DISTINCT addr.postal_code) AS distinct_postal_codes
"
```

Expected: `source_records = 280`, `linked_addresses = 280`, `distinct_postal_codes = 280` for the current dump.

- [ ] **Step 9: Stop containers**

Run:

```bash
docker compose stop
docker compose ps
```

Expected: no running services in `docker compose ps`.

- [ ] **Step 10: Commit**

Run:

```bash
git add infra/neo4j/init.cypher
git commit -m "Verify SG rental flats address ingestion"
```

If `infra/neo4j/init.cypher` did not change, skip this commit.

---

## Final Verification

Run all commands from the repo root:

```bash
uv run pytest services/ingestion/tests/test_sggov_dump.py services/ingestion/tests/test_sggov_bankruptcy_connector.py services/ingestion/tests/test_sggov_rental_flats_connector.py services/ingestion/tests/test_rental_flats_address_pipeline.py -q
uv run --package profile-unifier-ingestion ruff check services/ingestion/src services/ingestion/tests/test_sggov_rental_flats_connector.py services/ingestion/tests/test_rental_flats_address_pipeline.py
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src
docker compose ps
```

Expected:

- selected pytest suite passes;
- ruff exits 0;
- mypy exits 0;
- `docker compose ps` shows no running services after cleanup.

## Self-Review

- Spec coverage: The plan makes `sgrentalflats` dispatchable, creates SourceRecords, matches/creates Address by postal code, avoids fake Person nodes, and reuses the address upsert helper for other address-bearing ingestion.
- Placeholder scan: No TBD/TODO placeholders remain. Each code-changing task includes concrete file paths, code snippets, commands, and expected outputs.
- Type consistency: `SGGovernmentRentalFlatsConnector`, `ingest_address_record`, `link_source_record_to_address`, `_is_address_only_source`, and `LINK_SOURCE_RECORD_TO_ADDRESS` are named consistently across tasks.
