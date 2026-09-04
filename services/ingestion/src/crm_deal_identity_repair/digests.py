"""Canonical serialization and domain-separated digests for repair artifacts."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator

from src.connectors.bitrix_stage_history.artifact_manifest import canonical_json_bytes
from src.crm_deal_identity_repair.models import RepairInventoryItem
from src.models import JsonValue

MUTATION_REQUEST_DIGEST_DOMAIN = b"crm-deal-identity-repair-mutation-request-v1\x00"
MUTATION_AUTHORITY_DIGEST_DOMAIN = b"crm-deal-identity-repair-authority-v1\x00"
MUTATION_ROLLBACK_DIGEST_DOMAIN = b"crm-deal-identity-repair-rollback-v1\x00"
MUTATION_REPAIRED_STATE_DIGEST_DOMAIN = b"crm-deal-identity-repair-repaired-state-v1\x00"
MUTATION_RESULT_DIGEST_DOMAIN = b"crm-deal-identity-repair-result-v1\x00"
MUTATION_OUTBOX_DIGEST_DOMAIN = b"crm-deal-identity-repair-outbox-v1\x00"
ROLLBACK_REQUEST_DIGEST_DOMAIN = b"crm-deal-identity-repair-rollback-request-v1\x00"
ROLLBACK_AUTHORITY_DIGEST_DOMAIN = b"crm-deal-identity-repair-rollback-authority-v1\x00"
ROLLBACK_RESULT_DIGEST_DOMAIN = b"crm-deal-identity-repair-rollback-result-v1\x00"
ROLLBACK_STATUS_DIGEST_DOMAIN = b"crm-deal-identity-repair-rollback-status-v1\x00"
ROLLBACK_DRIFT_DIGEST_DOMAIN = b"crm-deal-identity-repair-rollback-drift-v1\x00"
VERIFICATION_REQUEST_DIGEST_DOMAIN = b"crm-deal-identity-repair-verification-request-v1\x00"
VERIFICATION_RESULT_DIGEST_DOMAIN = b"crm-deal-identity-repair-verification-result-v1\x00"
VERIFICATION_SUBJECT_DIGEST_DOMAIN = b"crm-deal-identity-repair-verification-subject-v1\x00"
VERIFICATION_DISPOSITION_DIGEST_DOMAIN = b"crm-deal-identity-repair-verification-disposition-v1\x00"
VERIFICATION_DERIVED_STATE_DIGEST_DOMAIN = b"crm-deal-identity-repair-verification-state-v1\x00"
VERIFICATION_NEGATIVE_CONTROL_DIGEST_DOMAIN = b"crm-deal-identity-repair-negative-control-v1\x00"
VERIFICATION_RUN_EQUATION_DIGEST_DOMAIN = b"crm-deal-identity-repair-run-equation-v1\x00"
VERIFICATION_OUTBOX_CLAIM_DIGEST_DOMAIN = b"crm-deal-identity-repair-outbox-claim-v1\x00"

INVENTORY_DIGEST_DOMAIN = b"crm-deal-identity-repair-inventory-v1\x00"
INVENTORY_PART_MAX_BYTES = 256 * 1024 * 1024


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
    return inventory_digest_from_parts(canonical_json_bytes(item.to_dict()) for item in ordered)


def inventory_digest_from_bytes(content: bytes) -> str:
    """Digest the exact immutable #254 inventory bytes without reserializing them."""
    if not content or not content.endswith(b"\n"):
        raise ValueError("repair inventory bytes must be non-empty canonical JSONL")
    return inventory_digest_from_parts((content,))


def inventory_digest_from_parts(parts: Iterable[bytes]) -> str:
    """Digest canonical inventory bytes without concatenating bounded artifact parts."""
    digest = hashlib.sha256()
    digest.update(INVENTORY_DIGEST_DOMAIN)
    observed = False
    for part in parts:
        if not part or not part.endswith(b"\n"):
            raise ValueError("repair inventory parts must be non-empty canonical JSONL")
        observed = True
        digest.update(part)
    if not observed:
        raise ValueError("repair inventory parts must be non-empty canonical JSONL")
    return "sha256:" + digest.hexdigest()


