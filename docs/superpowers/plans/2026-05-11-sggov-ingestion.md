# SG Gov Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add manual-dispatch ingestion for the SG Bankruptcy dump with first-class `BankruptcyCase` graph nodes, while registering SG Gov and SG Rental Flats source metadata without scheduling or person-ingesting rental flats.

**Architecture:** The ingestion service will parse PostgreSQL dump `COPY` sections directly into existing `SourceRecordEnvelope` dictionaries. Normal person resolution remains in `IngestPipeline`; after `sgbankruptcy` records are linked to a `Person`, a small graph helper materializes `BankruptcyCase` and relationships from the envelope raw payload.

**Tech Stack:** Python 3.13, Pydantic v2, Neo4j Cypher, Celery ingestion entrypoint, uv, pytest, ruff, mypy strict.

---

## File Structure

- Modify `infra/neo4j/init.cypher`: add `BankruptcyCase` uniqueness/index statements.
- Modify `services/ingestion/src/graph/bootstrap.py`: add `sggov` entity plus `sgbankruptcy` and `sgrentalflats` source systems.
- Create `services/ingestion/src/connectors/sggov/__init__.py`: export the bankruptcy connector.
- Create `services/ingestion/src/connectors/sggov/dump.py`: focused PostgreSQL dump `COPY` parser used by SG Gov connectors.
- Create `services/ingestion/src/connectors/sggov/bankruptcy.py`: parse bankruptcy tables and emit one source-record envelope per case.
- Create `services/ingestion/src/graph/queries/bankruptcy.py`: Cypher for materializing `BankruptcyCase` nodes and relationships.
- Modify `services/ingestion/src/graph/queries/__init__.py`: re-export bankruptcy query constants.
- Create `services/ingestion/src/pipeline_bankruptcy.py`: helper that detects `sgbankruptcy` envelopes and writes bankruptcy graph nodes.
- Modify `services/ingestion/src/pipeline.py`: call bankruptcy helper after `link_record_to_graph`.
- Modify `services/ingestion/src/main.py`: register `sgbankruptcy` connector only; do not register `sgrentalflats`.
- Create `services/ingestion/tests/test_sggov_dump.py`: parser tests.
- Create `services/ingestion/tests/test_sggov_bankruptcy_connector.py`: connector envelope tests.
- Create `services/ingestion/tests/test_bankruptcy_graph.py`: graph helper query/parameter tests.

---

### Task 1: Add SG Gov source metadata and Neo4j schema

**Files:**
- Modify: `infra/neo4j/init.cypher`
- Modify: `services/ingestion/src/graph/bootstrap.py`

- [ ] **Step 1: Add failing metadata expectation mentally before code**

Expected after this task:

```python
SOURCE_KEY_TO_ENTITY["sgbankruptcy"] == "sggov"
SOURCE_KEY_TO_ENTITY["sgrentalflats"] == "sggov"
```

- [ ] **Step 2: Modify `infra/neo4j/init.cypher`**

Add after the existing product constraints:

```cypher
CREATE CONSTRAINT bankruptcy_case_dedup_unique IF NOT EXISTS
  FOR (bc:BankruptcyCase) REQUIRE (bc.source_system_key, bc.source_case_id) IS UNIQUE;
```

Add near the other domain lookup indexes:

```cypher
// Bankruptcy lookups
CREATE INDEX idx_bankruptcy_case_number IF NOT EXISTS
  FOR (bc:BankruptcyCase) ON (bc.case_number);

CREATE INDEX idx_bankruptcy_event_date IF NOT EXISTS
  FOR (bc:BankruptcyCase) ON (bc.event_date);
```

- [ ] **Step 3: Modify `services/ingestion/src/graph/bootstrap.py`**

Add the entity inside `_ENTITIES`:

```python
    {
        "entity_key": "sggov",
        "display_name": "SG Gov",
        "entity_type": "government",
        "country_code": "SG",
    },
```

