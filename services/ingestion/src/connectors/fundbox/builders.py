"""Reusable builders for source-record envelopes.

Pure helpers — no DB or Neo4j dependencies — so they can be unit-tested in
isolation and reused by every Fundbox connector.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any

from src.connectors.fundbox.junk import is_junk_identifier, should_filter


def _json_default(value: object) -> str:
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def compute_hash(record: dict) -> str:
    payload = json.dumps(record, sort_keys=True, default=_json_default)
    return f"sha256:{hashlib.sha256(payload.encode()).hexdigest()[:16]}"


def to_iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat() + ("Z" if value.tzinfo is None else "")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def serialize_row(row: Any) -> dict:
    """Convert a SQLAlchemy Row mapping (or dict) to JSON-safe primitives."""
    if hasattr(row, "_mapping"):
        items = row._mapping.items()
    else:
        items = row.items()
    return {k: to_iso(v) for k, v in items}


def format_address(row: Any) -> str | None:
    """Format an address row into a single normalized string."""
    if row is None:
        return None
    m = row._mapping if hasattr(row, "_mapping") else row
    parts = [
        m.get("address_line_1"),
        m.get("address_line_2"),
        m.get("street"),
        m.get("building"),
        " ".join(p for p in (m.get("block"), m.get("floor"), m.get("unit")) if p) or None,
        m.get("city"),
        m.get("postal_code"),
        m.get("country"),
    ]
    cleaned = [str(p).strip() for p in parts if p]
    return ", ".join(cleaned) if cleaned else None


class IdentifierBag:
    """Collects identifiers, deduping by (type, value) and filtering junk."""

    __slots__ = ("items", "_seen")

    def __init__(self) -> None:
        self.items: list[dict] = []
        self._seen: set[tuple[str, str]] = set()

    def add(
        self,
        id_type: str,
        value: object,
        *,
        verified: bool = False,
        last_confirmed_at: str | None = None,
    ) -> None:
        if value is None:
            return
        value_str = str(value).strip()
        if not value_str:
            return
        if should_filter(id_type) and is_junk_identifier(value_str):
            return
        key = (id_type, value_str)
        if key in self._seen:
            return
        self._seen.add(key)
        item: dict = {"type": id_type, "value": value_str, "is_verified": verified}
        if last_confirmed_at is not None:
            item["last_confirmed_at"] = last_confirmed_at
        self.items.append(item)

    def __len__(self) -> int:
        return len(self.items)


def build_envelope(
    *,
    source_record_id: str,
    observed_at: str | None,
    identifiers: list[dict],
    attributes: dict,
    raw_payload: dict,
) -> dict:
    """Assemble a source-record envelope and stamp its content hash."""
    record = {
        "source_record_id": source_record_id,
        "observed_at": observed_at,
        "identifiers": identifiers,
        "attributes": {k: v for k, v in attributes.items() if v is not None},
        "raw_payload": raw_payload,
    }
    record["record_hash"] = compute_hash(record)
    return record
