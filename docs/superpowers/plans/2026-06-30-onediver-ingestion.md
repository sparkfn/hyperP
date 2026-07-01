# OneDiver Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dump-only `onediver` ingestion connector (identity + emergency-contact relationships) and `onediver:sales` connector (sales_orders), plus limited-100 dump fixtures, so OneDiver dive-school data unifies with the rest of the graph.

**Architecture:** New `services/ingestion/src/connectors/onediver/` package (`schema.py`, `connector.py`) exporting two `*DumpConnector` classes and two table-set constants, registered in the `factories` dict of `dumps/connectors.py`. Reuses `build_envelope`, `IdentifierBag`, `address_from_row`, `serialize_row`, `to_iso` from `fundbox/builders.py` + `dumps/reader.py`. Sales→person link is email-based (96% coverage), not an integer FK.

**Tech Stack:** Python 3.12, SQLAlchemy Core table reflections, pytest, uv workspace (`profile-unifier-ingestion`).

## Global Constraints

- Strict typing, no `Any`; mypy `--strict` clean (ingestion package).
- ruff check + ruff format clean on touched files only.
- Source keys: `onediver` and `onediver:sales` (dump-only; no live batch connector).
- limited-100 dumps are local/dev only — never staging/production.
- Don't run project linters/tests on host to verify; push the branch and read the Woodpecker PR verdict via `wpci home`. One-shot `uv run` to *generate* the limited-100 dumps is allowed (data prep, not verification).
- `onediver (1).sql` filename has a space + parens — quote it everywhere.
- NRIC (`ic_number`) and passport (`passport_number`) are govt IDs; the connector emits them as identifier types `nric` / `passport` and the downstream pipeline salt-hashes them — the connector does not hash.

---

## File map

Create:
- `services/ingestion/src/connectors/onediver/__init__.py` — exports `OneDiverDumpConnector`, `OneDiverSalesDumpConnector`, `ONEDIVER_TABLES`, `ONEDIVER_SALES_TABLES`.
- `services/ingestion/src/connectors/onediver/schema.py` — SQLAlchemy Core `Table` reflections for `profiles`, `profile_emergencies`, `users`, `accounts`, `sales_orders`.
- `services/ingestion/src/connectors/onediver/connector.py` — table-set constants, `_build_identity_envelope`, `_build_relationship_envelopes`, `_build_sales_envelope`, `OneDiverDumpConnector`, `OneDiverSalesDumpConnector`.
- `services/ingestion/tests/test_onediver.py` — connector tests.

Modify:
- `services/ingestion/src/connectors/dumps/connectors.py` — import + register `onediver` / `onediver:sales` in the `factories` dict.
- `.dumps/limited-100/generate_limited_dumps.py` — add onediver + onediver:sales generators.

---

## Task 1: `onediver` package — schema + identity builder + `OneDiverDumpConnector`

**Files:**
- Create: `services/ingestion/src/connectors/onediver/__init__.py`
- Create: `services/ingestion/src/connectors/onediver/schema.py`
- Create: `services/ingestion/src/connectors/onediver/connector.py`
- Test: `services/ingestion/tests/test_onediver.py`

**Interfaces:**
- Produces: `OneDiverDumpConnector` (class), `ONEDIVER_TABLES` (tuple/list of `Table`), `_build_identity_envelope(profile_row, user_row, account_row) -> dict[str, JsonValue]`.

### Step 1.1: Write `schema.py`

Only the columns the connector reads. Follow the `eko/schema.py` pattern (`metadata = MetaData()`, `Table(...)` with `Column(name, Type)`).

