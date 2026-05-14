# Ingestion Data Adjustments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract and write MachineUnit evidence from ingestion sources, add editable JSON exclusions, support versioned reingest, and sort chat messages by timestamp.

**Architecture:** Build on the MachineUnit graph plan. Keep extraction helpers separate from graph writes, merge JSON exclusions with existing env exclusions, and implement conservative versioned reingest that preserves immutable SourceRecord history. MachineUnit matching stops at safe review-only behavior; do not implement heuristic score changes without explicit approval.

**Tech Stack:** Python 3.12, Pydantic, Neo4j Cypher, Docker Compose, uv, pytest, ruff, mypy strict.

---

## File structure

- Create `services/ingestion/src/exclusion_config.py` — JSON exclusion-file loader.
- Create `config/ingestion-exclusions.example.json` — committed placeholder with empty arrays.
- Modify `.gitignore` — ignore `config/ingestion-exclusions.local.json`.
- Modify `docker-compose.yml` and `.docker/staging/docker-compose.yml` — bind mount exclusion JSON and set `INGESTION_EXCLUSIONS_FILE`.
- Modify `services/ingestion/src/config.py` — add `ingestion_exclusions_file` setting.
- Modify `services/ingestion/src/exclusions.py` — merge file exclusions with env exclusions through `ExclusionContext`.
- Create `services/ingestion/src/machine_unit_extraction.py` — extraction helpers from sales/chat payloads.
- Modify `services/ingestion/src/connectors/fundbox/sales.py` — ensure raw line metadata retains LTA/serial fields.
- Modify `services/ingestion/src/connectors/phppos_sales_common.py` — ensure raw line metadata retains serial number fields.
- Modify `services/ingestion/src/pipeline_sales.py` — write `INVOLVES_UNIT` and `BOUGHT_UNIT` from observations.
- Modify `services/ingestion/src/pipeline.py` and `services/ingestion/src/graph/queries/source_records.py` — versioned reingest detection and SourceRecord supersession.
- Modify `services/ingestion/src/connectors/whatsapp/connector.py` and `services/ingestion/src/connectors/bitrix/connector.py` — timestamp sort helpers and chat MachineUnit observations in raw payload.
- Add tests under `services/ingestion/tests/` for exclusions, extraction, sales writes, reingest, and chat ordering.

### Task 1: JSON exclusion file loader

**Files:**
- Create: `services/ingestion/src/exclusion_config.py`
- Modify: `services/ingestion/src/config.py`
- Test: `services/ingestion/tests/test_exclusion_config.py`

- [ ] **Step 1: Write failing tests**

```python
from __future__ import annotations

from pathlib import Path

import pytest

from src.exclusion_config import load_exclusion_file


def test_load_exclusion_file_returns_arrays(tmp_path: Path) -> None:
    path = tmp_path / "exclusions.json"
    path.write_text(
        '{"phones":["+6512345678"],"emails":["ops@example.com"],"names":["Ada Ops"],"source_ids":["staff-1"]}',
        encoding="utf-8",
    )

    loaded = load_exclusion_file(str(path))

    assert loaded.phones == ["+6512345678"]
    assert loaded.emails == ["ops@example.com"]
    assert loaded.names == ["Ada Ops"]
    assert loaded.source_ids == ["staff-1"]


def test_load_exclusion_file_blank_path_returns_empty() -> None:
    loaded = load_exclusion_file("")

    assert loaded.phones == []
    assert loaded.emails == []
    assert loaded.names == []
    assert loaded.source_ids == []


def test_load_exclusion_file_missing_configured_path_fails(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_exclusion_file(str(tmp_path / "missing.json"))


def test_load_exclusion_file_rejects_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{bad", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid ingestion exclusions JSON"):
        load_exclusion_file(str(path))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_exclusion_config.py -v`

Expected: FAIL with missing module.

- [ ] **Step 3: Implement loader**

```python
"""JSON-backed ingestion exclusion configuration."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel


class ExclusionFile(BaseModel):
    phones: list[str] = []
    emails: list[str] = []
    names: list[str] = []
    source_ids: list[str] = []


def load_exclusion_file(path_value: str) -> ExclusionFile:
    if not path_value.strip():
        return ExclusionFile()
    path = Path(path_value)
    if not path.exists():
        raise FileNotFoundError(f"Ingestion exclusions file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid ingestion exclusions JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid ingestion exclusions JSON: {path}")
    return ExclusionFile.model_validate(raw)
```

