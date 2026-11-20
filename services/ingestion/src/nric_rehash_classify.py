"""Typed NRIC Identifier rows and pure rehash classification."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from neo4j import Record

from src.normalizers.nric import (
    is_masked_nric,
    is_nric_digest,
    is_nric_shape,
    mask_nric,
    normalize_nric,
    nric_checksum_valid,
    nric_digest,
)


@dataclass(frozen=True)
class NricIdentifierRow:
    """Validated in-memory projection of one NRIC Identifier row."""

    identifier_id: str | None
    identifier_scope: str | None
    normalized_value: str | None = field(repr=False)
    hashed_value: str | None = field(repr=False)
    nric_masked: str | None = field(repr=False)
    checksum_valid: bool | None
    nric_key_version: str | None
    nric_encrypted: str | None = field(repr=False)


@dataclass(frozen=True)
class NricClassification:
    """Privacy-preserving classification and in-memory conversion values."""

    identifier_id: str | None
    category: Literal["convertible", "already_digest", "unprocessable"] | None
    blocker: Literal["missing_id", "unexpected_scope"] | None = None
    reason: Literal["masked", "empty", "unknown_format"] | None = None
    canonical: str | None = field(default=None, repr=False)
    digest: str | None = field(default=None, repr=False)
    masked: str | None = field(default=None, repr=False)
    checksum_valid: bool | None = None
    digest_metadata_gap: bool = False


def classify_nric_identifier(
    row: NricIdentifierRow,
    key: str,
    key_version: str,
) -> NricClassification:
    """Classify one row without exposing or persisting its sensitive values."""
    if row.identifier_id is None:
        return NricClassification(row.identifier_id, None, blocker="missing_id")
    if row.identifier_scope is not None and row.identifier_scope != "global":
        return NricClassification(row.identifier_id, None, blocker="unexpected_scope")

    value = row.normalized_value
    if value is None:
        return NricClassification(row.identifier_id, "unprocessable", reason="empty")
    if is_nric_digest(value):
        has_gap = (
            row.nric_masked is None
            or row.checksum_valid is None
            or row.nric_key_version is None
            or row.nric_encrypted is None
            or row.hashed_value != value
            or row.nric_key_version != key_version
        )
        return NricClassification(
            row.identifier_id,
            "already_digest",
            digest_metadata_gap=has_gap,
        )
    if is_masked_nric(value):
        return NricClassification(row.identifier_id, "unprocessable", reason="masked")

    canonical, _quality = normalize_nric(value)
    if canonical is None:
        return NricClassification(row.identifier_id, "unprocessable", reason="empty")
    if not is_nric_shape(canonical):
        return NricClassification(row.identifier_id, "unprocessable", reason="unknown_format")

    checksum_valid = nric_checksum_valid(canonical)
    return NricClassification(
        identifier_id=row.identifier_id,
        category="convertible",
        canonical=canonical,
        digest=nric_digest(canonical, key),
        masked=mask_nric(canonical),
        checksum_valid=checksum_valid,
    )


def identifier_row_from_record(record: Record) -> NricIdentifierRow:
    """Convert one Neo4j row to a validated NRIC Identifier projection."""
    raw_id: object = record.get("identifier_id")
    raw_scope: object = record.get("identifier_scope")
    raw_value: object = record.get("normalized_value")
    raw_hashed_value: object = record.get("hashed_value")
    raw_masked: object = record.get("nric_masked")
    raw_checksum: object = record.get("checksum_valid")
    raw_key_version: object = record.get("nric_key_version")
    raw_encrypted: object = record.get("nric_encrypted")
    return NricIdentifierRow(
        identifier_id=raw_id if isinstance(raw_id, str) else None,
        # Coerce non-string scopes to a non-global value for fail-closed classification.
        identifier_scope=(raw_scope if raw_scope is None or isinstance(raw_scope, str) else ""),
        normalized_value=raw_value if isinstance(raw_value, str) else None,
        hashed_value=raw_hashed_value if isinstance(raw_hashed_value, str) else None,
        nric_masked=raw_masked if isinstance(raw_masked, str) else None,
        checksum_valid=raw_checksum if isinstance(raw_checksum, bool) else None,
        nric_key_version=raw_key_version if isinstance(raw_key_version, str) else None,
        nric_encrypted=raw_encrypted if isinstance(raw_encrypted, str) else None,
    )