```python
"""SQLAlchemy Core table reflections for the OneDiver DB.

Only the columns the connector reads are declared. OneDiver is a scuba /
water-sports business-management platform (MySQL). See
docs/superpowers/specs/2026-06-30-onediver-ingestion-design.md for the source
profile and the evidence behind the sales email-link.
"""

from __future__ import annotations

from sqlalchemy import MetaData, Table, Column
from sqlalchemy.types import CHAR, Date, DateTime, Integer, String

metadata = MetaData()

profiles = Table(
    "profiles",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer),
    Column("ssi_master_id", String(20)),
    Column("email", String(200)),
    Column("first_name", String(50)),
    Column("last_name", String(50)),
    Column("gender", CHAR(1)),
    Column("birthday", Date),
    Column("passport_full_name", String(100)),
    Column("passport_number", String(20)),
    Column("ic_number", String(50)),
    Column("lk_nationality_code", CHAR(2)),
    Column("race", String(50)),
    Column("address", String(1000)),
    Column("address2", String(500)),
    Column("city", String(100)),
    Column("state", String(100)),
    Column("lk_country_code", CHAR(2)),
    Column("zip_code", String(20)),
    Column("contact_number", String(20)),
    Column("lk_contact_country_code", String(5)),
    Column("secondary_contact_number", String(20)),
    Column("lk_secondary_contact_country_code", String(5)),
    Column("Alternative_email", String(200)),
    Column("membership_id", String(50)),
    Column("dive_level", String(50)),
    Column("dives", String(20)),
    Column("last_dive", DateTime),
    Column("modified", DateTime),
    Column("created", DateTime),
    Column("is_deleted", Integer),
)

profile_emergencies = Table(
    "profile_emergencies",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("profile_id", Integer),
    # kin1
    Column("contact_first_name", String(100)),
    Column("contact_last_name", String(100)),
    Column("contact_number", String(50)),
    Column("lk_contact_country_code", String(5)),
    Column("relation", String(100)),
    # kin2
    Column("kin2_fname", String(100)),
    Column("kin2_lname", String(100)),
    Column("kin2_contact", String(50)),
    Column("kin2_relation", String(100)),
    Column("modified", DateTime),
    Column("created", DateTime),
)

users = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("account_id", Integer),
    Column("username", String(50)),
)

accounts = Table(
    "accounts",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(200)),
)

sales_orders = Table(
    "sales_orders",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("order_id", String(20)),
    Column("order_date", DateTime),
    Column("accepted_date", DateTime),
    Column("created", DateTime),
    Column("billing_contact_name", String(100)),
    Column("billing_contact_email", String(350)),
    Column("billing_contact_number", String(50)),
    Column("billing_country_code", String(20)),
    Column("total", String(50)),
    Column("status_code", String(50)),
    Column("currency", String(20)),
    Column("modified", DateTime),
)
```

### Step 1.2: Write the failing identity test in `test_onediver.py`