Add to `services/ingestion/src/config.py` settings:

```python
ingestion_exclusions_file: str = ""
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest services/ingestion/tests/test_exclusion_config.py -v`

Expected: PASS.

### Task 2: Placeholder file, gitignore, and Docker bind mount

**Files:**
- Create: `config/ingestion-exclusions.example.json`
- Modify: `.gitignore`
- Modify: `docker-compose.yml`
- Modify: `.docker/staging/docker-compose.yml`

- [ ] **Step 1: Create placeholder JSON**

Create `config/ingestion-exclusions.example.json`:

```json
{
  "phones": [],
  "emails": [],
  "names": [],
  "source_ids": []
}
```

- [ ] **Step 2: Ignore local exclusion file**

Add to `.gitignore`:

```gitignore
config/ingestion-exclusions.local.json
```

- [ ] **Step 3: Add compose env var and bind mounts**

In root `docker-compose.yml`, add to `x-ingestion-env`:

```yaml
  INGESTION_EXCLUSIONS_FILE: /app/config/ingestion-exclusions.local.json
```

Add this volume to `worker` and `beat` services:

```yaml
      - ./config/ingestion-exclusions.local.json:/app/config/ingestion-exclusions.local.json:ro
```

In `.docker/staging/docker-compose.yml`, mirror the env var and use the staging-relative bind mount:

```yaml
      - ../../config/ingestion-exclusions.local.json:/app/config/ingestion-exclusions.local.json:ro
```

- [ ] **Step 4: Verify files contain expected strings**

Run: `uv run python -c "from pathlib import Path; assert 'INGESTION_EXCLUSIONS_FILE' in Path('docker-compose.yml').read_text(); assert '../../config/ingestion-exclusions.local.json' in Path('.docker/staging/docker-compose.yml').read_text(); assert 'config/ingestion-exclusions.local.json' in Path('.gitignore').read_text()"`

Expected: PASS with no output.

### Task 3: Merge file exclusions into existing exclusion context

**Files:**
- Modify: `services/ingestion/src/exclusions.py`
- Test: `services/ingestion/tests/test_exclusions.py`

- [ ] **Step 1: Add failing merge test**

```python
from src.exclusion_config import ExclusionFile
from src.exclusions import build_exclusion_context


def test_build_exclusion_context_merges_env_and_file_values() -> None:
    context = build_exclusion_context(
        company_mobile_numbers=["+6511111111"],
        company_email_addresses=["env@example.com"],
        internal_person_names=["Env Person"],
        file_exclusions=ExclusionFile(
            phones=["+6522222222"],
            emails=["file@example.com"],
            names=["File Person"],
            source_ids=["staff-1"],
        ),
    )

    assert "+6511111111" in context.phones
    assert "+6522222222" in context.phones
    assert "env@example.com" in context.emails
    assert "file@example.com" in context.emails
    assert "staff-1" in context.source_ids
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_exclusions.py::test_build_exclusion_context_merges_env_and_file_values -v`

Expected: FAIL with missing `build_exclusion_context` or argument mismatch.

- [ ] **Step 3: Implement context builder**

Add to `services/ingestion/src/exclusions.py`:

```python
from src.exclusion_config import ExclusionFile


def build_exclusion_context(
    *,
    company_mobile_numbers: list[str],
    company_email_addresses: list[str],
    internal_person_names: list[str],
    file_exclusions: ExclusionFile,
) -> ExclusionContext:
    return ExclusionContext(
        phones=normalized_phone_set(company_mobile_numbers + file_exclusions.phones),
        emails=normalized_email_set(company_email_addresses + file_exclusions.emails),
        names=normalized_name_set(internal_person_names + file_exclusions.names),
        source_ids=frozenset(value.strip().lower() for value in file_exclusions.source_ids if value.strip()),
    )
```

- [ ] **Step 4: Run test**

Run: `uv run pytest services/ingestion/tests/test_exclusions.py::test_build_exclusion_context_merges_env_and_file_values -v`

Expected: PASS.

### Task 4: MachineUnit extraction helpers from sales and chat payloads

**Files:**
- Create: `services/ingestion/src/machine_unit_extraction.py`
- Test: `services/ingestion/tests/test_machine_unit_extraction.py`

