"""Strict provenance contract for restricted Bitrix evidence artifacts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import UUID

from src.models import JsonValue

SEALED_ARTIFACT_MODE = 0o500


@dataclass(frozen=True)
class ArtifactProvenanceInput:
    """Caller-supplied provenance whose filesystem fields are store-derived."""

    source_contract_uuid: str
    repository_sha: str
    image_digest: str
    configuration_digest: str
    restricted_boundaries_json: str
    counts_json: str

    @classmethod
    def create(
        cls,
        *,
        source_contract_uuid: str,
        repository_sha: str,
        image_digest: str,
        configuration_digest: str,
        restricted_boundaries: Mapping[str, JsonValue],
        counts: Mapping[str, int],
    ) -> ArtifactProvenanceInput:
        return cls(
            source_contract_uuid=source_contract_uuid,
            repository_sha=repository_sha,
            image_digest=image_digest,
            configuration_digest=configuration_digest,
            restricted_boundaries_json=_canonical_object(restricted_boundaries),
            counts_json=_canonical_counts(counts),
        )

    def __post_init__(self) -> None:
        _validate_identity_fields(
            self.source_contract_uuid,
            self.repository_sha,
            self.image_digest,
            self.configuration_digest,
        )
        if not _decode_object(self.restricted_boundaries_json):
            raise ValueError("artifact restricted boundaries must be non-empty")
        if not _decode_counts(self.counts_json):
            raise ValueError("artifact provenance counts must be non-empty")


@dataclass(frozen=True)
class ArtifactProvenance:
    """HMAC-covered provenance including pinned filesystem identity."""

    artifact_path: str
    primary_device: int
    primary_inode: int
    backup_device: int
    backup_inode: int
    owner_uid: int
    group_gid: int
    directory_mode: int
    source_contract_uuid: str
    repository_sha: str
    image_digest: str
    configuration_digest: str
    restricted_boundaries_json: str
    counts_json: str
    total_bytes: int

    def __post_init__(self) -> None:
        if not Path(self.artifact_path).is_absolute():
            raise ValueError("artifact provenance path must be absolute")
        identities = (
            self.primary_device,
            self.primary_inode,
            self.backup_device,
            self.backup_inode,
            self.owner_uid,
            self.group_gid,
        )
        if any(isinstance(value, bool) or value < 0 for value in identities):
            raise ValueError("artifact provenance filesystem identity must be non-negative")
        if self.directory_mode != SEALED_ARTIFACT_MODE:
            raise ValueError("artifact provenance mode must match the sealed directory mode")
        if isinstance(self.total_bytes, bool) or self.total_bytes < 0:
            raise ValueError("artifact provenance total bytes must be non-negative")
        ArtifactProvenanceInput(
            source_contract_uuid=self.source_contract_uuid,
            repository_sha=self.repository_sha,
            image_digest=self.image_digest,
            configuration_digest=self.configuration_digest,
            restricted_boundaries_json=self.restricted_boundaries_json,
            counts_json=self.counts_json,
        )

    @classmethod
    def from_input(
        cls,
        supplied: ArtifactProvenanceInput,
        *,
        artifact_path: Path,
        primary_device: int,
        primary_inode: int,
        backup_device: int,
        backup_inode: int,
        owner_uid: int,
        group_gid: int,
        total_bytes: int,
    ) -> ArtifactProvenance:
        return cls(
            artifact_path=str(artifact_path),
            primary_device=primary_device,
            primary_inode=primary_inode,
            backup_device=backup_device,
            backup_inode=backup_inode,
            owner_uid=owner_uid,
            group_gid=group_gid,
            directory_mode=SEALED_ARTIFACT_MODE,
            source_contract_uuid=supplied.source_contract_uuid,
            repository_sha=supplied.repository_sha,
            image_digest=supplied.image_digest,
            configuration_digest=supplied.configuration_digest,
            restricted_boundaries_json=supplied.restricted_boundaries_json,
            counts_json=supplied.counts_json,
            total_bytes=total_bytes,
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "artifact_path": self.artifact_path,
            "primary_device": self.primary_device,
            "primary_inode": self.primary_inode,
            "backup_device": self.backup_device,
            "backup_inode": self.backup_inode,
            "owner_uid": self.owner_uid,
            "group_gid": self.group_gid,
            "directory_mode": self.directory_mode,
            "source_contract_uuid": self.source_contract_uuid,
            "repository_sha": self.repository_sha,
            "image_digest": self.image_digest,
            "configuration_digest": self.configuration_digest,
            "restricted_boundaries": _decode_object(self.restricted_boundaries_json),
            "counts": _decode_counts(self.counts_json),
            "total_bytes": self.total_bytes,
        }


def parse_provenance(raw: JsonValue) -> ArtifactProvenance:
    if not isinstance(raw, dict):
        raise RuntimeError("sealed artifact provenance must be an object")
    expected = {
        "artifact_path",
        "primary_device",
        "primary_inode",
        "backup_device",
        "backup_inode",
        "owner_uid",
        "group_gid",
        "directory_mode",
        "source_contract_uuid",
        "repository_sha",
        "image_digest",
        "configuration_digest",
        "restricted_boundaries",
        "counts",
        "total_bytes",
    }
    if set(raw) != expected:
        raise RuntimeError("sealed artifact provenance fields are invalid")
    strings = (
        raw["artifact_path"],
        raw["source_contract_uuid"],
        raw["repository_sha"],
        raw["image_digest"],
        raw["configuration_digest"],
    )
    integers = (
        raw["owner_uid"],
        raw["group_gid"],
        raw["directory_mode"],
        raw["total_bytes"],
        raw["primary_device"],
        raw["primary_inode"],
        raw["backup_device"],
        raw["backup_inode"],
    )
    boundaries = raw["restricted_boundaries"]
    counts = raw["counts"]
    if (
        not all(isinstance(value, str) for value in strings)
        or not all(isinstance(value, int) and not isinstance(value, bool) for value in integers)
        or not isinstance(boundaries, dict)
        or not isinstance(counts, dict)
    ):
        raise RuntimeError("sealed artifact provenance has an invalid shape")
    try:
        return ArtifactProvenance(
            artifact_path=cast(str, strings[0]),
            primary_device=cast(int, integers[4]),
            primary_inode=cast(int, integers[5]),
            backup_device=cast(int, integers[6]),
            backup_inode=cast(int, integers[7]),
            owner_uid=cast(int, integers[0]),
            group_gid=cast(int, integers[1]),
            directory_mode=cast(int, integers[2]),
            source_contract_uuid=cast(str, strings[1]),
            repository_sha=cast(str, strings[2]),
            image_digest=cast(str, strings[3]),
            configuration_digest=cast(str, strings[4]),
            restricted_boundaries_json=_canonical_object(boundaries),
            counts_json=_canonical_counts_object(counts),
            total_bytes=cast(int, integers[3]),
        )
    except ValueError as exc:
        raise RuntimeError("sealed artifact provenance values are invalid") from exc


def _validate_identity_fields(
    source_contract_uuid: str,
    repository_sha: str,
    image_digest: str,
    configuration_digest: str,
) -> None:
    try:
        parsed_uuid = UUID(source_contract_uuid)
    except ValueError as exc:
        raise ValueError("artifact source-contract UUID is invalid") from exc
    if str(parsed_uuid) != source_contract_uuid:
        raise ValueError("artifact source-contract UUID must use canonical lowercase form")
    if len(repository_sha) != 40 or not _is_lower_hex(repository_sha):
        raise ValueError("artifact repository SHA must be a 40-character lowercase digest")
    for label, digest in (
        ("image", image_digest),
        ("configuration", configuration_digest),
    ):
        prefix, separator, value = digest.partition(":")
        if prefix != "sha256" or separator != ":" or len(value) != 64 or not _is_lower_hex(value):
            raise ValueError(f"artifact {label} digest must use sha256 lowercase hex")


def _canonical_object(value: Mapping[str, JsonValue]) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("artifact provenance must contain finite JSON values") from exc


def _canonical_counts(value: Mapping[str, int]) -> str:
    return _canonical_counts_object(dict(value))


def _canonical_counts_object(value: Mapping[str, JsonValue]) -> str:
    if any(
        not key or not isinstance(count, int) or isinstance(count, bool) or count < 0
        for key, count in value.items()
    ):
        raise ValueError("artifact provenance counts must be named non-negative integers")
    return _canonical_object(value)


def _decode_object(value: str) -> dict[str, JsonValue]:
    decoded = cast(JsonValue, json.loads(value))
    if not isinstance(decoded, dict):
        raise ValueError("artifact provenance JSON must be an object")
    return decoded


def _decode_counts(value: str) -> dict[str, JsonValue]:
    decoded = _decode_object(value)
    _canonical_counts_object(decoded)
    return decoded


def _is_lower_hex(value: str) -> bool:
    return all(character in "0123456789abcdef" for character in value)
