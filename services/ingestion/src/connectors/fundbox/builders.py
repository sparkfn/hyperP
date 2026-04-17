"""Reusable builders for source-record envelopes.

Pure helpers — no DB or Neo4j dependencies — so they can be unit-tested in
isolation and reused by every Fundbox connector.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Literal

from src.connectors.fundbox.junk import is_junk_identifier, should_filter
from src.models import JsonValue


def _json_default(value: object) -> str:
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def compute_hash(record: dict[str, JsonValue]) -> str:
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


# SQLAlchemy Row objects are dynamic — `_mapping` is typed as a Mapping
# whose values come from arbitrary columns. We collapse them to JSON-safe
# primitives at this boundary so the rest of the pipeline stays in
# `JsonValue` territory.
def serialize_row(row: Any) -> dict[str, JsonValue]:
    """Convert a SQLAlchemy Row mapping (or dict) to JSON-safe primitives."""
    mapping = row._mapping if hasattr(row, "_mapping") else row
    return {str(k): to_iso(v) for k, v in mapping.items()}


def format_address(row: Any) -> str | None:
    """Format an address row into a single normalized string.

    Handles both the Fundbox schema (address_line_1/2, street, building,
    block/floor/unit) and the phppos schema (address_1/2, state, zip). Any
    missing key resolves to None via ``.get()``.
    """
    if row is None:
        return None
    m = row._mapping if hasattr(row, "_mapping") else row
    parts: list[object] = [
        m.get("address_line_1") or m.get("address_1"),
        m.get("address_line_2") or m.get("address_2"),
        m.get("street"),
        m.get("building"),
        " ".join(p for p in (m.get("block"), m.get("floor"), m.get("unit")) if p) or None,
        m.get("city"),
        m.get("state"),
        m.get("postal_code") or m.get("zip"),
        m.get("country"),
    ]
    cleaned = [str(p).strip() for p in parts if p]
    return ", ".join(cleaned) if cleaned else None


class IdentifierBag:
    """Collects identifiers, deduping by (type, value) and filtering junk."""

    __slots__ = ("items", "_seen")

    def __init__(self) -> None:
        self.items: list[dict[str, JsonValue]] = []
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
        item: dict[str, JsonValue] = {
            "type": id_type, "value": value_str, "is_verified": verified,
        }
        if last_confirmed_at is not None:
            item["last_confirmed_at"] = last_confirmed_at
        self.items.append(item)

    def __len__(self) -> int:
        return len(self.items)


def build_envelope(
    *,
    source_record_id: str,
    observed_at: str | None,
    identifiers: list[dict[str, JsonValue]],
    attributes: dict[str, JsonValue],
    raw_payload: dict[str, JsonValue],
    record_type: Literal["system", "conversation", "sales"] = "system",
    extraction_confidence: float | None = None,
    extraction_method: str | None = None,
    conversation_ref: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    """Assemble a source-record envelope and stamp its content hash.

    ``record_type`` is ``"system"`` for deterministic extracts from a system
    of record (the default for every Fundbox table connector) and
    ``"conversation"`` for heuristic chat/voice extracts. Conversation
    envelopes must also supply ``extraction_confidence`` and
    ``extraction_method``; conversation_ref carries channel/thread metadata.
    """
    record: dict[str, JsonValue] = {
        "source_record_id": source_record_id,
        "observed_at": observed_at,
        "record_type": record_type,
        "identifiers": list(identifiers),
        "attributes": {k: v for k, v in attributes.items() if v is not None},
        "raw_payload": raw_payload,
    }
    if extraction_confidence is not None:
        record["extraction_confidence"] = extraction_confidence
    if extraction_method is not None:
        record["extraction_method"] = extraction_method
    if conversation_ref is not None:
        record["conversation_ref"] = conversation_ref
    record["record_hash"] = compute_hash(record)
    return record