- [ ] **Step 1: Write failing extraction tests**

```python
from __future__ import annotations

from src.machine_unit_extraction import observations_from_chat_inquiries, observations_from_sales_lines


def test_observations_from_sales_lines_extracts_fundbox_lta_and_serial() -> None:
    observations = observations_from_sales_lines(
        source_system_key="fundbox_consumer_backend",
        source_record_id="fundbox-sale-1",
        observed_at="2026-05-14T00:00:00",
        lines=[
            {
                "source_line_item_id": "line-1",
                "metadata": {"lta_tag": "LTA 123", "serial_no": "SN-9"},
                "product": {"display_name": "Bike A"},
            }
        ],
    )

    assert len(observations) == 1
    assert observations[0].lta_tag == "LTA 123"
    assert observations[0].serial_number == "SN-9"
    assert observations[0].machine_product == "Bike A"


def test_observations_from_sales_lines_extracts_phppos_serialnumber() -> None:
    observations = observations_from_sales_lines(
        source_system_key="speedzone_phppos",
        source_record_id="speedzone-sale-1",
        observed_at="2026-05-14T00:00:00",
        lines=[{"source_line_item_id": "line-1", "metadata": {"serialnumber": "SN-10"}}],
    )

    assert len(observations) == 1
    assert observations[0].serial_number == "SN-10"
    assert observations[0].lta_tag is None


def test_observations_from_chat_inquiries_are_inquiry_evidence() -> None:
    observations = observations_from_chat_inquiries(
        source_system_key="whatsapp_chat",
        source_record_id="whatsapp-chat-1",
        observed_at="2026-05-14T00:00:00",
        inquiries=[{"lta_tag": "LTA123", "serial_number": "SN-9", "notes": "Asked availability"}],
    )

    assert len(observations) == 1
    assert observations[0].source_kind == "chat_inquiry"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_machine_unit_extraction.py -v`

Expected: FAIL with missing module.

- [ ] **Step 3: Implement extraction helpers**

```python
"""Extract MachineUnit observations from connector payloads."""

from __future__ import annotations

from src.machine_units import MachineUnitObservation, valid_machine_unit_observation
from src.models import JsonValue, QualityFlag


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _product_name(line: dict[str, JsonValue]) -> str | None:
    product = line.get("product")
    if isinstance(product, dict):
        display = product.get("display_name")
        name = product.get("name")
        return _str_or_none(display) or _str_or_none(name)
    return None


def observations_from_sales_lines(
    *,
    source_system_key: str,
    source_record_id: str,
    observed_at: str | None,
    lines: list[JsonValue],
) -> list[MachineUnitObservation]:
    observations: list[MachineUnitObservation] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        metadata = line.get("metadata")
        if not isinstance(metadata, dict):
            continue
        observation = MachineUnitObservation(
            lta_tag=_str_or_none(metadata.get("lta_tag")),
            serial_number=_str_or_none(metadata.get("serial_no")) or _str_or_none(metadata.get("serialnumber")),
            machine_product=_product_name(line),
            unit_label=_str_or_none(metadata.get("unit")),
            source_kind="sales",
            source_system_key=source_system_key,
            source_record_id=source_record_id,
            observed_at=observed_at,
            confidence=1.0,
            quality_flag=QualityFlag.VALID,
            raw_context=_str_or_none(line.get("source_line_item_id")),
        )
        if valid_machine_unit_observation(observation):
            observations.append(observation)
    return observations


def observations_from_chat_inquiries(
    *,
    source_system_key: str,
    source_record_id: str,
    observed_at: str | None,
    inquiries: list[JsonValue],
) -> list[MachineUnitObservation]:
    observations: list[MachineUnitObservation] = []
    for inquiry in inquiries:
        if not isinstance(inquiry, dict):
            continue
        observation = MachineUnitObservation(
            lta_tag=_str_or_none(inquiry.get("lta_tag")),
            serial_number=_str_or_none(inquiry.get("serial_number")),
            machine_product=_str_or_none(inquiry.get("machine_product")),
            unit_label=_str_or_none(inquiry.get("unit")),
            source_kind="chat_inquiry",
            source_system_key=source_system_key,
            source_record_id=source_record_id,
            observed_at=observed_at,
            confidence=0.6,
            quality_flag=QualityFlag.PARTIAL_PARSE,
            raw_context=_str_or_none(inquiry.get("notes")),
        )
        if valid_machine_unit_observation(observation):
            observations.append(observation)
    return observations
```