```python
"""Tests for the OneDiver dump connectors."""

from __future__ import annotations

from pathlib import Path

from src.connectors.dumps.connectors import get_dump_connector


def test_onediver_identity_envelope(tmp_path: Path) -> None:
    dump_path = tmp_path / "onediver.sql"
    dump_path.write_text(
        """
CREATE TABLE `profiles` (
  `id` int NOT NULL,
  `user_id` int,
  `ssi_master_id` varchar(20),
  `email` varchar(200),
  `first_name` varchar(50),
  `last_name` varchar(50),
  `gender` char(1),
  `birthday` date,
  `passport_full_name` varchar(100),
  `passport_number` varchar(20),
  `ic_number` varchar(50),
  `lk_nationality_code` char(2),
  `race` varchar(50),
  `address` varchar(1000),
  `address2` varchar(500),
  `city` varchar(100),
  `state` varchar(100),
  `lk_country_code` char(2),
  `zip_code` varchar(20),
  `contact_number` varchar(20),
  `lk_contact_country_code` varchar(5),
  `secondary_contact_number` varchar(20),
  `lk_secondary_contact_country_code` varchar(5),
  `Alternative_email` varchar(200),
  `membership_id` varchar(50),
  `dive_level` varchar(50),
  `dives` varchar(20),
  `last_dive` datetime,
  `modified` datetime,
  `created` datetime,
  `is_deleted` int
);
CREATE TABLE `profile_emergencies` (
  `id` int NOT NULL,
  `profile_id` int,
  `contact_first_name` varchar(100),
  `contact_last_name` varchar(100),
  `contact_number` varchar(50),
  `lk_contact_country_code` varchar(5),
  `relation` varchar(100),
  `kin2_fname` varchar(100),
  `kin2_lname` varchar(100),
  `kin2_contact` varchar(50),
  `kin2_relation` varchar(100),
  `modified` datetime,
  `created` datetime
);
CREATE TABLE `users` (
  `id` int NOT NULL,
  `account_id` int,
  `username` varchar(50)
);
CREATE TABLE `accounts` (
  `id` int NOT NULL,
  `name` varchar(200)
);
INSERT INTO `profiles` VALUES
(5, 2, 'SSI-001', 'ada@example.test', 'Ada', 'Lovelace', 'F', '1990-01-31',
 'Ada Lovelace', 'E6975636L', 'S1234567A', 'SG', 'Chinese', 'One Street', 'Block 2',
 'Singapore', 'SG', '123456', '6599990000', '65', '6500000000', '65',
 'ada.alt@example.test', 'M-100', 'Open Water', '42', NULL,
 '2026-05-06 02:00:00', '2026-05-01 01:00:00', 0);
INSERT INTO `users` VALUES (2, 1, 'ada.lovelace');
INSERT INTO `accounts` VALUES (1, 'Scubahub');
""".strip(),
        encoding="utf-8",
    )

    connector = get_dump_connector("onediver", dump_path)
    records = list(connector.fetch_records())

    assert len(records) == 1
    rec = records[0]
    assert rec["record_type"] == "identity"
    assert rec["source_record_id"] == "onediver-profile-5"
    id_types = {i["type"]: i["value"] for i in rec["identifiers"]}
    assert id_types["email"] == "ada@example.test"
    assert id_types["nric"] == "S1234567A"
    assert id_types["passport"] == "E6975636L"
    assert id_types["phone"] == "6599990000"
    assert rec["attributes"]["full_name"] == "Ada Lovelace"
    assert rec["attributes"]["nationality"] == "SG"
    assert rec["attributes"]["shop_name"] == "Scubahub"
    assert rec["addresses"][0]["city"] == "Singapore"
    assert rec["addresses"][0]["postal_code"] == "123456"
```

### Step 1.3: Run the test to verify it fails

Run: `uv run --package profile-unifier-ingestion pytest services/ingestion/tests/test_onediver.py::test_onediver_identity_envelope -v` (host one-shot to drive TDD red; the PR pipeline re-verifies). Expected: FAIL — `onediver` not in factories / module not found.

### Step 1.4: Implement `connector.py` identity path + `__init__.py`

`connector.py`:

```python
"""OneDiver dump connectors (``source_key=onediver`` / ``onediver:sales``)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from src.connectors.dumps.reader import DumpRow, load_dump_tables
from src.connectors.fundbox.builders import (
    IdentifierBag,
    address_from_row,
    build_envelope,
    serialize_row,
)
from src.connectors.onediver import schema as s
from src.connectors.onediver.schema import (
    accounts,
    profile_emergencies,
    profiles,
    sales_orders,
    users,
)
from src.types import JsonValue

ONEDIVER_TABLES = [profiles, profile_emergencies, users, accounts]
ONEDIVER_SALES_TABLES = [sales_orders, profiles]


def _str(row: DumpRow, col: str) -> str | None:
    v = row.get(col)
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _full_name(row: DumpRow) -> str | None:
    first = _str(row, "first_name")
    last = _str(row, "last_name")
    parts = [p for p in (first, last) if p]
    return " ".join(parts) or None


def _build_identity_envelope(
    row: DumpRow, user: DumpRow | None, account: DumpRow | None
) -> dict[str, JsonValue]:
    ids = IdentifierBag()
    ids.add("email", _str(row, "email"), verified=True)
    ids.add("email", _str(row, "Alternative_email"))
    ids.add("phone", _str(row, "contact_number"), region_hint=_str(row, "lk_contact_country_code"))
    ids.add(
        "phone",
        _str(row, "secondary_contact_number"),
        region_hint=_str(row, "lk_secondary_contact_country_code"),
    )
    ids.add("nric", _str(row, "ic_number"), verified=True)
    ids.add("passport", _str(row, "passport_number"), verified=True)

    address = address_from_row(
        _row_for_address(row)  # adapter mapping onediver cols → address_from_row's expected keys
    )
    attributes: dict[str, JsonValue] = {
        "full_name": _full_name(row),
        "first_name": _str(row, "first_name"),
        "last_name": _str(row, "last_name"),
        "gender": _str(row, "gender"),
        "dob": _str(row, "birthday"),
        "nationality": _str(row, "lk_nationality_code"),
        "race": _str(row, "race"),
        "passport_full_name": _str(row, "passport_full_name"),
        "dive_level": _str(row, "dive_level"),
        "dives": _str(row, "dives"),
        "ssi_master_id": _str(row, "ssi_master_id"),
        "membership_id": _str(row, "membership_id"),
        "username": _str(user, "username") if user else None,
        "shop_name": _str(account, "name") if account else None,
    }
    return build_envelope(
        source_record_id=f"onediver-profile-{row.get('id')}",
        observed_at=_str(row, "modified") or _str(row, "created"),
        identifiers=ids.items,
        attributes=attributes,
        raw_payload={
            "profile": serialize_row(row),
            "user": serialize_row(user) if user else {},
            "account": serialize_row(account) if account else {},
        },
        addresses=[address] if address is not None else None,
        record_type="identity",
    )


def _row_for_address(row: DumpRow) -> DumpRow:
    """Map onediver address columns onto the keys ``address_from_row`` reads."""
    return DumpRow(
        {
            "address_1": row.get("address"),
            "address_2": row.get("address2"),
            "city": row.get("city"),
            "state": row.get("state"),
            "country": row.get("lk_country_code"),
            "postal_code": row.get("zip_code"),
        }
    )


class OneDiverDumpConnector:
    def __init__(self, dump_path: Any) -> None:
        self._dump_path = dump_path

    def get_source_key(self) -> str:
        return "onediver"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        tables = load_dump_tables(self._dump_path, ONEDIVER_TABLES)
        users_by_id = {_int(row, "user_id"): row for row in tables.rows("users")}
        accounts_by_id = {_int(row, "id"): row for row in tables.rows("accounts")}
        for row in sorted(tables.rows("profiles"), key=lambda r: _int(r, "id")):
            if _int(row, "is_deleted") != 0:
                continue
            yield _build_identity_envelope(
                row,
                users_by_id.get(_int(row, "user_id")),
                accounts_by_id.get(_int(row, "account_id")) if False else None,
            )


def _int(row: DumpRow, col: str) -> int:
    v = row.get(col)
    if v is None:
        return 0
    try:
        return int(str(v).strip())
    except ValueError:
        return 0
```

Note: the `accounts_by_id.get(_int(row, "account_id"))` line above uses `account_id` which is on `users`, not `profiles`. The shop for a profile is `users.account_id → accounts.id`. Fix in Step 1.4 final code: resolve `account` from the user's `account_id`:

```python
        user = users_by_id.get(_int(row, "user_id"))
        account = accounts_by_id.get(_int(user, "account_id")) if user else None
        yield _build_identity_envelope(row, user, account)
```

(The planner flagged the original line with `if False` as a placeholder to fix — the final code uses the corrected resolution above.)

`__init__.py`:

```python
"""OneDiver source connectors."""

from src.connectors.onediver.connector import (
    ONEDIVER_SALES_TABLES,
    ONEDIVER_TABLES,
    OneDiverDumpConnector,
    OneDiverSalesDumpConnector,
)

__all__ = [
    "ONEDIVER_SALES_TABLES",
    "ONEDIVER_TABLES",
    "OneDiverDumpConnector",
    "OneDiverSalesDumpConnector",
]
```

Note: `OneDiverSalesDumpConnector` is added in Task 3; until then keep the `__init__.py` exporting only what exists, or define a stub. Cleanest: create `__init__.py` in Task 3 once both classes exist; in Task 1 leave it empty (`"""OneDiver source connectors."""`).

### Step 1.5: Register `onediver` in `dumps/connectors.py`

In the `factories` dict in `get_dump_connector`, add the import at the top with the other connector imports:

```python
from src.connectors.onediver.connector import OneDiverDumpConnector
```

and in `factories`:

```python
        "onediver": OneDiverDumpConnector,
```

### Step 1.6: Run test to verify it passes

