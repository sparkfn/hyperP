"""Canonical serialization and domain-separated digests for repair artifacts."""

from __future__ import annotations

import hashlib

from src.connectors.bitrix_stage_history.artifact_manifest import canonical_json_bytes
from src.crm_deal_identity_repair.models import RepairInventoryItem
from src.models import JsonValue

INVENTORY_DIGEST_DOMAIN = b"crm-deal-identity-repair-inventory-v1\x00"


def canonical_jsonl(rows: tuple[dict[str, JsonValue], ...]) -> bytes:
    """Encode pre-sorted repair rows as canonical newline-delimited JSON."""
    return b"".join(canonical_json_bytes(row) for row in rows)


def object_digest(domain: bytes, value: dict[str, JsonValue]) -> str:
    """Digest one canonical JSON object under a non-empty caller domain."""
    if not domain:
        raise ValueError("repair digest domain must be non-empty")
    digest = hashlib.sha256()
    digest.update(domain)
    digest.update(canonical_json_bytes(value))
    return "sha256:" + digest.hexdigest()


def inventory_digest(items: tuple[RepairInventoryItem, ...]) -> str:
    """Digest frozen inventory rows independently of caller ordering."""
    ordered = tuple(sorted(items, key=lambda item: item.inventory_key))
    keys = tuple(item.inventory_key for item in ordered)
    if len(keys) != len(set(keys)):
        raise ValueError("repair inventory rows must have unique source-version identities")
    digest = hashlib.sha256()
    digest.update(INVENTORY_DIGEST_DOMAIN)
    digest.update(canonical_jsonl(tuple(item.to_dict() for item in ordered)))
    return "sha256:" + digest.hexdigest()


def inventory_digest_from_bytes(content: bytes) -> str:
    """Digest the exact immutable #254 inventory bytes without reserializing them."""
    if not content or not content.endswith(b"\n"):
        raise ValueError("repair inventory bytes must be non-empty canonical JSONL")
    digest = hashlib.sha256()
    digest.update(INVENTORY_DIGEST_DOMAIN)
    digest.update(content)
    return "sha256:" + digest.hexdigest()


def inventory_jsonl(items: tuple[RepairInventoryItem, ...]) -> bytes:
    """Return the exact ordering and bytes that inventory digest authenticates."""
    ordered = tuple(sorted(items, key=lambda item: item.inventory_key))
    keys = tuple(item.inventory_key for item in ordered)
    if len(keys) != len(set(keys)):
        raise ValueError("repair inventory rows must have unique source-version identities")
    return canonical_jsonl(tuple(item.to_dict() for item in ordered))