Add trust map after `_CHAT_TRUST`:

```python
_GOVERNMENT_REGISTRY_TRUST: dict[str, str] = {
    "full_name": "tier_4",
    "nric": "tier_4",
    "address": "tier_4",
}
```

Add source systems inside `_SOURCE_SYSTEMS`:

```python
    {
        "source_key": "sgbankruptcy",
        "display_name": "SG Bankruptcy Register",
        "system_type": "government_registry",
        "entity_key": "sggov",
        "field_trust": _GOVERNMENT_REGISTRY_TRUST,
    },
    {
        "source_key": "sgrentalflats",
        "display_name": "SG Rental Flats",
        "system_type": "government_registry",
        "entity_key": "sggov",
        "field_trust": _GOVERNMENT_REGISTRY_TRUST,
    },
```

- [ ] **Step 4: Run focused import check**

Run: `uv run --package profile-unifier-ingestion python - <<'PY'\nfrom src.graph.bootstrap import SOURCE_KEY_TO_ENTITY\nassert SOURCE_KEY_TO_ENTITY['sgbankruptcy'] == 'sggov'\nassert SOURCE_KEY_TO_ENTITY['sgrentalflats'] == 'sggov'\nprint('ok')\nPY`

Expected: prints `ok`.

---

### Task 2: Add PostgreSQL dump COPY parser

**Files:**
- Create: `services/ingestion/src/connectors/sggov/__init__.py`
- Create: `services/ingestion/src/connectors/sggov/dump.py`
- Test: `services/ingestion/tests/test_sggov_dump.py`

- [ ] **Step 1: Write failing parser tests**

Create `services/ingestion/tests/test_sggov_dump.py`:

```python
from __future__ import annotations

from pathlib import Path

from src.connectors.sggov.dump import parse_copy_tables


def test_parse_copy_tables_extracts_requested_tables(tmp_path: Path) -> None:
    dump = tmp_path / "sample.sql"
    dump.write_text(
        "COPY public.people (id, name, missing, note) FROM stdin;\n"
        "1\tAlice\t\\N\thello\\nworld\n"
        "2\tBob\tvalue\tplain\n"
        "\\.\n"
        "COPY public.ignored (id) FROM stdin;\n"
        "9\n"
        "\\.\n",
        encoding="utf-8",
    )

    tables = parse_copy_tables(dump, {"people"})

    assert list(tables) == ["people"]
    assert tables["people"] == [
        {"id": "1", "name": "Alice", "missing": None, "note": "hello\nworld"},
        {"id": "2", "name": "Bob", "missing": "value", "note": "plain"},
    ]
```

- [ ] **Step 2: Run test and verify it fails**

Run: `uv run pytest services/ingestion/tests/test_sggov_dump.py -q`

Expected: import failure because `src.connectors.sggov.dump` does not exist.

- [ ] **Step 3: Create parser package and implementation**

Create `services/ingestion/src/connectors/sggov/__init__.py`:

```python
"""SG Gov dump connectors."""

from src.connectors.sggov.bankruptcy import SGGovernmentBankruptcyConnector

__all__ = ["SGGovernmentBankruptcyConnector"]
```

Create `services/ingestion/src/connectors/sggov/dump.py`:

```python
"""Minimal PostgreSQL dump COPY parser for SG Gov source dumps."""

from __future__ import annotations

from pathlib import Path

from src.models import JsonValue

CopyRow = dict[str, JsonValue]
CopyTables = dict[str, list[CopyRow]]


def _decode_copy_value(value: str) -> str | None:
    if value == r"\N":
        return None
    return (
        value.replace(r"\n", "\n")
        .replace(r"\t", "\t")
        .replace(r"\r", "\r")
        .replace(r"\\", "\\")
    )


def _parse_copy_header(line: str) -> tuple[str, list[str]] | None:
    prefix = "COPY public."
    suffix = ") FROM stdin;"
    if not line.startswith(prefix) or not line.endswith(suffix):
        return None
    table_part, columns_part = line[len(prefix) : -len(suffix)].split(" (", 1)
    return table_part, [column.strip() for column in columns_part.split(",")]


def parse_copy_tables(path: Path, table_names: set[str]) -> CopyTables:
    tables: CopyTables = {}
    current_table: str | None = None
    current_columns: list[str] = []

    for line in path.read_text(encoding="utf-8").splitlines():
        header = _parse_copy_header(line)
        if header is not None:
            table, columns = header
            if table in table_names:
                current_table = table
                current_columns = columns
                tables[table] = []
            else:
                current_table = None
                current_columns = []
            continue

        if line == r"\.":
            current_table = None
            current_columns = []
            continue

        if current_table is None:
            continue

        values = line.split("\t")
        row: CopyRow = {
            column: _decode_copy_value(values[index]) if index < len(values) else None
            for index, column in enumerate(current_columns)
        }
        tables[current_table].append(row)

    return tables
```

- [ ] **Step 4: Run parser test**

Run: `uv run pytest services/ingestion/tests/test_sggov_dump.py -q`

Expected: PASS.

---

### Task 3: Add SG Bankruptcy connector

**Files:**
- Create: `services/ingestion/src/connectors/sggov/bankruptcy.py`
- Modify: `services/ingestion/src/main.py`
- Test: `services/ingestion/tests/test_sggov_bankruptcy_connector.py`

- [ ] **Step 1: Write failing connector tests**

Create `services/ingestion/tests/test_sggov_bankruptcy_connector.py`:

```python
from __future__ import annotations

from pathlib import Path

from src.connectors.sggov.bankruptcy import SGGovernmentBankruptcyConnector
from src.main import get_connector


def _write_dump(path: Path) -> None:
    path.write_text(
        "COPY public.bankruptcy_cases (id, case_number, identification_number, person_name, first_seen_at, last_seen_at, latest_document_type, latest_document_date) FROM stdin;\n"
        "1\t1561/2025\tS9350236A\tSHARIFAH ALFIEYAH BINTE ABDULLAH\t2026-05-05 13:05:42.10213+00\t2026-05-05 13:05:42.351832+00\tbankruptcy_order\t2026-02-26\n"
        "\\.\n"
        "COPY public.case_events (id, bankruptcy_case_id, source_document_id, event_type, event_date, identification_number, person_name, trustee_name, trustee_firm, raw_text, parsed_payload_json, created_at, updated_at) FROM stdin;\n"
        "7\t1\t3\tbankruptcy_order\t2026-02-26\tS9350236A\tSHARIFAH ALFIEYAH BINTE ABDULLAH\tGOH WEE TECK\tRSM CORPORATE ADVISORY PTE. LTD.\t1561/2025\\nS9350236A\t{}\t2026-05-05 13:05:42.10213+00\t2026-05-05 13:05:42.362259+00\n"
        "\\.\n"
        "COPY public.source_documents (id, source_page_id, document_type, source_url, raw_href, link_text, week_label, week_number, week_suffix, document_date, is_new, filename, local_path, content_sha256, first_seen_at, last_seen_at, downloaded_at, extraction_status, extracted_at, extraction_error) FROM stdin;\n"
        "3\t1\tbankruptcy_order\thttps://example.test/file.pdf\t/file.pdf\tBankruptcy Orders\t15\t15\t\\N\t2026-02-26\tf\tfile.pdf\t/data/file.pdf\tabc123\t2026-05-05 13:00:00+00\t2026-05-05 13:00:00+00\t2026-05-05 13:00:01+00\tsuccess\t2026-05-05 13:05:00+00\t\\N\n"
        "\\.\n",
        encoding="utf-8",
    )


def test_bankruptcy_connector_yields_case_envelope(tmp_path: Path) -> None:
    dump = tmp_path / "sgbankruptcy.sql"
    _write_dump(dump)

    connector = SGGovernmentBankruptcyConnector(dump_path=dump)
    records = list(connector.fetch_records())

    assert len(records) == 1
    record = records[0]
    assert record["source_record_id"] == "bankruptcy_case:1"
    assert record["observed_at"] == "2026-05-05T13:05:42.351832+00:00"
    assert record["record_type"] == "system"
    assert record["identifiers"] == [
        {"type": "nric", "value": "S9350236A", "is_verified": True}
    ]
    assert record["attributes"]["full_name"] == "SHARIFAH ALFIEYAH BINTE ABDULLAH"
    assert record["attributes"]["bankruptcy_case_number"] == "1561/2025"
    assert record["raw_payload"]["case"]["case_number"] == "1561/2025"
    assert record["raw_payload"]["event"]["trustee_name"] == "GOH WEE TECK"
    assert record["raw_payload"]["source_document"]["source_url"] == "https://example.test/file.pdf"
    assert isinstance(record["record_hash"], str)
    assert record["record_hash"].startswith("sha256:")


def test_bankruptcy_connector_is_registered() -> None:
    assert isinstance(get_connector("sgbankruptcy"), SGGovernmentBankruptcyConnector)
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `uv run pytest services/ingestion/tests/test_sggov_bankruptcy_connector.py -q`

Expected: import failure or connector not registered.

- [ ] **Step 3: Implement `bankruptcy.py`**

Create `services/ingestion/src/connectors/sggov/bankruptcy.py`:

```python
"""Connector for SG Bankruptcy Register dump records."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from src.connectors.base import SourceConnector
from src.connectors.fundbox.builders import IdentifierBag, build_envelope
from src.connectors.sggov.dump import CopyRow, parse_copy_tables
from src.models import JsonValue

_DEFAULT_DUMP_PATH = Path(".dumps/sgbankruptcy_2026-05-11.sql")


def _iso_datetime(value: JsonValue) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    stripped = value.strip()
    normalized = stripped.replace(" ", "T", 1)
    if normalized.endswith("+00"):
        normalized = f"{normalized}:00"
    try:
        return datetime.fromisoformat(normalized).isoformat()
    except ValueError:
        return normalized


def _str_value(row: CopyRow | None, key: str) -> str | None:
    if row is None:
        return None
    value = row.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _index_by_id(rows: list[CopyRow]) -> dict[str, CopyRow]:
    indexed: dict[str, CopyRow] = {}
    for row in rows:
        row_id = _str_value(row, "id")
        if row_id is not None:
            indexed[row_id] = row
    return indexed


def _events_by_case(rows: list[CopyRow]) -> dict[str, CopyRow]:
    indexed: dict[str, CopyRow] = {}
    for row in rows:
        case_id = _str_value(row, "bankruptcy_case_id")
        if case_id is not None and case_id not in indexed:
            indexed[case_id] = row
    return indexed


class SGGovernmentBankruptcyConnector(SourceConnector):
    """Yields one system source record per SG bankruptcy case."""

    def __init__(self, dump_path: Path = _DEFAULT_DUMP_PATH) -> None:
        self._dump_path = dump_path

    def get_source_key(self) -> str:
        return "sgbankruptcy"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        tables = parse_copy_tables(
            self._dump_path,
            {"bankruptcy_cases", "case_events", "source_documents"},
        )
        cases = tables.get("bankruptcy_cases", [])
        events = _events_by_case(tables.get("case_events", []))
        documents = _index_by_id(tables.get("source_documents", []))

        for case in cases:
            case_id = _str_value(case, "id")
            if case_id is None:
                continue
            event = events.get(case_id)
            document = documents.get(_str_value(event, "source_document_id") or "")
            yield self._build_envelope(case, event, document)

    def _build_envelope(
        self,
        case: CopyRow,
        event: CopyRow | None,
        document: CopyRow | None,
    ) -> dict[str, JsonValue]:
        case_id = _str_value(case, "id") or "unknown"
        identification_number = _str_value(case, "identification_number") or _str_value(
            event, "identification_number"
        )
        person_name = _str_value(case, "person_name") or _str_value(event, "person_name")
        event_type = _str_value(event, "event_type") or _str_value(case, "latest_document_type")
        event_date = _str_value(event, "event_date") or _str_value(case, "latest_document_date")

        identifiers = IdentifierBag()
        identifiers.add("nric", identification_number, verified=True)

        raw_payload: dict[str, JsonValue] = {
            "case": case,
            "event": event or {},
            "source_document": document or {},
        }
        return build_envelope(
            source_record_id=f"bankruptcy_case:{case_id}",
            observed_at=_iso_datetime(case.get("last_seen_at")) or _iso_datetime(case.get("first_seen_at")),
            identifiers=identifiers.items,
            attributes={
                "full_name": person_name,
                "bankruptcy_case_number": _str_value(case, "case_number"),
                "bankruptcy_document_type": _str_value(case, "latest_document_type"),
                "bankruptcy_document_date": _str_value(case, "latest_document_date"),
                "bankruptcy_event_type": event_type,
                "bankruptcy_event_date": event_date,
                "bankruptcy_trustee_name": _str_value(event, "trustee_name"),
                "bankruptcy_trustee_firm": _str_value(event, "trustee_firm"),
            },
            raw_payload=raw_payload,
        )
```

- [ ] **Step 4: Register connector in `services/ingestion/src/main.py`**

Add import:

```python
from src.connectors.sggov import SGGovernmentBankruptcyConnector
```

Add registry entry:

```python
    "sgbankruptcy": SGGovernmentBankruptcyConnector,
```

Do not add `sgrentalflats` to `_CONNECTOR_REGISTRY`.

- [ ] **Step 5: Run connector tests**

Run: `uv run pytest services/ingestion/tests/test_sggov_bankruptcy_connector.py -q`

Expected: PASS.

---

### Task 4: Add BankruptcyCase graph materialization

**Files:**
- Create: `services/ingestion/src/graph/queries/bankruptcy.py`
- Modify: `services/ingestion/src/graph/queries/__init__.py`
- Create: `services/ingestion/src/pipeline_bankruptcy.py`
- Modify: `services/ingestion/src/pipeline.py`
- Test: `services/ingestion/tests/test_bankruptcy_graph.py`

- [ ] **Step 1: Write failing graph tests**

Create `services/ingestion/tests/test_bankruptcy_graph.py`:

```python
from __future__ import annotations

from typing import cast

from neo4j import ManagedTransaction
from src.models import SourceRecordEnvelope
from src.pipeline_bankruptcy import materialize_bankruptcy_case


class _Tx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **kwargs: object) -> object:
        self.calls.append((query, kwargs))
        return object()


def _envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="sgbankruptcy",
        source_record_id="bankruptcy_case:1",
        observed_at="2026-05-05T13:05:42.351832+00:00",
        record_hash="sha256:abc",
        identifiers=[{"type": "nric", "value": "S9350236A", "is_verified": True}],
        attributes={"full_name": "SHARIFAH ALFIEYAH BINTE ABDULLAH"},
        raw_payload={
            "case": {
                "id": "1",
                "case_number": "1561/2025",
                "latest_document_type": "bankruptcy_order",
                "latest_document_date": "2026-02-26",
                "first_seen_at": "2026-05-05 13:05:42.10213+00",
                "last_seen_at": "2026-05-05 13:05:42.351832+00",
            },
            "event": {
                "event_type": "bankruptcy_order",
                "event_date": "2026-02-26",
                "trustee_name": "GOH WEE TECK",
                "trustee_firm": "RSM CORPORATE ADVISORY PTE. LTD.",
            },
            "source_document": {"source_url": "https://example.test/file.pdf"},
        },
    )


def test_materialize_bankruptcy_case_writes_case_for_sgbankruptcy() -> None:
    tx = _Tx()

    materialize_bankruptcy_case(
        cast(ManagedTransaction, tx),
        envelope=_envelope(),
        person_id="person-1",
        source_record_pk="sr-1",
    )

    assert len(tx.calls) == 1
    params = tx.calls[0][1]
    assert params["person_id"] == "person-1"
    assert params["source_record_pk"] == "sr-1"
    assert params["source_system_key"] == "sgbankruptcy"
    assert params["source_case_id"] == "1"
    assert params["case_number"] == "1561/2025"
    assert params["event_type"] == "bankruptcy_order"
    assert params["event_date"] == "2026-02-26"
    assert params["trustee_name"] == "GOH WEE TECK"
    assert params["source_url"] == "https://example.test/file.pdf"


def test_materialize_bankruptcy_case_skips_other_sources() -> None:
    tx = _Tx()
    envelope = _envelope().model_copy(update={"source_system": "fundbox"})

    materialize_bankruptcy_case(
        cast(ManagedTransaction, tx),
        envelope=envelope,
        person_id="person-1",
        source_record_pk="sr-1",
    )

    assert tx.calls == []
```

- [ ] **Step 2: Run graph test and verify it fails**

Run: `uv run pytest services/ingestion/tests/test_bankruptcy_graph.py -q`

Expected: import failure because `src.pipeline_bankruptcy` does not exist.

- [ ] **Step 3: Create bankruptcy query**

Create `services/ingestion/src/graph/queries/bankruptcy.py`:

```python
"""Cypher constants for bankruptcy case materialization."""

from __future__ import annotations

MERGE_BANKRUPTCY_CASE = """
MATCH (p:Person {person_id: $person_id})
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
MERGE (bc:BankruptcyCase {
    source_system_key: $source_system_key,
    source_case_id: $source_case_id
})
ON CREATE SET
    bc.bankruptcy_case_id = randomUUID(),
    bc.created_at = datetime()