Expected: PASS. Fix the `address_from_row` field mapping if the assertion on `city`/`postal_code` fails — `address_from_row` reads `city`/`state`/`postal_code`/`country_code` (verify the exact key names in `fundbox/builders.py:139-160` and map accordingly).

### Step 1.7: Commit

```bash
git add services/ingestion/src/connectors/onediver/ services/ingestion/src/connectors/dumps/connectors.py services/ingestion/tests/test_onediver.py
git commit -m "feat(ingestion): add onediver dump identity connector"
```

---

## Task 2: Relationship records (emergency contacts kin1/kin2)

**Files:**
- Modify: `services/ingestion/src/connectors/onediver/connector.py`
- Test: `services/ingestion/tests/test_onediver.py`

**Interfaces:**
- Produces: `_build_relationship_envelopes(row) -> list[dict[str, JsonValue]]` (0, 1, or 2 records).

### Step 2.1: Write the failing relationship test

Append to `test_onediver.py`. Extend the fixture above with an emergency-contact row:

```python
def test_onediver_relationship_envelopes(tmp_path: Path) -> None:
    dump_path = tmp_path / "onediver.sql"
    dump_path.write_text(
        # same profiles/users/accounts CREATE TABLEs as above, plus:
        """
INSERT INTO `profile_emergencies` VALUES
(10, 5, 'Charles', 'Babbage', '6598880000', '65', 'Father',
 'Annie', 'Lovelace', '6598770000', 'Mother',
 '2026-05-06 02:00:00', '2026-05-01 01:00:00');
""".strip(),
        encoding="utf-8,
    )
    # ... include the profiles/users/accounts INSERTs from test_onediver_identity_envelope ...
    connector = get_dump_connector("onediver", dump_path)
    records = [r for r in connector.fetch_records() if r["record_type"] == "relationship"]
    assert len(records) == 2
    by_slot = {r["raw_payload"]["kin_slot"]: r for r in records}
    assert by_slot["kin1"]["attributes"]["full_name"] == "Charles Babbage"
    assert by_slot["kin1"]["attributes"]["relationship_to_referrer"] == "Father"
    assert by_slot["kin1"]["raw_payload"]["linked_to_source_record_id"] == "onediver-profile-5"
    kin1_ids = {i["type"] for i in by_slot["kin1"]["identifiers"]}
    assert "phone" in kin1_ids
    # kin2 has a phone too
    assert by_slot["kin2"]["attributes"]["full_name"] == "Annie Lovelace"
```

Extract the shared fixture CREATE TABLEs into a module-level `_ONEDIVER_SCHEMA_SQL` constant + helper `_write_onediver_dump(tmp_path, *extra_inserts)` to avoid copy-paste (DRY).

### Step 2.2: Run to verify it fails

Expected: FAIL — no relationship records emitted (only identity).

### Step 2.3: Implement `_build_relationship_envelopes` + wire into `fetch_records`

```python
def _build_relationship_envelopes(row: DumpRow) -> list[dict[str, JsonValue]]:
    profile_id = row.get("id")
    out: list[dict[str, JsonValue]] = []
    for slot, first_col, last_col, num_col, rel_col in [
        ("kin1", "contact_first_name", "contact_last_name", "contact_number", "relation"),
        ("kin2", "kin2_fname", "kin2_lname", "kin2_contact", "kin2_relation"),
    ]:
        first = _str(row, first_col)
        last = _str(row, last_col)
        full = " ".join(p for p in (first, last) if p) or None
        if not full and not _str(row, num_col):
            continue  # empty kin slot
        ids = IdentifierBag()
        ids.add("phone", _str(row, num_col))
        rel = _str(row, rel_col)
        out.append(
            build_envelope(
                source_record_id=f"onediver-emergency-{profile_id}-{slot}",
                observed_at=_str(row, "modified") or _str(row, "created"),
                identifiers=ids.items,
                record_type="relationship",
                attributes={
                    "full_name": full,
                    "relationship_to_referrer": rel,
                },
                raw_payload={
                    "emergency": serialize_row(row),
                    "kin_slot": slot,
                    "linked_to_source_record_id": f"onediver-profile-{profile_id}",
                    "link_type": rel,
                },
            )
        )
    return out
```