- [ ] **Step 4: Run extraction tests**

Run: `uv run pytest services/ingestion/tests/test_machine_unit_extraction.py -v`

Expected: PASS.

### Task 5: Sales MachineUnit writes

**Files:**
- Modify: `services/ingestion/src/pipeline_sales.py`
- Test: `services/ingestion/tests/test_pipeline_sales_machine_units.py`

- [ ] **Step 1: Write focused fake-transaction test**

```python
from __future__ import annotations

from src.models import RecordType, SourceRecordEnvelope
from src.pipeline_sales import _ingest_sales_record_tx


class _Result:
    def __init__(self, row: dict[str, object] | None = None) -> None:
        self._row = row

    def single(self) -> dict[str, object] | None:
        return self._row


class _Tx:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.params: list[dict[str, object]] = []

    def run(self, query: str, **kwargs: object) -> _Result:
        self.queries.append(query)
        self.params.append(kwargs)
        if "CREATE (sr:SourceRecord" in query:
            return _Result({"source_record_pk": "sr-1"})
        if "RETURN unit.machine_unit_id" in query:
            return _Result({"machine_unit_id": "mu-1", "conflict": False})
        if "RETURN p.person_id" in query:
            return _Result({"person_id": "person-1"})
        return _Result({})


def test_sales_ingest_writes_machine_unit_links() -> None:
    tx = _Tx()
    envelope = SourceRecordEnvelope(
        source_system="fundbox_consumer_backend",
        source_record_id="fundbox-sale-1",
        record_type=RecordType.SALES,
        observed_at="2026-05-14T00:00:00",
        record_hash="hash-1",
        raw_payload={
            "order": {"source_order_id": "order-1", "currency": "SGD"},
            "line_items": [
                {
                    "source_line_item_id": "line-1",
                    "line_no": 1,
                    "currency": "SGD",
                    "metadata": {"lta_tag": "LTA123", "serial_no": "SN-9"},
                    "product": {"source_product_id": "product-1", "display_name": "Bike A", "is_active": True, "attributes": {}},
                }
            ],
            "customer_link": {"identity_source_record_id": "identity-1", "source_system_key": "fundbox_consumer_backend"},
        },
    )

    _ingest_sales_record_tx(tx, envelope, ingest_run_id=None)

    joined_queries = "\n".join(tx.queries)
    assert "INVOLVES_UNIT" in joined_queries
    assert "BOUGHT_UNIT" in joined_queries
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_pipeline_sales_machine_units.py -v`

Expected: FAIL because sales pipeline does not write MachineUnit links.

- [ ] **Step 3: Add MachineUnit write helpers to sales pipeline**

In `services/ingestion/src/pipeline_sales.py`, import:

```python
from src.machine_unit_extraction import observations_from_sales_lines
from src.machine_units import normalize_lta_tag, normalize_serial_number
```

Add helper:

```python
def _write_machine_unit_observations(
    tx: ManagedTransaction,
    *,
    source_system_key: str,
    source_record_pk: str,
    source_record_id: str,
    source_order_id: str,
    observed_at: str | None,
    line_items: list[JsonValue],
    person_id: str | None,
) -> None:
    observations = observations_from_sales_lines(
        source_system_key=source_system_key,
        source_record_id=source_record_id,
        observed_at=observed_at,
        lines=line_items,
    )
    for observation in observations:
        row = tx.run(
            queries.UPSERT_MACHINE_UNIT,
            lta_tag=observation.lta_tag,
            normalized_lta_tag=normalize_lta_tag(observation.lta_tag),
            serial_number=observation.serial_number,
            normalized_serial_number=normalize_serial_number(observation.serial_number),
        ).single()
        if row is None:
            continue
        machine_unit_id = str(row["machine_unit_id"])
        tx.run(
            queries.LINK_ORDER_INVOLVES_UNIT,
            source_system_key=source_system_key,
            source_order_id=source_order_id,
            source_record_pk=source_record_pk,
            machine_unit_id=machine_unit_id,
            raw_context=observation.raw_context,
            observed_at=observation.observed_at,
            confidence=observation.confidence,
            quality_flag=observation.quality_flag.value,
        )
        if person_id is not None:
            tx.run(
                queries.LINK_PERSON_BOUGHT_UNIT,
                person_id=person_id,
                source_system_key=source_system_key,
                source_order_id=source_order_id,
                source_record_pk=source_record_pk,
                machine_unit_id=machine_unit_id,
                raw_context=observation.raw_context,
                observed_at=observation.observed_at,
                confidence=observation.confidence,
                quality_flag=observation.quality_flag.value,
            )
```

