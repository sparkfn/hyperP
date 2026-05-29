# MachineUnit Sales-First Chat-Link Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create MachineUnit nodes from deterministic sales dump data and make chat evidence link only to existing MachineUnit/Person/Order context.

**Architecture:** Sales ingestion remains the only MachineUnit creation path. Dump connectors must preserve product plus unit identifiers in the shared line-item shape consumed by `observations_from_sales_lines`; chat ingestion uses separate link-only queries that never call `UPSERT_MACHINE_UNIT`.

**Tech Stack:** Python 3.12, Neo4j Cypher query constants, SQL dump fixture files, uv, pytest, ruff, mypy strict, Docker Compose for end-to-end ingestion verification.

---

## File Structure

- Modify `services/ingestion/src/connectors/dumps/connectors.py`
  - Fix Fundbox dump product lookup through `merchant_products.product_variant_id`.
  - Align PHPPOS dump sales line payloads with the shared machine-unit extractor by adding `metadata.serialnumber`.
- Modify `services/ingestion/src/machine_unit_extraction.py`
  - Keep sales extraction generic; allow top-level serial fallback if needed for already-built PHPPOS line payloads.
- Modify `services/ingestion/src/graph/queries/machine_units.py`
  - Add link-only query constants for chat-resolved existing MachineUnit evidence.
- Modify `services/ingestion/src/graph/queries/__init__.py`
  - Export new query constants.
- Modify `services/ingestion/src/pipeline.py`
  - Change chat machine-unit writer so chat never creates MachineUnit nodes; it resolves existing units and links evidence only.
- Modify `services/ingestion/tests/test_dump_connectors.py`
  - Regression tests for Fundbox product lookup and PHPPOS serial metadata payload.
- Modify `services/ingestion/tests/test_machine_unit_extraction.py`
  - Regression tests for PHPPOS top-level/metadata serial extraction.
- Modify `services/ingestion/tests/test_machine_unit_queries.py`
  - Query-shape tests for chat link-only matching and no create/upsert behavior.
- Modify `services/ingestion/tests/test_chat_extraction_batch.py` or add a focused pipeline test if current fixtures fit.
  - Prove chat machine-unit path does not call `UPSERT_MACHINE_UNIT`.
- Modify limited dump files under `.dumps/limited-100/`
  - `fundbox_sales_100.sql`
  - `eko_sales_100.sql`
  - `speedzone_sales_100.sql`

---

### Task 1: Fix Fundbox dump product lookup

**Files:**
- Modify: `services/ingestion/src/connectors/dumps/connectors.py`
- Test: `services/ingestion/tests/test_dump_connectors.py`

- [ ] **Step 1: Add a failing regression test**

Add this test to `services/ingestion/tests/test_dump_connectors.py`:

```python
def test_fundbox_sales_dump_resolves_product_from_product_variant_id(tmp_path: Path) -> None:
    dump_path = tmp_path / "fundbox.sql"
    dump_path.write_text(
        "\n".join(
            [
                "INSERT INTO `orders` (`id`,`order_no`,`user_id`,`merchant_id`,`status`,`created_at`,`updated_at`,`deleted_at`) VALUES (10,'INV-10',123,1,'completed','2026-05-01 00:00:00','2026-05-01 00:00:00',NULL);",
                "INSERT INTO `order_items` (`id`,`order_id`,`merchant_product_id`,`quantity`,`price`,`lta_tag`,`serial_no`,`created_at`,`updated_at`) VALUES (77,10,501,1,1599.00,'X891','SN-891','2026-05-01 00:00:00','2026-05-01 00:00:00');",
                "INSERT INTO `merchant_products` (`id`,`merchant_id`,`product_variant_id`,`price`,`created_at`,`updated_at`) VALUES (501,1,701,1599.00,'2026-05-01 00:00:00','2026-05-01 00:00:00');",
                "INSERT INTO `product_variants` (`id`,`product_id`,`sku`,`name`,`image`,`price`,`attributes`,`active`,`visible`,`deleted_at`,`created_at`,`updated_at`) VALUES (701,801,'SKU-701','Variant Bike','',1599.00,'{}',1,1,NULL,'2026-05-01 00:00:00','2026-05-01 00:00:00');",
                "INSERT INTO `products` (`id`,`product_id`,`name`,`image`,`type`,`sub_type`,`category`,`sub_category`,`description`,`make`,`model`,`has_serial_number`,`has_lta_tag`,`active`,`visible`,`deleted_at`,`created_at`,`updated_at`) VALUES (801,'P-801','Parent Bike','','Micro Mobility',NULL,'Bicycles',NULL,'','Brand','Model X',1,1,1,1,NULL,'2026-05-01 00:00:00','2026-05-01 00:00:00');",
            ]
        ),
        encoding="utf-8",
    )

    records = list(FundboxSalesDumpConnector(dump_path).fetch_records())

    assert len(records) == 1
    raw_payload = records[0]["raw_payload"]
    assert isinstance(raw_payload, dict)
    line_items = raw_payload["line_items"]
    assert isinstance(line_items, list)
    line = line_items[0]
    assert isinstance(line, dict)
    product = line["product"]
    assert isinstance(product, dict)
    assert product["display_name"] == "Parent Bike"
    assert product["name"] == "Variant Bike"
    assert product["attributes"] == {
        "variant_attributes": "{}",
        "type": "Micro Mobility",
        "sub_type": None,
        "model": "Model X",
    }
```