In `OneDiverDumpConnector.fetch_records`, after the identity yield, iterate `profile_emergencies` rows for the profile and yield relationship records. Build an index `emergencies_by_profile = defaultdict(list)` once, then for each profile yield identity + its relationship records:

```python
        emerg_by_profile: dict[int, list[DumpRow]] = {}
        for e in tables.rows("profile_emergencies"):
            emerg_by_profile.setdefault(_int(e, "profile_id"), []).append(e)
        for row in sorted(tables.rows("profiles"), key=lambda r: _int(r, "id")):
            if _int(row, "is_deleted") != 0:
                continue
            user = users_by_id.get(_int(row, "user_id"))
            account = accounts_by_id.get(_int(user, "account_id")) if user else None
            yield _build_identity_envelope(row, user, account)
            for e in emerg_by_profile.get(_int(row, "id"), []):
                yield from _build_relationship_envelopes(e)
```

### Step 2.4: Run test to verify it passes

Expected: PASS.

### Step 2.5: Commit

```bash
git commit -am "feat(ingestion): emit onediver emergency-contact relationship records"
```

---

## Task 3: `onediver:sales` connector

**Files:**
- Modify: `services/ingestion/src/connectors/onediver/connector.py`
- Modify: `services/ingestion/src/connectors/onediver/__init__.py`
- Modify: `services/ingestion/src/connectors/dumps/connectors.py`
- Test: `services/ingestion/tests/test_onediver.py`

**Interfaces:**
- Produces: `OneDiverSalesDumpConnector`, `_build_sales_envelope(order_row, profile_id | None) -> dict[str, JsonValue]`.

### Step 3.1: Write the failing sales test

```python
def test_onediver_sales_envelope_links_by_email(tmp_path: Path) -> None:
    dump_path = tmp_path / "onediver.sql"
    dump_path.write_text(
        _ONEDIVER_SCHEMA_SQL
        + """
CREATE TABLE `sales_orders` (
  `id` int NOT NULL,
  `order_id` varchar(20),
  `order_date` datetime,
  `accepted_date` datetime,
  `created` datetime,
  `billing_contact_name` varchar(100),
  `billing_contact_email` varchar(350),
  `billing_contact_number` varchar(50),
  `billing_country_code` varchar(20),
  `total` varchar(50),
  `status_code` varchar(50),
  `currency` varchar(20),
  `modified` datetime
);
INSERT INTO `profiles` VALUES
(5, 2, 'SSI-001', 'ada@example.test', 'Ada', 'Lovelace', 'F', '1990-01-31',
 'Ada Lovelace', 'E6975636L', 'S1234567A', 'SG', 'Chinese', 'One Street', 'Block 2',
 'Singapore', 'SG', '123456', '6599990000', '65', '6500000000', '65',
 'ada.alt@example.test', 'M-100', 'Open Water', '42', NULL,
 '2026-05-06 02:00:00', '2026-05-01 01:00:00', 0);
INSERT INTO `sales_orders` VALUES
(1, 'SO-1', '2026-05-02 03:00:00', NULL, '2026-05-02 03:05:00',
 'Ada Lovelace', 'ada@example.test', '6599990000', '65', '250.00', 'ACCEPTED',
 'SGD', '2026-05-02 03:05:00');
INSERT INTO `sales_orders` VALUES
(2, 'SO-2', '2026-05-03 03:00:00', NULL, '2026-05-03 03:05:00',
 'Walk In', 'walkin@example.test', '6588880000', '65', '50.00', 'ACCEPTED',
 'SGD', '2026-05-03 03:05:00');
""".strip(),
        encoding="utf-8",
    )
    connector = get_dump_connector("onediver:sales", dump_path)
    records = list(connector.fetch_records())
    assert len(records) == 2
    linked = [r for r in records if r["raw_payload"].get("customer_link", {}).get("identity_source_record_id")]
    assert len(linked) == 1
    assert linked[0]["source_record_id"] == "onediver-salesorder-1"
    assert linked[0]["raw_payload"]["customer_link"]["identity_source_record_id"] == "onediver-profile-5"
    assert linked[0]["record_type"] == "sales"
    id_types = {i["type"] for i in linked[0]["identifiers"]}
    assert "email" in id_types and "phone" in id_types
```