def inventory_jsonl(items: tuple[RepairInventoryItem, ...]) -> bytes:
    """Return the exact ordering and bytes that inventory digest authenticates."""
    ordered = tuple(sorted(items, key=lambda item: item.inventory_key))
    keys = tuple(item.inventory_key for item in ordered)
    if len(keys) != len(set(keys)):
        raise ValueError("repair inventory rows must have unique source-version identities")
    return canonical_jsonl(tuple(item.to_dict() for item in ordered))


def inventory_jsonl_parts(
    items: tuple[RepairInventoryItem, ...],
    *,
    max_part_bytes: int = INVENTORY_PART_MAX_BYTES,
) -> Iterator[tuple[str, bytes]]:
    """Encode canonical inventory rows into deterministic bounded JSONL parts."""
    if type(max_part_bytes) is not int or max_part_bytes < 1:
        raise ValueError("repair inventory part limit must be a positive integer")
    ordered = tuple(sorted(items, key=lambda item: item.inventory_key))
    keys = tuple(item.inventory_key for item in ordered)
    if not keys:
        raise ValueError("repair inventory cannot be empty")
    if len(keys) != len(set(keys)):
        raise ValueError("repair inventory rows must have unique source-version identities")
    part_index = 1
    current = bytearray()
    for item in ordered:
        row = canonical_json_bytes(item.to_dict())
        if len(row) > max_part_bytes:
            raise ValueError("repair inventory row exceeds the part byte limit")
        if current and len(current) + len(row) > max_part_bytes:
            yield _inventory_part_name(part_index), bytes(current)
            part_index += 1
            current.clear()
        current.extend(row)
    if current:
        yield _inventory_part_name(part_index), bytes(current)


def _inventory_part_name(index: int) -> str:
    return f"inventory-{index:05d}.jsonl"


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


def rollback_request_digest(value: dict[str, JsonValue]) -> str:
    """Digest one immutable rollback transition request."""
    return object_digest(ROLLBACK_REQUEST_DIGEST_DOMAIN, value)


def rollback_authority_digest(value: dict[str, JsonValue]) -> str:
    """Digest the run/unit/fence authorization consumed by rollback."""
    return object_digest(ROLLBACK_AUTHORITY_DIGEST_DOMAIN, value)


def rollback_result_digest(value: dict[str, JsonValue]) -> str:
    """Digest one terminal rollback disposition and state transition."""
    return object_digest(ROLLBACK_RESULT_DIGEST_DOMAIN, value)


def rollback_status_digest(value: dict[str, JsonValue]) -> str:
    """Digest a read-only, validated rollback status projection."""
    return object_digest(ROLLBACK_STATUS_DIGEST_DOMAIN, value)


def rollback_drift_digest(value: dict[str, JsonValue]) -> str:
    """Digest the complete canonical CAS mismatch set without exposing values."""
    return object_digest(ROLLBACK_DRIFT_DIGEST_DOMAIN, value)


def verification_request_digest(value: dict[str, JsonValue]) -> str:
    return object_digest(VERIFICATION_REQUEST_DIGEST_DOMAIN, value)


def verification_result_digest(value: dict[str, JsonValue]) -> str:
    return object_digest(VERIFICATION_RESULT_DIGEST_DOMAIN, value)


def subject_digest(value: dict[str, JsonValue]) -> str:
    return object_digest(VERIFICATION_SUBJECT_DIGEST_DOMAIN, value)


def disposition_digest(value: dict[str, JsonValue]) -> str:
    return object_digest(VERIFICATION_DISPOSITION_DIGEST_DOMAIN, value)


def derived_state_digest(value: dict[str, JsonValue]) -> str:
    return object_digest(VERIFICATION_DERIVED_STATE_DIGEST_DOMAIN, value)


def negative_control_digest(value: dict[str, JsonValue]) -> str:
    return object_digest(VERIFICATION_NEGATIVE_CONTROL_DIGEST_DOMAIN, value)


def run_equation_digest(value: dict[str, JsonValue]) -> str:
    return object_digest(VERIFICATION_RUN_EQUATION_DIGEST_DOMAIN, value)


def outbox_claim_digest(value: dict[str, JsonValue]) -> str:
    return object_digest(VERIFICATION_OUTBOX_CLAIM_DIGEST_DOMAIN, value)