Ensure imports include:

```python
from pathlib import Path
from src.connectors.dumps.connectors import FundboxSalesDumpConnector
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
uv run pytest services/ingestion/tests/test_dump_connectors.py::test_fundbox_sales_dump_resolves_product_from_product_variant_id -v
```

Expected: FAIL because product is `None` due to lookup using `variant_id` instead of `product_variant_id`.

- [ ] **Step 3: Fix the lookup**

In `services/ingestion/src/connectors/dumps/connectors.py`, replace:

```python
variant = variants.get(_row_int(merchant_product, "variant_id"))
```

with:

```python
variant = variants.get(_row_int(merchant_product, "product_variant_id"))
```

- [ ] **Step 4: Verify the test passes**

Run:

```bash
uv run pytest services/ingestion/tests/test_dump_connectors.py::test_fundbox_sales_dump_resolves_product_from_product_variant_id -v
```

Expected: PASS.

---

### Task 2: Align PHPPOS sales serial payloads with machine-unit extraction

**Files:**
- Modify: `services/ingestion/src/connectors/dumps/connectors.py`
- Modify: `services/ingestion/src/machine_unit_extraction.py`
- Test: `services/ingestion/tests/test_dump_connectors.py`
- Test: `services/ingestion/tests/test_machine_unit_extraction.py`

- [ ] **Step 1: Add PHPPOS dump connector payload test**

Add this test to `services/ingestion/tests/test_dump_connectors.py`:

```python
def test_phppos_sales_dump_puts_serialnumber_in_metadata(tmp_path: Path) -> None:
    dump_path = tmp_path / "phppos.sql"
    dump_path.write_text(
        "\n".join(
            [
                "INSERT INTO `phppos_sales` (`sale_id`,`customer_id`,`sale_time`,`invoice_date`,`invoice_number`,`sale_status`,`suspended`) VALUES (1,55,'2026-05-01 00:00:00','2026-05-01','INV-1','0','0');",
                "INSERT INTO `phppos_sales_items` (`sale_id`,`item_id`,`line`,`quantity_purchased`,`item_unit_price`,`discount_percent`,`serialnumber`) VALUES (1,22,0,1.0,899.0,0.0,'SER-22');",
                "INSERT INTO `phppos_items` (`item_id`,`item_number`,`name`,`category`,`subcategory`,`size`,`cost_price`,`unit_price`,`description`) VALUES (22,'SKU-22','Scooter Model','Scooters','Electric','Large',500.0,899.0,'');",
            ]
        ),
        encoding="utf-8",
    )

    records = list(_fetch_phppos_dump_sales(dump_path, "eko_phppos"))

    assert len(records) == 1
    raw_payload = records[0]["raw_payload"]
    assert isinstance(raw_payload, dict)
    line_items = raw_payload["line_items"]
    assert isinstance(line_items, list)
    line = line_items[0]
    assert isinstance(line, dict)
    metadata = line["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["serialnumber"] == "SER-22"
```

Ensure imports include `_fetch_phppos_dump_sales`.

- [ ] **Step 2: Add extractor fallback test**

Add this test to `services/ingestion/tests/test_machine_unit_extraction.py`:

```python
def test_observations_from_sales_lines_extracts_top_level_phppos_serial() -> None:
    observations = observations_from_sales_lines(
        source_system_key="eko_phppos:sales",
        source_record_id="eko-sale-1",
        observed_at="2026-05-14T00:00:00",
        lines=[
            {
                "source_line_id": "eko-line-1",
                "serial_number": "SER-22",
                "product": {"display_name": "Scooter Model", "name": "Scooter Model"},
            }
        ],
    )

    assert len(observations) == 1
    assert observations[0].serial_number == "SER-22"
    assert observations[0].machine_product == "Scooter Model"
    assert observations[0].raw_context == "eko-line-1"
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
uv run pytest services/ingestion/tests/test_dump_connectors.py::test_phppos_sales_dump_puts_serialnumber_in_metadata services/ingestion/tests/test_machine_unit_extraction.py::test_observations_from_sales_lines_extracts_top_level_phppos_serial -v
```

Expected: FAIL because PHPPOS lines do not include `metadata`, and the extractor ignores top-level `serial_number` and `source_line_id`.

- [ ] **Step 4: Add metadata to PHPPOS sales line payload**

In `_build_phppos_sales_envelope` in `services/ingestion/src/connectors/dumps/connectors.py`, change the line item dict to include:

```python
"source_line_item_id": f"{source_system_key}-sale-{sale.sale_id}-line-{line.line}",
"metadata": {"serialnumber": line.serialnumber},
```

Keep existing `source_line_id`, `serial_number`, and `raw` fields.

- [ ] **Step 5: Add top-level fallback in extractor**

In `services/ingestion/src/machine_unit_extraction.py`, change metadata handling so missing metadata is allowed:

```python
metadata_raw = line.get("metadata")
metadata: dict[str, JsonValue] = metadata_raw if isinstance(metadata_raw, dict) else {}
serial_number = (
    _str_or_none(metadata.get("serial_no"))
    or _str_or_none(metadata.get("serialnumber"))
    or _str_or_none(line.get("serial_number"))
)
raw_context = _str_or_none(line.get("source_line_item_id")) or _str_or_none(
    line.get("source_line_id")
)
```

Use `serial_number=serial_number` and `raw_context=raw_context` when creating `MachineUnitObservation`.

- [ ] **Step 6: Verify tests pass**

Run:

```bash
uv run pytest services/ingestion/tests/test_dump_connectors.py::test_phppos_sales_dump_puts_serialnumber_in_metadata services/ingestion/tests/test_machine_unit_extraction.py::test_observations_from_sales_lines_extracts_top_level_phppos_serial -v
```

Expected: PASS.

---

### Task 3: Add chat link-only MachineUnit queries and pipeline behavior

**Files:**
- Modify: `services/ingestion/src/graph/queries/machine_units.py`
- Modify: `services/ingestion/src/graph/queries/__init__.py`
- Modify: `services/ingestion/src/pipeline.py`
- Test: `services/ingestion/tests/test_machine_unit_queries.py`

- [ ] **Step 1: Add query-shape tests**

Add these tests to `services/ingestion/tests/test_machine_unit_queries.py`:

```python
def test_chat_machine_unit_query_does_not_create_units() -> None:
    query = queries.RESOLVE_EXISTING_MACHINE_UNIT_FOR_CHAT

    assert "CREATE (" not in query
    assert "MERGE (u:MachineUnit" not in query
    assert "MATCH (u:MachineUnit" in query
    assert "normalized_lta_tag" in query
    assert "normalized_serial_number" in query


def test_chat_mentions_unit_query_links_existing_source_record_to_unit() -> None:
    query = queries.LINK_CHAT_SOURCE_RECORD_MENTIONS_EXISTING_UNIT

    assert "MATCH (sr:SourceRecord" in query
    assert "MATCH (u:MachineUnit" in query
    assert "MERGE (sr)-[rel:MENTIONS_UNIT]->(u)" in query
    assert "CREATE (u:MachineUnit" not in query
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest services/ingestion/tests/test_machine_unit_queries.py::test_chat_machine_unit_query_does_not_create_units services/ingestion/tests/test_machine_unit_queries.py::test_chat_mentions_unit_query_links_existing_source_record_to_unit -v
```

Expected: FAIL because query constants do not exist.

- [ ] **Step 3: Add link-only query constants**

Add to `services/ingestion/src/graph/queries/machine_units.py`:

```python
RESOLVE_EXISTING_MACHINE_UNIT_FOR_CHAT = """
MATCH (u:MachineUnit)
WHERE (
    $normalized_lta_tag IS NOT NULL
    AND u.normalized_lta_tag = $normalized_lta_tag
    AND ($normalized_machine_product IS NULL OR u.normalized_machine_product = $normalized_machine_product)
  )
  OR (
    $normalized_serial_number IS NOT NULL
    AND u.normalized_serial_number = $normalized_serial_number
    AND ($normalized_machine_product IS NULL OR u.normalized_machine_product = $normalized_machine_product)
  )
RETURN collect(DISTINCT u.machine_unit_id) AS machine_unit_ids
"""

LINK_CHAT_SOURCE_RECORD_MENTIONS_EXISTING_UNIT = """
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
MATCH (u:MachineUnit {machine_unit_id: $machine_unit_id})
MERGE (sr)-[rel:MENTIONS_UNIT]->(u)
SET rel.source_system_key = $source_system_key,
    rel.source_record_id = $source_record_id,
    rel.raw_context = $raw_context,
    rel.observed_at = $observed_at,
    rel.confidence = $confidence,
    rel.quality_flag = $quality_flag,
    rel.last_seen_at = datetime(),
    rel.updated_at = datetime()
"""
```

- [ ] **Step 4: Export query constants**

In `services/ingestion/src/graph/queries/__init__.py`, import and add both constants to `__all__`.

- [ ] **Step 5: Change chat writer to link-only**

In `services/ingestion/src/pipeline.py`, inside `_write_chat_machine_unit_observations`, replace the `UPSERT_MACHINE_UNIT` call with:

```python
row = tx.run(
    queries.RESOLVE_EXISTING_MACHINE_UNIT_FOR_CHAT,
    normalized_machine_product=normalize_machine_product(observation.machine_product),
    normalized_lta_tag=normalize_lta_tag(observation.lta_tag),
    normalized_serial_number=normalize_serial_number(observation.serial_number),
).single()
if row is None:
    continue
machine_unit_ids = [str(item) for item in row["machine_unit_ids"]]
if len(machine_unit_ids) != 1:
    continue
machine_unit_id = machine_unit_ids[0]
tx.run(
    queries.LINK_CHAT_SOURCE_RECORD_MENTIONS_EXISTING_UNIT,
    source_record_pk=source_record_pk,
    source_system_key=observation.source_system_key,
    source_record_id=observation.source_record_id,
    machine_unit_id=machine_unit_id,
    raw_context=observation.raw_context,
    observed_at=observation.observed_at,
    confidence=observation.confidence,
    quality_flag=observation.quality_flag.value,
)
```

Remove the chat use of `queries.UPSERT_MACHINE_UNIT`; do not remove sales use.

- [ ] **Step 6: Verify query tests pass**

Run:

```bash
uv run pytest services/ingestion/tests/test_machine_unit_queries.py -v
```

Expected: PASS.

---

### Task 4: Curate limited-100 sales dump files

**Files:**
- Modify: `.dumps/limited-100/fundbox_sales_100.sql`
- Modify: `.dumps/limited-100/eko_sales_100.sql`
- Modify: `.dumps/limited-100/speedzone_sales_100.sql`

- [ ] **Step 1: Generate curated limited dump files from full dumps**

Run this Python one-off from repo root. It uses the existing dump reader to select rows with usable machine-unit identifiers and writes limited SQL files that preserve the needed dependency rows.

```bash
uv run --package profile-unifier-ingestion python - <<'PY'
from __future__ import annotations

from pathlib import Path
from src.connectors.dumps.connectors import FUNDBOX_TABLES, PHPPOS_SALES_TABLES
from src.connectors.dumps.reader import DumpRow, load_dump_tables

ROOT = Path('.dumps')
LIMITED = ROOT / 'limited-100'
PLACEHOLDERS = {'', '-', '--', 'N/A', 'NA', 'NONE', 'NULL', 'UNKNOWN', 'NIL'}


def clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in PLACEHOLDERS:
        return None
    return text


def sql_value(value: object) -> str:
    if value is None:
        return 'NULL'
    if isinstance(value, int | float):
        return str(value)
    text = str(value).replace('\\', '\\\\').replace("'", "''")
    return f"'{text}'"


def insert_sql(table: str, rows: list[DumpRow]) -> str:
    if not rows:
        return ''
    columns = list(rows[0].as_dict().keys())
    values = []
    for row in rows:
        data = row.as_dict()
        values.append('(' + ','.join(sql_value(data.get(column)) for column in columns) + ')')
    return f"INSERT INTO `{table}` (`" + '`,`'.join(columns) + '`) VALUES\n' + ',\n'.join(values) + ';\n'