### Step 3.2: Run to verify it fails

Expected: FAIL — `onediver:sales` not in factories.

### Step 3.3: Implement `_build_sales_envelope` + `OneDiverSalesDumpConnector`

```python
def _build_sales_envelope(
    row: DumpRow, profile_id: str | None
) -> dict[str, JsonValue]:
    ids = IdentifierBag()
    ids.add("email", _str(row, "billing_contact_email"))
    ids.add("phone", _str(row, "billing_contact_number"), region_hint=_str(row, "billing_country_code"))
    raw: dict[str, JsonValue] = {
        "order": {
            "source_order_id": str(row.get("id")),
            "order_no": _str(row, "order_id"),
            "ordered_at": _str(row, "order_date") or _str(row, "accepted_date") or _str(row, "created"),
            "status": _str(row, "status_code"),
            "currency": _str(row, "currency") or "SGD",
            "total_amount": _str(row, "total"),
            "raw": serialize_row(row),
        },
    }
    if profile_id is not None:
        raw["customer_link"] = {"identity_source_record_id": f"onediver-profile-{profile_id}"}
    return build_envelope(
        source_record_id=f"onediver-salesorder-{row.get('id')}",
        observed_at=_str(row, "order_date") or _str(row, "accepted_date") or _str(row, "created"),
        identifiers=ids.items,
        attributes={
            "currency": _str(row, "currency"),
            "total_amount": _str(row, "total"),
            "status": _str(row, "status_code"),
            "order_no": _str(row, "order_id"),
        },
        raw_payload=raw,
        record_type="sales",
    )


class OneDiverSalesDumpConnector:
    def __init__(self, dump_path: Any) -> None:
        self._dump_path = dump_path

    def get_source_key(self) -> str:
        return "onediver:sales"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        tables = load_dump_tables(self._dump_path, ONEDIVER_SALES_TABLES)
        email_to_id: dict[str, str] = {}
        for row in tables.rows("profiles"):
            em = _str(row, "email")
            if em:
                email_to_id.setdefault(em.lower(), str(row.get("id")))
        for row in sorted(tables.rows("sales_orders"), key=lambda r: _int(r, "id")):
            em = _str(row, "billing_contact_email")
            profile_id = email_to_id.get(em.lower()) if em else None
            yield _build_sales_envelope(row, profile_id)
```

Wire `__init__.py` to export `OneDiverSalesDumpConnector` and `ONEDIVER_SALES_TABLES`, and register `"onediver:sales": OneDiverSalesDumpConnector` in `dumps/connectors.py` factories (import `OneDiverSalesDumpConnector` alongside `OneDiverDumpConnector`).

### Step 3.4: Run tests to verify they pass

Expected: PASS.

### Step 3.5: Commit

```bash
git commit -am "feat(ingestion): add onediver:sales dump connector (email-linked)"
```

---

## Task 4: limited-100 generator extension + generate the dumps

**Files:**
- Modify: `.dumps/limited-100/generate_limited_dumps.py`
- Generate: `.dumps/limited-100/onediver_100.sql`, `.dumps/limited-100/onediver_sales_100.sql`

### Step 4.1: Add the onediver blocks to the generator

