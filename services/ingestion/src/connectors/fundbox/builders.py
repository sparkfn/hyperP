"""Reusable builders for source-record envelopes.

Pure helpers — no DB or Neo4j dependencies — so they can be unit-tested in
isolation and reused by every Fundbox connector.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

import phonenumbers

from src.connectors.fundbox.junk import is_junk_identifier, should_filter
from src.models import JsonValue

_COUNTRY_NAME_TO_REGION: dict[str, str] = {
    "singapore": "SG",
    "sg": "SG",
    "malaysia": "MY",
    "malaysian": "MY",
    "indonesia": "ID",
    "indonesian": "ID",
    "philippines": "PH",
    "filipino": "PH",
}


def phone_region_hint(phone_code: object, country: object) -> str | None:
    """Derive an ISO region hint for phone normalization from POS columns.

    Prefers the numeric calling code (``phone_code``, e.g. ``"60"``); falls
    back to a free-text ``country`` column. Returns ``None`` when neither
    yields a recognized region, so the normalizer falls back to its default
    (SG).
    """
    if phone_code is not None:
        code_str = str(phone_code).strip()
        if code_str.isdigit():
            region = phonenumbers.region_code_for_country_code(int(code_str))
            if region and region != "ZZ":
                return region
    if country is not None:
        country_key = str(country).strip().lower()
        if country_key in _COUNTRY_NAME_TO_REGION:
            return _COUNTRY_NAME_TO_REGION[country_key]
    return None


def _norm_race(value: object) -> str | None:
    """Normalize a Fundbox ``race`` value: trim, title-case, drop empties.

    Fundbox stores race as free text with inconsistent casing
    (``"MALAY"``/``"Malay"``). Title-casing collapses the duplicates while
    preserving sub-ethnicity detail (Javanese, Boyanese, ...). Returns
    ``None`` for empty/whitespace so ``build_envelope`` omits the attribute.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.title()


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
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("0000-"):
            # MySQL zero-date sentinel (non-strict-mode default for unset
            # date/datetime columns, e.g. '0000-00-00', '0000-00-00 00:00:00')
            # — year 0 is never a real timestamp; treat as missing. Shared-helper
            # note: ``serialize_row`` routes every column through ``to_iso``, so
            # any zero-date cell across every connector now serializes as None
            # instead of the literal string, changing the computed ``record_hash``
            # for affected rows — a deliberate one-time hash invalidation (the
            # old literal was never a real timestamp).
            return None
        try:
            d = datetime.fromisoformat(stripped)
        except ValueError:
            try:
                day = date.fromisoformat(stripped)
            except ValueError:
                return stripped
            if not (1900 <= day.year <= 2100):
                return None
            return day.isoformat()
        if not (1900 <= d.year <= 2100):
            return None
        return d.isoformat() + ("Z" if d.tzinfo is None else "")
    return str(value)


def to_iso_first(*values: object) -> str | None:
    """Return the first value among ``values`` that parses to a real ISO timestamp.

    Unlike ``to_iso(a or b)``, each value is run through ``to_iso`` independently,
    so a truthy-but-invalid MySQL zero-date sentinel in an earlier column does
    not mask a valid later column (e.g. a zero-date ``modified`` falls back to
    ``created``). Use this for multi-column ``observed_at`` resolution.
    """
    for value in values:
        iso = to_iso(value)
        if iso is not None:
            return iso
    return None


def coerce_float(value: object) -> float | None:
    """Coerce a source cell (``Decimal``/``int``/``float``/``str``) to ``float``.

    Returns ``None`` for ``None``, empty/whitespace strings, or unparseable text.
    Handles the string form dump-reader cells arrive in (the live-DB
    ``_decimal_to_float`` helpers only accept ``Decimal``/``int``/``float``).
    """
    if value is None:
        return None
    if isinstance(value, int | float | Decimal):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


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


def address_from_row(row: Any) -> dict[str, JsonValue] | None:
    if row is None:
        return None
    m = row._mapping if hasattr(row, "_mapping") else row
    raw = format_address(row)
    address: dict[str, JsonValue] = {
        "raw": raw,
        "street_number": m.get("block"),
        "street_name": m.get("street") or m.get("address_line_1") or m.get("address_1"),
        "unit_number": _unit_from_parts(m.get("floor"), m.get("unit")),
        "building_name": m.get("building") or m.get("address_line_2") or m.get("address_2"),
        "city": m.get("city"),
        "state_province": m.get("state"),
        "postal_code": m.get("postal_code") or m.get("zip"),
        "country_code": m.get("country"),
    }
    cleaned = {key: _json_str(value) for key, value in address.items()}
    if not any(value for value in cleaned.values()):
        return None
    return cleaned


def addresses_from_rows(rows: list[Any]) -> list[dict[str, JsonValue]]:
    addresses: list[dict[str, JsonValue]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for row in rows:
        address = address_from_row(row)
        if address is None:
            continue
        key = tuple(sorted((k, str(v)) for k, v in address.items() if v is not None))
        if key in seen:
            continue
        seen.add(key)
        addresses.append(address)
    return addresses


def _unit_from_parts(floor: object, unit: object) -> str | None:
    floor_text = str(floor or "").strip().lstrip("#")
    unit_text = str(unit or "").strip().lstrip("#")
    if floor_text and unit_text:
        return f"#{floor_text}-{unit_text}"
    if unit_text:
        return unit_text if unit_text.startswith("#") else f"#{unit_text}"
    return None


def _json_str(value: object) -> JsonValue:
    text = str(value or "").strip()
    return text or None


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
        region_hint: str | None = None,
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
            "type": id_type,
            "value": value_str,
            "is_verified": verified,
        }
        if last_confirmed_at is not None:
            item["last_confirmed_at"] = last_confirmed_at
        if region_hint is not None:
            item["region_hint"] = region_hint
        self.items.append(item)

    def __len__(self) -> int:
        return len(self.items)


def build_envelope(
    *,
    source_record_id: str,
    entity_key: str | None = None,
    observed_at: str | None,
    identifiers: list[dict[str, JsonValue]],
    attributes: dict[str, JsonValue],
    raw_payload: dict[str, JsonValue],
    addresses: list[dict[str, JsonValue]] | None = None,
    record_type: Literal[
        "identity", "bankruptcy", "rental_flat", "relationship", "conversation", "sales"
    ] = "identity",
    extraction_confidence: float | None = None,
    extraction_method: str | None = None,
    conversation_ref: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    """Assemble a source-record envelope and stamp its content hash.

    ``record_type`` defaults to ``"identity"`` (first-party identity extract —
    the common case for Fundbox table connectors). Use ``"relationship"`` for
    records describing a different person (e.g. emergency contacts),
    ``"bankruptcy"`` for a government register about a person (SG bankruptcy),
    ``"rental_flat"`` for a government register about a place (SG rental flats),
    and ``"conversation"`` for heuristic chat/voice extracts. Conversation
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
    if entity_key is not None:
        record["entity_key"] = entity_key
    if addresses:
        address_payloads: list[JsonValue] = []
        address_payloads.extend(addresses)
        record["addresses"] = address_payloads
    if extraction_confidence is not None:
        record["extraction_confidence"] = extraction_confidence
    if extraction_method is not None:
        record["extraction_method"] = extraction_method
    if conversation_ref is not None:
        record["conversation_ref"] = conversation_ref
    record["record_hash"] = compute_hash(record)
    return record