def write_fundbox() -> None:
    tables = load_dump_tables(ROOT / 'fundbox_2026-05-06.sql', FUNDBOX_TABLES)
    orders_by_id = {int(row.id): row for row in tables.rows('orders')}
    merchant_products_by_id = {int(row.id): row for row in tables.rows('merchant_products')}
    variants_by_id = {int(row.id): row for row in tables.rows('product_variants')}
    products_by_id = {int(row.id): row for row in tables.rows('products')}
    selected_items: list[DumpRow] = []
    selected_order_ids: set[int] = set()
    selected_mp_ids: set[int] = set()
    selected_variant_ids: set[int] = set()
    selected_product_ids: set[int] = set()
    for item in tables.rows('order_items'):
        if clean(item.lta_tag) is None and clean(item.serial_no) is None:
            continue
        order_id = int(item.order_id)
        mp_id = int(item.merchant_product_id)
        mp = merchant_products_by_id.get(mp_id)
        if order_id not in orders_by_id or mp is None:
            continue
        variant_id = int(mp.product_variant_id)
        variant = variants_by_id.get(variant_id)
        if variant is None:
            continue
        product = products_by_id.get(int(variant.product_id))
        if product is None:
            continue
        selected_items.append(item)
        selected_order_ids.add(order_id)
        selected_mp_ids.add(mp_id)
        selected_variant_ids.add(variant_id)
        selected_product_ids.add(int(variant.product_id))
        if len(selected_items) >= 100:
            break
    content = ''.join([
        insert_sql('orders', [orders_by_id[item] for item in sorted(selected_order_ids)]),
        insert_sql('order_items', selected_items),
        insert_sql('merchant_products', [merchant_products_by_id[item] for item in sorted(selected_mp_ids)]),
        insert_sql('product_variants', [variants_by_id[item] for item in sorted(selected_variant_ids)]),
        insert_sql('products', [products_by_id[item] for item in sorted(selected_product_ids)]),
    ])
    (LIMITED / 'fundbox_sales_100.sql').write_text(content, encoding='utf-8')


def write_phppos(full_name: str, limited_name: str) -> None:
    tables = load_dump_tables(ROOT / full_name, PHPPOS_SALES_TABLES)
    sales_by_id = {int(row.sale_id): row for row in tables.rows('phppos_sales')}
    items_by_id = {int(row.item_id): row for row in tables.rows('phppos_items')}
    selected_lines: list[DumpRow] = []
    selected_sale_ids: set[int] = set()
    selected_item_ids: set[int] = set()
    for line in tables.rows('phppos_sales_items'):
        if clean(line.serialnumber) is None:
            continue
        sale_id = int(line.sale_id)
        item_id = int(line.item_id)
        if sale_id not in sales_by_id or item_id not in items_by_id:
            continue
        selected_lines.append(line)
        selected_sale_ids.add(sale_id)
        selected_item_ids.add(item_id)
        if len(selected_lines) >= 100:
            break
    content = ''.join([
        insert_sql('phppos_sales', [sales_by_id[item] for item in sorted(selected_sale_ids)]),
        insert_sql('phppos_sales_items', selected_lines),
        insert_sql('phppos_items', [items_by_id[item] for item in sorted(selected_item_ids)]),
    ])
    (LIMITED / limited_name).write_text(content, encoding='utf-8')


write_fundbox()
write_phppos('eko_phppos_2026-05-06.sql', 'eko_sales_100.sql')
write_phppos('speedzone_phppos_2026-05-06.sql', 'speedzone_sales_100.sql')
PY
```

- [ ] **Step 2: Verify curated files produce observations**

Run:

```bash
docker compose exec -T worker python - <<'PY'
from __future__ import annotations
from pathlib import Path
from src.connectors.dumps.connectors import get_dump_connector
from src.machine_unit_extraction import observations_from_sales_lines

for source_key, dump_path in [
    ('fundbox_consumer_backend:sales', 'limited-100/fundbox_sales_100.sql'),
    ('eko_phppos:sales', 'limited-100/eko_sales_100.sql'),
    ('speedzone_phppos:sales', 'limited-100/speedzone_sales_100.sql'),
]:
    connector = get_dump_connector(source_key, Path('/app/dumps') / dump_path)
    observations = 0
    records = 0
    for record in connector.fetch_records():
        records += 1
        raw_payload = record.get('raw_payload')
        if not isinstance(raw_payload, dict):
            continue
        line_items = raw_payload.get('line_items')
        if not isinstance(line_items, list):
            continue
        observations += len(observations_from_sales_lines(
            source_system_key=source_key,
            source_record_id=str(record.get('source_record_id')),
            observed_at=str(record.get('observed_at')) if record.get('observed_at') else None,
            lines=line_items,
        ))
    print(f'{source_key}: records={records} observations={observations}')