SET
    bc.case_number = $case_number,
    bc.document_type = $document_type,
    bc.document_date = $document_date,
    bc.event_type = $event_type,
    bc.event_date = $event_date,
    bc.trustee_name = $trustee_name,
    bc.trustee_firm = $trustee_firm,
    bc.source_url = $source_url,
    bc.first_seen_at = CASE WHEN $first_seen_at IS NULL THEN null ELSE datetime($first_seen_at) END,
    bc.last_seen_at = CASE WHEN $last_seen_at IS NULL THEN null ELSE datetime($last_seen_at) END,
    bc.raw_payload = $raw_payload,
    bc.updated_at = datetime()
MERGE (p)-[person_rel:HAS_BANKRUPTCY_CASE]->(bc)
ON CREATE SET
    person_rel.first_seen_at = datetime(),
    person_rel.source_record_pk = $source_record_pk,
    person_rel.observed_at = datetime($observed_at)
ON MATCH SET
    person_rel.source_record_pk = $source_record_pk,
    person_rel.observed_at = datetime($observed_at),
    person_rel.last_seen_at = datetime()
MERGE (sr)-[record_rel:DESCRIBES_CASE]->(bc)
ON CREATE SET record_rel.linked_at = datetime()
"""
```

- [ ] **Step 4: Export query in `services/ingestion/src/graph/queries/__init__.py`**

Add import:

```python
from src.graph.queries.bankruptcy import MERGE_BANKRUPTCY_CASE
```

Add `"MERGE_BANKRUPTCY_CASE"` to `__all__`.

- [ ] **Step 5: Create graph helper**

Create `services/ingestion/src/pipeline_bankruptcy.py`:

```python
"""Bankruptcy-specific graph materialization for SG Gov ingestion."""

from __future__ import annotations

import json
from datetime import datetime

from neo4j import ManagedTransaction

from src.graph import queries
from src.models import JsonValue, SourceRecordEnvelope


def _row(payload: dict[str, JsonValue], key: str) -> dict[str, JsonValue]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _str_value(row: dict[str, JsonValue], key: str) -> str | None:
    value = row.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _iso_datetime(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().replace(" ", "T", 1)
    if normalized.endswith("+00"):
        normalized = f"{normalized}:00"
    try:
        return datetime.fromisoformat(normalized).isoformat()
    except ValueError:
        return normalized


def materialize_bankruptcy_case(
    tx: ManagedTransaction,
    *,
    envelope: SourceRecordEnvelope,
    person_id: str,
    source_record_pk: str,
) -> None:
    if envelope.source_system != "sgbankruptcy":
        return

    case = _row(envelope.raw_payload, "case")
    event = _row(envelope.raw_payload, "event")
    document = _row(envelope.raw_payload, "source_document")
    source_case_id = _str_value(case, "id")
    if source_case_id is None:
        return

    tx.run(
        queries.MERGE_BANKRUPTCY_CASE,
        person_id=person_id,
        source_record_pk=source_record_pk,
        source_system_key=envelope.source_system,
        source_case_id=source_case_id,
        case_number=_str_value(case, "case_number"),
        document_type=_str_value(case, "latest_document_type") or _str_value(document, "document_type"),
        document_date=_str_value(case, "latest_document_date") or _str_value(document, "document_date"),
        event_type=_str_value(event, "event_type"),
        event_date=_str_value(event, "event_date"),
        trustee_name=_str_value(event, "trustee_name"),
        trustee_firm=_str_value(event, "trustee_firm"),
        source_url=_str_value(document, "source_url"),
        first_seen_at=_iso_datetime(_str_value(case, "first_seen_at")),
        last_seen_at=_iso_datetime(_str_value(case, "last_seen_at")),
        observed_at=envelope.observed_at,
        raw_payload=json.dumps(envelope.raw_payload, default=str),
    )
```

- [ ] **Step 6: Call helper from `services/ingestion/src/pipeline.py`**

Add import:

```python
from src.pipeline_bankruptcy import materialize_bankruptcy_case
```

After `link_record_to_graph(...)` call and before `compute_golden_profile(tx, person_id)`, add:

```python
        materialize_bankruptcy_case(
            tx,
            envelope=envelope,
            person_id=person_id,
            source_record_pk=source_record_pk,
        )
```

- [ ] **Step 7: Run graph tests**

Run: `uv run pytest services/ingestion/tests/test_bankruptcy_graph.py -q`

Expected: PASS.

---

### Task 5: Verification

**Files:**
- All touched files.

- [ ] **Step 1: Run focused test suite**

Run: `uv run pytest services/ingestion/tests/test_sggov_dump.py services/ingestion/tests/test_sggov_bankruptcy_connector.py services/ingestion/tests/test_bankruptcy_graph.py -q`

Expected: PASS.

- [ ] **Step 2: Run ingestion tests**

Run: `uv run pytest services/ingestion/tests -q`

Expected: PASS.

- [ ] **Step 3: Run ruff check**

Run: `uv run --package profile-unifier-ingestion ruff check services/ingestion/src services/ingestion/tests`

Expected: PASS or only pre-existing failures unrelated to touched files.

- [ ] **Step 4: Run ruff format check**

Run: `uv run --package profile-unifier-ingestion ruff format --check services/ingestion/src services/ingestion/tests`

Expected: PASS.

- [ ] **Step 5: Run mypy strict on ingestion source**

Run: `uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src`

Expected: PASS.

- [ ] **Step 6: Confirm no schedules were added**

Run: `python - <<'PY'\nfrom pathlib import Path\ntext = Path('services/ingestion/src/celery_app.py').read_text(encoding='utf-8')\nassert 'sgbankruptcy' not in text\nassert 'sgrentalflats' not in text\nprint('no schedules added')\nPY`

Expected: prints `no schedules added`.

- [ ] **Step 7: Report and notify**

Summarize changed files, verification command results, and explicitly note that no commit was created. Send the user a push notification because they asked to be pinged once verification is done.