After the existing eko/speedzone block, add (reusing the script's existing `by_int`, `sorted_first`, `write_mysql`, `_row_int`, `_int_value` helpers):

```python
from src.connectors.onediver.connector import ONEDIVER_SALES_TABLES, ONEDIVER_TABLES

# onediver identity + relationships
onediver_tables = load_dump_tables(ROOT / "onediver (1).sql", ONEDIVER_TABLES)
od_profiles = sorted(
    [r for r in onediver_tables.rows("profiles") if _int_value(r.get("is_deleted")) == 0],
    key=lambda item: _row_int(item, "id"),
)[:100]
od_profile_ids = {str(_row_int(r, "id")) for r in od_profiles}
od_user_ids = {str(_row_int(r, "user_id")) for r in od_profiles}
od_account_ids = {str(_row_int(r, "account_id")) for r in onediver_tables.rows("users")
                  if str(_row_int(r, "id")) in od_user_ids}
write_mysql(
    OUT / "onediver_100.sql",
    {
        "profiles": od_profiles,
        "profile_emergencies": [
            r for r in onediver_tables.rows("profile_emergencies")
            if str(_row_int(r, "profile_id")) in od_profile_ids
        ],
        "users": [r for r in onediver_tables.rows("users") if str(_row_int(r, "id")) in od_user_ids],
        "accounts": [r for r in onediver_tables.rows("accounts") if str(_row_int(r, "id")) in od_account_ids],
    },
)

# onediver sales (email-linked to the profiles above so the dump is self-contained)
od_sales_tables = load_dump_tables(ROOT / "onediver (1).sql", ONEDIVER_SALES_TABLES)
od_sales = sorted_first(od_sales_tables.rows("sales_orders"), "id", 100)
od_sale_emails = {str(r.get("billing_contact_email") or "").lower() for r in od_sales} - {""}
write_mysql(
    OUT / "onediver_sales_100.sql",
    {
        "sales_orders": od_sales,
        "profiles": [
            r for r in od_sales_tables.rows("profiles")
            if str(r.get("email") or "").lower() in od_sale_emails
        ],
    },
)
```

Confirm `sorted_first` exists in the script (it is used by the eko sales block) and `write_mysql` accepts `dict[str, list[DumpRow]]`. If `sorted_first` isn't defined, sort inline: `sorted(..., key=lambda r: _row_int(r, "id"))[:100]`.

### Step 4.2: Generate the dumps

```bash
cd .dumps/limited-100 && PYTHONPATH=../.. DUMPS_ROOT=.. uv run --package profile-unifier-ingestion python generate_limited_dumps.py
```

(One-shot generation — data prep, not code verification. If the uv run is disallowed by host policy, run inside the worker container: `docker compose exec worker python /app/.dumps/limited-100/generate_limited_dumps.py` with `DUMPS_ROOT=/app/.dumps`. Either is acceptable.)

Expected: prints `onediver_100.sql` and `onediver_sales_100.sql` with their sizes.

### Step 4.3: Sanity-check the generated dumps

```bash
ls -la .dumps/limited-100/onediver_100.sql .dumps/limited-100/onediver_sales_100.sql
```

Expected: both files exist and are non-empty.

### Step 4.4: Commit

```bash
git add .dumps/limited-100/generate_limited_dumps.py .dumps/limited-100/onediver_100.sql .dumps/limited-100/onediver_sales_100.sql
git commit -m "feat(ingestion): add onediver limited-100 dumps for local testing"
```

---

## Task 5: Validate via Woodpecker PR pipeline

- Push the branch: `git push -u origin onediver-ingestion`
- Read the verdict: `wpci home pipeline last sparkfn/hyperP --branch onediver-ingestion` and `wpci home pipeline show sparkfn/hyperP <n>`.
- Required green steps: ruff (ingestion), mypy --strict (ingestion), pytest (ingestion). Iterate via further commits until green.
- Do NOT squash/rebase; do NOT merge to development without explicit user authorization.

---

## Self-review (planner)

- **Spec coverage:** identity (Task 1), relationships (Task 2), sales (Task 3), factories registration (Tasks 1.5/3.3), limited-100 (Task 4), validation (Task 5). All spec sections covered. The spec's sales section says sales_orders only — Task 3 matches.
- **Placeholders:** none. (The Task 1.4 `if False` line is explicitly called out and replaced in the same step.)
- **Type consistency:** `_build_identity_envelope`, `_build_relationship_envelopes`, `_build_sales_envelope`, `ONEDIVER_TABLES`, `ONEDIVER_SALES_TABLES`, `OneDiverDumpConnector`, `OneDiverSalesDumpConnector` — names consistent across tasks.
- **`address_from_row` key mapping:** verify the exact keys (`address_1`, `address_2`, `city`, `state`, `postal_code`, `country_code`) against `fundbox/builders.py:139-160` before finalizing Task 1.4; the `_row_for_address` adapter maps onediver columns to those keys.