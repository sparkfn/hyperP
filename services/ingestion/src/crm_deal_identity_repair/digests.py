"""Canonical serialization and domain-separated digests for repair artifacts."""

from __future__ import annotations

import hashlib

from src.connectors.bitrix_stage_history.artifact_manifest import canonical_json_bytes
from src.crm_deal_identity_repair.models import RepairInventoryItem
from src.models import JsonValue

MUTATION_REQUEST_DIGEST_DOMAIN = b"crm-deal-identity-repair-mutation-request-v1\x00"
MUTATION_AUTHORITY_DIGEST_DOMAIN = b"crm-deal-identity-repair-authority-v1\x00"
MUTATION_ROLLBACK_DIGEST_DOMAIN = b"crm-deal-identity-repair-rollback-v1\x00"
MUTATION_REPAIRED_STATE_DIGEST_DOMAIN = b"crm-deal-identity-repair-repaired-state-v1\x00"
MUTATION_RESULT_DIGEST_DOMAIN = b"crm-deal-identity-repair-result-v1\x00"
MUTATION_OUTBOX_DIGEST_DOMAIN = b"crm-deal-identity-repair-outbox-v1\x00"

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


def mutation_request_digest(value: dict[str, JsonValue]) -> str:
    """Digest the full immutable command identity before graph work begins."""
    return object_digest(MUTATION_REQUEST_DIGEST_DOMAIN, value)


def authority_evidence_digest(value: dict[str, JsonValue]) -> str:
    """Digest current independently locked authority evidence."""
    return object_digest(MUTATION_AUTHORITY_DIGEST_DOMAIN, value)


def rollback_image_digest(value: dict[str, JsonValue]) -> str:
    """Digest the executable, pre-write rollback image."""
    return object_digest(MUTATION_ROLLBACK_DIGEST_DOMAIN, value)


def repaired_state_digest(value: dict[str, JsonValue]) -> str:
    """Digest the transaction-local expected repaired state."""
    return object_digest(MUTATION_REPAIRED_STATE_DIGEST_DOMAIN, value)


def mutation_result_digest(value: dict[str, JsonValue]) -> str:
    """Digest the immutable result returned for a committed mutation."""
    return object_digest(MUTATION_RESULT_DIGEST_DOMAIN, value)


def outbox_event_digest(value: dict[str, JsonValue]) -> str:
    """Digest the bounded, non-sensitive pending outbox stub."""
    return object_digest(MUTATION_OUTBOX_DIGEST_DOMAIN, value)
