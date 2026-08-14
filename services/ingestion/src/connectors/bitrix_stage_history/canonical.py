"""Immutable v1 stage-event identity and hashing rules.

This module deliberately hashes only frozen semantic fields.  It never hashes
whole REST payloads, pagination metadata, credentials, or runtime timestamps.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
from datetime import UTC, datetime

from src.connectors.bitrix_stage_history.models import StageHistoryItem

_HASH_VERSION = "bitrix-stage-history-v1"


def encode_stage_source_record_id(
    source_contract_id: str,
    entity_type_id: str,
    history_id: str,
) -> str:
    """Encode the approved identity domain injectively without delimiters."""
    return "bitrix-crm-stagehistory-v1:" + "".join(
        _component(value)
        for value in (
            normalize_source_contract_id(source_contract_id),
            _required_text(entity_type_id, "entity_type_id"),
            _required_text(history_id, "history_id"),
        )
    )


def decode_stage_source_record_id(value: str) -> tuple[str, str, str]:
    """Decode the frozen injective identity without changing its encoded bytes."""
    prefix = "bitrix-crm-stagehistory-v1:"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ValueError("stage source record identity has an invalid prefix")
    remainder = value[len(prefix) :]
    components: list[str] = []
    for field_name in ("source_contract_id", "entity_type_id", "history_id"):
        separator = remainder.find(":")
        if separator < 1:
            raise ValueError(f"stage source record identity lacks {field_name} length")
        length_text = remainder[:separator]
        if not length_text.isascii() or not length_text.isdecimal():
            raise ValueError(f"stage source record identity has invalid {field_name} length")
        length = int(length_text)
        if length < 1:
            raise ValueError(f"stage source record identity has empty {field_name}")
        start = separator + 1
        end = start + length
        component = remainder[start:end]
        if len(component) != length:
            raise ValueError(f"stage source record identity truncates {field_name}")
        components.append(component)
        remainder = remainder[end:]
    if remainder:
        raise ValueError("stage source record identity has trailing bytes")
    source_contract_id, entity_type_id, history_id = components
    normalized_contract = normalize_source_contract_id(source_contract_id)
    if source_contract_id != normalized_contract:
        raise ValueError("stage source record identity uses a non-canonical source contract")
    if encode_stage_source_record_id(*components) != value:
        raise ValueError("stage source record identity is not canonically encoded")
    return normalized_contract, entity_type_id, history_id


def canonical_stage_hash_v1(source_contract_id: str, item: StageHistoryItem) -> str:
    """Return the canonical v1 SHA-256 for one typed source observation."""
    payload = {
        "hash_version": _HASH_VERSION,
        "source_contract_id": normalize_source_contract_id(source_contract_id),
        "entity_type_id": _required_text(item.entity_type_id, "entity_type_id"),
        "history_id": _required_text(item.history_id, "history_id"),
        "type_id": _optional_text(item.type_id),
        "owner_id": _required_text(item.owner_id, "owner_id"),
        "event_at": _timestamp(item.created_time),
        "category_id": _optional_text(item.category_id),
        "stage_semantic_id": _optional_text(item.stage_semantic_id),
        "stage_id": _optional_text(item.stage_id),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def normalize_source_contract_id(value: str) -> str:
    """Validate and canonicalize the approved source-contract UUID."""
    if not isinstance(value, str):
        raise ValueError("source_contract_id must be a UUID string")
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError("source_contract_id must be a UUID") from exc


def _component(value: str) -> str:
    return f"{len(value)}:{value}"


def _required_text(value: str, field_name: str) -> str:
    normalized = _normalize_text(value)
    if not normalized.strip():
        raise ValueError(f"{field_name} must be non-empty")
    return normalized


def _optional_text(value: str | None) -> str | None:
    return None if value is None else _normalize_text(value)


def _normalize_text(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("canonical text values must be strings")
    normalized = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in normalized):
        raise ValueError("canonical text values cannot contain control or surrogate characters")
    return normalized


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("stage event timestamp must be timezone-aware")
    utc_value = value.astimezone(UTC)
    return utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")
