"""Authenticated immutable manifests for restricted Bitrix artifacts."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import cast

from src.connectors.bitrix_stage_history.artifact_provenance import (
    ArtifactProvenance,
    parse_provenance,
)
from src.models import JsonValue

MANIFEST_NAME = "artifact-manifest.json"
MANIFEST_SCHEMA_VERSION = 1
MANIFEST_HMAC_DOMAIN = b"bitrix-restricted-artifact-manifest-v1\x00"


@dataclass(frozen=True)
class ArtifactFileDigest:
    """One sealed artifact file and its content digest."""

    relative_path: str
    sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        path = PurePosixPath(self.relative_path)
        if (
            not self.relative_path
            or len(path.parts) != 1
            or path.is_absolute()
            or self.relative_path == MANIFEST_NAME
        ):
            raise ValueError("artifact file path must be a safe flat non-manifest name")
        if len(self.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.sha256
        ):
            raise ValueError("artifact file SHA-256 must be a lowercase hex digest")
        if isinstance(self.byte_count, bool) or self.byte_count < 0:
            raise ValueError("artifact file byte count must be non-negative")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "path": self.relative_path,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
        }


@dataclass(frozen=True)
class ArtifactManifest:
    """Exact authenticated representation of one sealed artifact directory."""

    schema_version: int
    artifact_id: str
    artifact_kind: str
    created_at: str
    retention_expires_at: str
    metadata_json: str
    files: tuple[ArtifactFileDigest, ...]
    provenance: ArtifactProvenance
    backup_path: str
    backup_verified: bool
    signing_key_id: str
    manifest_hmac: str

    def __post_init__(self) -> None:
        if self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise ValueError("unsupported artifact manifest schema version")
        if not self.artifact_id or not self.artifact_kind or not self.signing_key_id:
            raise ValueError("artifact identity, kind, and signing key ID must be non-empty")
        created_at = _parse_utc_timestamp(self.created_at, "created_at")
        retention_expires_at = _parse_utc_timestamp(
            self.retention_expires_at, "retention_expires_at"
        )
        if retention_expires_at <= created_at:
            raise ValueError("artifact retention expiry must be after creation")
        _decode_json_object(self.metadata_json)
        if not self.backup_path or not self.backup_verified:
            raise ValueError("sealed artifacts require a verified backup path")
        paths = tuple(item.relative_path for item in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("artifact manifest file paths must be sorted and unique")
        if self.provenance.total_bytes != sum(item.byte_count for item in self.files):
            raise ValueError("artifact provenance total bytes do not match manifest files")
        if len(self.manifest_hmac) != 64 or any(
            character not in "0123456789abcdef" for character in self.manifest_hmac
        ):
            raise ValueError("artifact manifest HMAC must be a lowercase hex digest")

    @property
    def metadata(self) -> dict[str, JsonValue]:
        """Return an independent copy of authenticated metadata."""
        return _decode_json_object(self.metadata_json)

    def unsigned_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "artifact_id": self.artifact_id,
            "artifact_kind": self.artifact_kind,
            "created_at": self.created_at,
            "retention_expires_at": self.retention_expires_at,
            "metadata": self.metadata,
            "files": [item.to_dict() for item in self.files],
            "provenance": self.provenance.to_dict(),
            "backup": {
                "path": self.backup_path,
                "verified": self.backup_verified,
            },
            "signing_key_id": self.signing_key_id,
        }

    def to_dict(self) -> dict[str, JsonValue]:
        return {**self.unsigned_dict(), "manifest_hmac": self.manifest_hmac}

    def is_expired(self, *, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("artifact verification time must be timezone-aware")
        expiry = _parse_utc_timestamp(self.retention_expires_at, "retention_expires_at")
        return expiry <= current.astimezone(UTC)


def canonical_json_bytes(value: Mapping[str, JsonValue]) -> bytes:
    """Encode standards-compliant canonical JSON for storage and authentication."""
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("artifact JSON must contain finite JSON values") from exc
    return (encoded + "\n").encode("utf-8")


def canonical_metadata_json(value: Mapping[str, JsonValue]) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def compute_manifest_hmac(
    manifest: ArtifactManifest, key: bytes, *, domain: bytes = MANIFEST_HMAC_DOMAIN
) -> str:
    """HMAC the manifest under a caller-scoped domain separator.

    The default preserves the Bitrix stage-history contract byte-for-byte;
    other restricted-artifact consumers (e.g. sales prediction) pass their own
    domain so a manifest sealed by one consumer cannot be replayed as another.
    """
    if len(key) < 32:
        raise ValueError("artifact signing keys must contain at least 32 bytes")
    if not domain:
        raise ValueError("artifact manifest HMAC domain must be non-empty")
    digest = hmac.new(key, digestmod=hashlib.sha256)
    digest.update(domain)
    digest.update(canonical_json_bytes(manifest.unsigned_dict()))
    return digest.hexdigest()


def canonical_marker_bytes(manifest: ArtifactManifest) -> bytes:
    return canonical_json_bytes(
        {
            "artifact_id": manifest.artifact_id,
            "manifest_hmac": manifest.manifest_hmac,
        }
    )


def parse_manifest_bytes(content: bytes) -> ArtifactManifest:
    try:
        raw = cast(JsonValue, json.loads(content.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("sealed artifact manifest is unreadable") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("sealed artifact manifest must be an object")
    expected_keys = {
        "schema_version",
        "artifact_id",
        "artifact_kind",
        "created_at",
        "retention_expires_at",
        "metadata",
        "files",
        "provenance",
        "backup",
        "signing_key_id",
        "manifest_hmac",
    }
    if set(raw) != expected_keys:
        raise RuntimeError("sealed artifact manifest fields are invalid")
    return _parse_manifest(raw)


def _parse_manifest(raw: dict[str, JsonValue]) -> ArtifactManifest:
    schema_version = raw["schema_version"]
    artifact_id = raw["artifact_id"]
    artifact_kind = raw["artifact_kind"]
    created_at = raw["created_at"]
    retention_expires_at = raw["retention_expires_at"]
    metadata = raw["metadata"]
    files = raw["files"]
    provenance = raw["provenance"]
    backup = raw["backup"]
    signing_key_id = raw["signing_key_id"]
    manifest_hmac = raw["manifest_hmac"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or not isinstance(artifact_id, str)
        or not isinstance(artifact_kind, str)
        or not isinstance(created_at, str)
        or not isinstance(retention_expires_at, str)
        or not isinstance(metadata, dict)
        or not isinstance(files, list)
        or not isinstance(backup, dict)
        or not isinstance(signing_key_id, str)
        or not isinstance(manifest_hmac, str)
    ):
        raise RuntimeError("sealed artifact manifest has an invalid shape")
    if set(backup) != {"path", "verified"}:
        raise RuntimeError("sealed artifact backup fields are invalid")
    backup_path = backup["path"]
    backup_verified = backup["verified"]
    if not isinstance(backup_path, str) or backup_verified is not True:
        raise RuntimeError("sealed artifact backup evidence is invalid")
    parsed_files = tuple(_parse_file_digest(item) for item in files)
    try:
        return ArtifactManifest(
            schema_version=schema_version,
            artifact_id=artifact_id,
            artifact_kind=artifact_kind,
            created_at=created_at,
            retention_expires_at=retention_expires_at,
            metadata_json=canonical_metadata_json(metadata),
            files=parsed_files,
            provenance=parse_provenance(provenance),
            backup_path=backup_path,
            backup_verified=True,
            signing_key_id=signing_key_id,
            manifest_hmac=manifest_hmac,
        )
    except ValueError as exc:
        raise RuntimeError("sealed artifact manifest values are invalid") from exc


def _parse_file_digest(raw: JsonValue) -> ArtifactFileDigest:
    if not isinstance(raw, dict) or set(raw) != {"path", "sha256", "byte_count"}:
        raise RuntimeError("sealed artifact file digest fields are invalid")
    relative_path = raw["path"]
    sha256 = raw["sha256"]
    byte_count = raw["byte_count"]
    if (
        not isinstance(relative_path, str)
        or not isinstance(sha256, str)
        or isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
    ):
        raise RuntimeError("sealed artifact file digest is invalid")
    try:
        return ArtifactFileDigest(relative_path, sha256, byte_count)
    except ValueError as exc:
        raise RuntimeError("sealed artifact file digest is invalid") from exc


def _decode_json_object(value: str) -> dict[str, JsonValue]:
    try:
        decoded = cast(JsonValue, json.loads(value))
    except json.JSONDecodeError as exc:
        raise ValueError("artifact metadata JSON is invalid") from exc
    if not isinstance(decoded, dict):
        raise ValueError("artifact metadata must be a JSON object")
    return decoded


def _parse_utc_timestamp(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"artifact {field_name} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"artifact {field_name} must be UTC")
    return parsed.astimezone(UTC)
