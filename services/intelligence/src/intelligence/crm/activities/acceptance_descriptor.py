"""Immutable acceptance descriptor codec and staging writer."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath

from intelligence.artifacts import canonical_json
from intelligence.crm.activities.checkpoint_resume import request_digest
from intelligence.crm.activities.model_parsing import parse_request
from intelligence.crm.activities.models import ArchiveRequest, validate_snapshot_id
from intelligence.models import OutputInventory

_SHA256 = frozenset("0123456789abcdef")
_DESCRIPTOR_SCHEMA = "crm-activities-acceptance-descriptor-v1"
_DESCRIPTOR_DIRECTORY = "acceptance-descriptors/crm/activities"


@dataclass(frozen=True)
class PublicationPointer:
    """The complete hidden publication-candidate payload."""

    run_id: str
    descriptor_relative_path: str
    descriptor_sha256: str
    descriptor_byte_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "descriptor_relative_path": self.descriptor_relative_path,
            "descriptor_sha256": self.descriptor_sha256,
            "descriptor_byte_count": self.descriptor_byte_count,
        }


@dataclass(frozen=True)
class AcceptanceDescriptor:
    """Immutable published provenance for one accepted archive snapshot."""

    raw: dict[str, object]
    checkpoint_id: str
    request: ArchiveRequest
    request_digest: str
    boundary_digest: str
    run_id: str
    command: str
    snapshot_id: str
    manifest_digest: str
    cleanup_identity_digest: str
    snapshot_inventory: tuple[OutputInventory, ...]
    digest: str


def descriptor_relative_path(checkpoint_id: str) -> str:
    """Return the fixed published descriptor path for one checkpoint/run output."""
    validate_snapshot_id(checkpoint_id)
    return f"{_DESCRIPTOR_DIRECTORY}/{checkpoint_id}.json"


def publication_descriptor(
    checkpoint_id: str,
    request: ArchiveRequest,
    boundary_digest: str,
    run_id: str,
    command: str,
    snapshot_id: str,
    manifest_digest: str,
    cleanup_identity_digest: str,
    snapshot_inventory: Iterable[OutputInventory],
) -> dict[str, object]:
    """Build canonical acceptance evidence before the runtime publishes it."""
    validate_snapshot_id(checkpoint_id)
    validate_snapshot_id(snapshot_id)
    if request.snapshot_id != checkpoint_id:
        raise ValueError("descriptor checkpoint and request differ")
    _digest(boundary_digest, "descriptor boundary digest")
    _run_id(run_id, "descriptor run")
    _text({"command": command}, "command")
    _digest(manifest_digest, "descriptor manifest digest")
    _digest(cleanup_identity_digest, "descriptor cleanup digest")
    inventory = snapshot_inventory_for(snapshot_id, tuple(snapshot_inventory))
    value: dict[str, object] = {
        "schema_version": _DESCRIPTOR_SCHEMA,
        "checkpoint_id": checkpoint_id,
        "request": request.as_public_dict(),
        "request_digest": request_digest(request),
        "boundary_digest": boundary_digest,
        "run_id": run_id,
        "command": command,
        "snapshot_id": snapshot_id,
        "manifest_digest": manifest_digest,
        "cleanup_identity_digest": cleanup_identity_digest,
        "snapshot_inventory": [inventory_dict(item) for item in inventory],
    }
    value["digest"] = json_digest(value)
    return value


def write_publication_descriptor(
    run_staging: Path, value: Mapping[str, object]
) -> PublicationPointer:
    """Write one canonical descriptor into current staging and return its pointer."""
    descriptor = parse_descriptor(value)
    if descriptor.run_id != run_staging.name:
        raise ValueError("descriptor run does not match staging run")
    relative_path = descriptor_relative_path(descriptor.checkpoint_id)
    target = run_staging.joinpath(*PurePosixPath(relative_path).parts)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = canonical_json(descriptor.raw).encode("utf-8")
    try:
        handle = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if target.read_bytes() != payload:
            raise RuntimeError("immutable publication descriptor conflicts") from None
    else:
        with os.fdopen(handle, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    return PublicationPointer(run_staging.name, relative_path, bytes_digest(payload), len(payload))


def parse_descriptor(value: Mapping[str, object]) -> AcceptanceDescriptor:
    """Parse the exact canonical published acceptance-descriptor schema."""
    expected = {
        "schema_version",
        "checkpoint_id",
        "request",
        "request_digest",
        "boundary_digest",
        "run_id",
        "command",
        "snapshot_id",
        "manifest_digest",
        "cleanup_identity_digest",
        "snapshot_inventory",
        "digest",
    }
    raw = dict(value)
    if set(raw) != expected or raw.get("schema_version") != _DESCRIPTOR_SCHEMA:
        raise ValueError("acceptance descriptor schema is invalid")
    checkpoint_id = snapshot_id(raw, "checkpoint_id")
    request_value = raw.get("request")
    if not isinstance(request_value, dict):
        raise ValueError("acceptance descriptor request is invalid")
    request = parse_request(request_value)
    if request.snapshot_id != checkpoint_id or request.as_public_dict() != request_value:
        raise ValueError("acceptance descriptor request is invalid")
    descriptor_digest = _digest(raw.get("digest"), "acceptance descriptor digest")
    unsigned = dict(raw)
    unsigned.pop("digest")
    if descriptor_digest != json_digest(unsigned):
        raise ValueError("acceptance descriptor digest is invalid")
    result = AcceptanceDescriptor(
        raw,
        checkpoint_id,
        request,
        _digest(raw.get("request_digest"), "request digest"),
        _digest(raw.get("boundary_digest"), "boundary digest"),
        _run_id(_text(raw, "run_id"), "descriptor run"),
        _text(raw, "command"),
        snapshot_id(raw, "snapshot_id"),
        _digest(raw.get("manifest_digest"), "manifest digest"),
        _digest(raw.get("cleanup_identity_digest"), "cleanup digest"),
        snapshot_inventory_for(
            snapshot_id(raw, "snapshot_id"),
            inventory(raw, "snapshot_inventory"),
        ),
        descriptor_digest,
    )
    if result.request_digest != request_digest(result.request):
        raise ValueError("acceptance descriptor request digest is invalid")
    return result


def snapshot_inventory_for(
    snapshot_id_value: str, values: tuple[OutputInventory, ...]
) -> tuple[OutputInventory, ...]:
    prefix = f"snapshots/crm/activities/{snapshot_id_value}/"
    if not values or tuple(sorted(values, key=lambda item: item.relative_path)) != values:
        raise ValueError("descriptor snapshot inventory is invalid")
    if len({item.relative_path for item in values}) != len(values):
        raise ValueError("descriptor snapshot inventory has duplicate paths")
    if any(not item.relative_path.startswith(prefix) for item in values):
        raise ValueError("descriptor snapshot inventory path is invalid")
    return values


def inventory(value: Mapping[str, object], key: str) -> tuple[OutputInventory, ...]:
    """Parse a sorted, unique canonical output inventory."""
    raw = value.get(key)
    if not isinstance(raw, list):
        raise ValueError("candidate inventory is invalid")
    parsed = tuple(inventory_item(item) for item in raw)
    if tuple(sorted(parsed, key=lambda item: item.relative_path)) != parsed:
        raise ValueError("candidate inventory is not sorted")
    if len({item.relative_path for item in parsed}) != len(parsed):
        raise ValueError("candidate inventory has duplicate paths")
    return parsed


def inventory_item(value: object) -> OutputInventory:
    if not isinstance(value, dict) or set(value) != {"relative_path", "sha256", "byte_count"}:
        raise ValueError("candidate inventory item is invalid")
    path = value.get("relative_path")
    if not isinstance(path, str) or not safe_relative_path(path):
        raise ValueError("candidate inventory item path is invalid")
    return OutputInventory(path, _digest(value.get("sha256"), "sha256"), count(value, "byte_count"))


def published_inventory(
    run_id: str, values: tuple[OutputInventory, ...]
) -> tuple[OutputInventory, ...]:
    return tuple(
        OutputInventory(f"outputs/{run_id}/{item.relative_path}", item.sha256, item.byte_count)
        for item in values
    )


def inventory_dict(item: OutputInventory) -> dict[str, object]:
    return {
        "relative_path": item.relative_path,
        "sha256": item.sha256,
        "byte_count": item.byte_count,
    }


def bytes_digest(value: bytes) -> str:
    return sha256(value).hexdigest()


def json_digest(value: Mapping[str, object]) -> str:
    return bytes_digest(canonical_json(dict(value)).encode("utf-8"))


def count(value: Mapping[str, object], key: str) -> int:
    result = value.get(key)
    if not isinstance(result, int) or isinstance(result, bool) or result < 0:
        raise ValueError(f"{key} is invalid")
    return result


def safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        value not in {"", ".", ".."}
        and "\\" not in value
        and not path.is_absolute()
        and ".." not in path.parts
        and path.as_posix() == value
    )


def snapshot_id(value: Mapping[str, object], key: str) -> str:
    result = _text(value, key)
    validate_snapshot_id(result)
    return result


def run_id(value: str, field: str) -> str:
    return _run_id(value, field)


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or set(value) - _SHA256:
        raise ValueError(f"{field} is invalid")
    return value


def _run_id(value: str, field: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"{field} is invalid")
    return value


def _text(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"{key} is invalid")
    return result