Call this after `_resolve_and_link_customer(...)`:

```python
    _write_machine_unit_observations(
        tx,
        source_system_key=source_system_key,
        source_record_pk=source_record_pk,
        source_record_id=envelope.source_record_id,
        source_order_id=source_order_id,
        observed_at=envelope.observed_at,
        line_items=cast(list[JsonValue], line_items),
        person_id=person_id,
    )
```

- [ ] **Step 4: Run test**

Run: `uv run pytest services/ingestion/tests/test_pipeline_sales_machine_units.py -v`

Expected: PASS.

### Task 6: Versioned SourceRecord reingest

**Files:**
- Modify: `services/ingestion/src/graph/queries/source_records.py`
- Modify: `services/ingestion/src/pipeline.py`
- Modify: `services/ingestion/src/pipeline_sales.py`
- Test: `services/ingestion/tests/test_source_record_reingest.py`

- [ ] **Step 1: Write query/decision tests**

```python
from __future__ import annotations

from src.graph import queries


def test_source_record_reingest_queries_exist() -> None:
    assert "is_latest" in queries.GET_LATEST_SOURCE_RECORD
    assert "SUPERSEDED_BY" in queries.SUPERSEDE_SOURCE_RECORD
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_source_record_reingest.py -v`

Expected: FAIL with missing query exports.

- [ ] **Step 3: Add source-record version queries**

Add to `services/ingestion/src/graph/queries/source_records.py`:

```python
GET_LATEST_SOURCE_RECORD = """
MATCH (sr:SourceRecord {source_record_id: $source_record_id})-[:FROM_SOURCE]->(:SourceSystem {source_key: $source_system})
WHERE coalesce(sr.is_latest, true) = true
RETURN sr.source_record_pk AS source_record_pk,
       sr.record_hash AS record_hash,
       coalesce(sr.source_record_version, 1) AS source_record_version
ORDER BY sr.source_record_version DESC
LIMIT 1
"""

SUPERSEDE_SOURCE_RECORD = """
MATCH (old:SourceRecord {source_record_pk: $old_source_record_pk})
MATCH (new:SourceRecord {source_record_pk: $new_source_record_pk})
SET old.is_latest = false,
    old.superseded_at = datetime(),
    old.updated_at = datetime(),
    new.is_latest = true,
    new.updated_at = datetime()
MERGE (old)-[:SUPERSEDED_BY]->(new)
"""
```

Modify `CREATE_SOURCE_RECORD` to set:

```cypher
is_latest:             true,
```

Re-export `GET_LATEST_SOURCE_RECORD` and `SUPERSEDE_SOURCE_RECORD` from `queries/__init__.py`.

- [ ] **Step 4: Add reingest decision helper**

Add a small helper in both identity and sales pipelines or a shared module:

```python
class ReingestDecision(TypedDict):
    latest_pk: str | None
    unchanged: bool
    next_version: int


def _get_reingest_decision(client: Neo4jClient, envelope: SourceRecordEnvelope) -> ReingestDecision:
    def _read(tx: ManagedTransaction) -> ReingestDecision:
        row = tx.run(
            queries.GET_LATEST_SOURCE_RECORD,
            source_system=envelope.source_system,
            source_record_id=envelope.source_record_id,
        ).single()
        if row is None:
            return {"latest_pk": None, "unchanged": False, "next_version": 1}
        latest_hash = str(row["record_hash"])
        latest_version = int(row["source_record_version"])
        return {
            "latest_pk": str(row["source_record_pk"]),
            "unchanged": latest_hash == envelope.record_hash,
            "next_version": latest_version + 1,
        }

    return client.execute_read(_read)
```

Use the helper so unchanged records skip and changed records set `envelope.source_record_version` to `next_version` before creating the new SourceRecord. After the new SourceRecord is created, run `SUPERSEDE_SOURCE_RECORD` when `latest_pk` is not `None`.

- [ ] **Step 5: Run reingest tests**

Run: `uv run pytest services/ingestion/tests/test_source_record_reingest.py -v`