PY
```

Expected: each sales source reports `observations` greater than 0.

---

### Task 5: Run focused tests and type checks

**Files:**
- Verify only.

- [ ] **Step 1: Format changed Python files**

Run:

```bash
uv run --package profile-unifier-ingestion ruff format services/ingestion/src/connectors/dumps/connectors.py services/ingestion/src/machine_unit_extraction.py services/ingestion/src/graph/queries/machine_units.py services/ingestion/src/graph/queries/__init__.py services/ingestion/src/pipeline.py services/ingestion/tests/test_dump_connectors.py services/ingestion/tests/test_machine_unit_extraction.py services/ingestion/tests/test_machine_unit_queries.py
```

Expected: exits 0.

- [ ] **Step 2: Lint changed Python files**

Run:

```bash
uv run --package profile-unifier-ingestion ruff check services/ingestion/src/connectors/dumps/connectors.py services/ingestion/src/machine_unit_extraction.py services/ingestion/src/graph/queries/machine_units.py services/ingestion/src/graph/queries/__init__.py services/ingestion/src/pipeline.py services/ingestion/tests/test_dump_connectors.py services/ingestion/tests/test_machine_unit_extraction.py services/ingestion/tests/test_machine_unit_queries.py
```

Expected: PASS.

- [ ] **Step 3: Run focused tests**

Run:

```bash
uv run pytest services/ingestion/tests/test_dump_connectors.py services/ingestion/tests/test_machine_unit_extraction.py services/ingestion/tests/test_machine_unit_queries.py -v
```

Expected: PASS.

- [ ] **Step 4: Run strict mypy**

Run:

```bash
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src
```

Expected: PASS.

---

### Task 6: End-to-end reset and limited-100 ingestion verification

**Files:**
- Runtime verification only.

- [ ] **Step 1: Rebuild changed services**

Run:

```bash
docker compose build --no-cache api frontend worker beat
```

Expected: PASS.

- [ ] **Step 2: Reset containers and graph data if the user still wants a fresh run**

Run only if the user still wants the same fresh-run behavior:

```bash
docker compose down --remove-orphans && rm -rf data/neo4j/data data/neo4j/logs data/redis && mkdir -p data/neo4j/data data/neo4j/logs data/redis && docker compose up -d
```

Expected: containers start; Neo4j and Redis healthy.

- [ ] **Step 3: Dispatch limited-100 dump ingestions through Celery**

Use the same `run_ingestion_task.delay(..., mode='dump', dump_path='limited-100/...')` pattern used in the previous run.

- [ ] **Step 4: Verify MachineUnit creation**

Run:

```bash
docker compose exec -T neo4j cypher-shell -u neo4j -p hyperP_dev_2026 "MATCH (u:MachineUnit) RETURN count(u) AS machine_units; MATCH ()-[r:INVOLVES_UNIT]->() RETURN count(r) AS involves_unit; MATCH ()-[r:BOUGHT_UNIT]->() RETURN count(r) AS bought_unit; MATCH ()-[r:MENTIONS_UNIT]->() RETURN count(r) AS mentions_unit;"
```

Expected: `machine_units`, `involves_unit`, and sales-side unit relationships are greater than 0. `MENTIONS_UNIT` may be 0 if limited chat data contains no matchable LTA/serial evidence.

---

## Self-Review

- Spec coverage: Fundbox product lookup is Task 1; PHPPOS serial payload and extraction are Task 2; limited-100 dump curation is Task 4; chat link-only behavior is Task 3; focused and end-to-end verification are Tasks 5 and 6.
- Placeholder scan: no TBD/TODO/fill-in-later placeholders remain.
- Type consistency: `RESOLVE_EXISTING_MACHINE_UNIT_FOR_CHAT`, `LINK_CHAT_SOURCE_RECORD_MENTIONS_EXISTING_UNIT`, `metadata.serialnumber`, and `product_variant_id` are named consistently across tasks.
- Scope check: chat participant-phone/order-invoice Person→MachineUnit confirmation is intentionally limited to link-only evidence in this plan. It does not create chat-origin ownership edges unless an unambiguous existing unit is found; deeper invoice/person disambiguation can be a follow-up if limited chat data actually contains those fields.