Expected: PASS.

### Task 7: Chat timestamp sorting

**Files:**
- Modify: `services/ingestion/src/connectors/whatsapp/connector.py`
- Modify: `services/ingestion/src/connectors/bitrix/connector.py`
- Test: `services/ingestion/tests/test_chat_timestamp_ordering.py`

- [ ] **Step 1: Write failing order tests**

```python
from __future__ import annotations

from datetime import datetime

from src.connectors.whatsapp.connector import _format_messages


def test_whatsapp_format_messages_sorts_oldest_to_newest() -> None:
    text = _format_messages(
        [
            {"timestamp": datetime(2026, 5, 14, 10, 2), "body": "later", "from_id": "1@c.us"},
            {"timestamp": datetime(2026, 5, 14, 10, 1), "body": "earlier", "from_id": "2@c.us"},
        ]
    )

    assert text.index("earlier") < text.index("later")
```

- [ ] **Step 2: Run test to verify it fails if sorting is absent**

Run: `uv run pytest services/ingestion/tests/test_chat_timestamp_ordering.py -v`

Expected: FAIL if the formatter preserves input order.

- [ ] **Step 3: Sort WhatsApp messages in formatter**

Add helper in `whatsapp/connector.py`:

```python
def _message_sort_key(msg: dict[str, object]) -> tuple[int, str, str]:
    ts = msg.get("timestamp")
    if isinstance(ts, datetime):
        return (0, ts.isoformat(), str(msg.get("id") or ""))
    return (1, "", str(msg.get("id") or ""))
```

At the top of `_format_messages`, iterate over `sorted(msgs, key=_message_sort_key)` instead of `msgs`.

- [ ] **Step 4: Sort Bitrix combined events**

In `bitrix/connector.py`, when building conversation lines from personalized and sent-message logs, collect event dictionaries first:

```python
events: list[tuple[datetime | None, str, int, str]] = []
```

Append each formatted line with its timestamp/source/id, then sort:

```python
for _ts, _source, _row_id, line in sorted(
    events,
    key=lambda item: (0 if item[0] is not None else 1, item[0] or datetime.max, item[1], item[2]),
):
    lines.append(line)
```

Keep deal title as a non-timestamped context header before sorted event lines.

- [ ] **Step 5: Run timestamp tests**

Run: `uv run pytest services/ingestion/tests/test_chat_timestamp_ordering.py services/ingestion/tests/test_whatsapp_connector.py services/ingestion/tests/test_bitrix_connector.py -v`

Expected: PASS.

### Task 8: Matching stopping-point implementation

**Files:**
- Modify: `services/ingestion/src/matching/engine.py` only if implementing review-only candidate routing.

- [ ] **Step 1: Apply conservative stopping-point decision**

Do not modify heuristic score weights in this plan. The first implementation writes MachineUnit graph evidence and leaves MachineUnit matching review-case scoring for a follow-up approval.

Document this in the final verification summary:

```text
MachineUnit matching stopped at graph evidence writes. Heuristic scoring was not changed, preserving the review-only stopping point.
```

### Task 9: Ingestion data adjustments verification

**Files:**
- None

- [ ] **Step 1: Run focused tests**

Run: `uv run pytest services/ingestion/tests/test_exclusion_config.py services/ingestion/tests/test_exclusions.py services/ingestion/tests/test_machine_unit_extraction.py services/ingestion/tests/test_pipeline_sales_machine_units.py services/ingestion/tests/test_source_record_reingest.py services/ingestion/tests/test_chat_timestamp_ordering.py -v`

Expected: PASS.

- [ ] **Step 2: Run ingestion test suite**

Run: `uv run pytest services/ingestion/tests -v`

Expected: PASS.

- [ ] **Step 3: Run lint**

Run: `uv run --package profile-unifier-ingestion ruff check services/ingestion/src services/ingestion/tests`

Expected: PASS.

- [ ] **Step 4: Run type check**

Run: `uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src`

Expected: PASS or only documented pre-existing failures outside changed files.

## Self-review

- Spec coverage: source extraction, sales writes, exclusions, compose mounts, versioned reingest, timestamp ordering, and matching stopping point are covered.
- Placeholder scan: no placeholder tasks remain.
- Type consistency: `MachineUnitObservation`, `ExclusionFile`, and query names align with the MachineUnit graph plan.
