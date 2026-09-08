"""Bounded candidate-to-State acceptance derivation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from intelligence.crm.activities.bounded import (
    CANDIDATE_READ_LIMITS,
    ReadBudget,
    ReadLimits,
    checkpoint_root,
    read_evidence,
)
from intelligence.crm.activities.model_parsing import parse_request
from intelligence.crm.activities.models import ArchiveRequest, validate_snapshot_id
from intelligence.models import OutputInventory
from intelligence.runtime import IntelligenceRuntime

_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class PublicationCandidate:
    """A structurally valid child-written publication candidate."""

    raw: dict[str, object]
    request: ArchiveRequest
    boundary_digest: str
    run_id: str
    snapshot_id: str
    manifest_digest: str
    cleanup_identity_digest: str
    inventory: tuple[OutputInventory, ...]


@dataclass(frozen=True)
class VerificationCandidate:
    """A structurally valid child-written verification candidate."""

    raw: dict[str, object]
    run_id: str
    accepted_run_id: str
    snapshot_id: str
    accepted_manifest_digest: str
    inventory: tuple[OutputInventory, ...]


def publication(runtime: IntelligenceRuntime, checkpoint_id: str) -> dict[str, object] | None:
    """Derive publication only from bounded candidate metadata and completed State output rows."""
    candidate = read_publication_candidate(runtime.config.workspace, checkpoint_id)
    if candidate is None:
        return None
    _completed_with_inventory(runtime, candidate.run_id, candidate.inventory)
    return dict(candidate.raw)


def verification(runtime: IntelligenceRuntime, checkpoint_id: str) -> dict[str, object] | None:
    """Derive verification only from bounded candidate metadata and completed State output rows."""
    candidate = read_verification_candidate(runtime.config.workspace, checkpoint_id)
    if candidate is None:
        return None
    _completed_with_inventory(runtime, candidate.run_id, candidate.inventory)
    return dict(candidate.raw)


def read_publication_candidate(
    workspace: Path, checkpoint_id: str, limits: ReadLimits = CANDIDATE_READ_LIMITS
) -> PublicationCandidate | None:
    """Read one bounded publication candidate without inspecting a published snapshot."""
    value = _candidate(workspace, checkpoint_id, "publication-candidate.json", limits)
    return None if value is None else _parse_publication(value, checkpoint_id)


def read_verification_candidate(
    workspace: Path, checkpoint_id: str, limits: ReadLimits = CANDIDATE_READ_LIMITS
) -> VerificationCandidate | None:
    """Read one bounded verification candidate without inspecting a published snapshot."""
    value = _candidate(workspace, checkpoint_id, "verification-candidate.json", limits)
    return None if value is None else _parse_verification(value, checkpoint_id)


def _candidate(
    workspace: Path, checkpoint_id: str, name: str, limits: ReadLimits
) -> dict[str, object] | None:
    root = checkpoint_root(workspace, checkpoint_id)
    if root is None:
        return None
    budget = ReadBudget(limits)
    value = read_evidence(root, name, budget)
    if value is not None and isinstance(value.get("inventory"), list):
        budget.add_rows(len(value["inventory"]))
    return value


def _parse_publication(value: dict[str, object], checkpoint_id: str) -> PublicationCandidate:
    required = {
        "checkpoint_id",
        "request",
        "boundary_digest",
        "run_id",
        "snapshot_id",
        "manifest_digest",
        "cleanup_identity_digest",
        "inventory",
    }
    if set(value) != required or value.get("checkpoint_id") != checkpoint_id:
        raise ValueError("publication candidate schema is invalid")
    request_value = value.get("request")
    if not isinstance(request_value, dict):
        raise ValueError("publication candidate request is invalid")
    request = parse_request(request_value)
    if request.snapshot_id != checkpoint_id or request.as_public_dict() != request_value:
        raise ValueError("publication candidate request is invalid")
    return PublicationCandidate(
        value,
        request,
        _digest(value, "boundary_digest"),
        _run_id(value, "run_id"),
        _snapshot_id(value, "snapshot_id"),
        _digest(value, "manifest_digest"),
        _digest(value, "cleanup_identity_digest"),
        _inventory(value),
    )


def _parse_verification(value: dict[str, object], checkpoint_id: str) -> VerificationCandidate:
    required = {
        "checkpoint_id",
        "run_id",
        "accepted_run_id",
        "snapshot_id",
        "accepted_manifest_digest",
        "inventory",
    }
    if set(value) != required or value.get("checkpoint_id") != checkpoint_id:
        raise ValueError("verification candidate schema is invalid")
    return VerificationCandidate(
        value,
        _run_id(value, "run_id"),
        _run_id(value, "accepted_run_id"),
        _snapshot_id(value, "snapshot_id"),
        _digest(value, "accepted_manifest_digest"),
        _inventory(value),
    )


def _inventory(value: dict[str, object]) -> tuple[OutputInventory, ...]:
    raw_inventory = value.get("inventory")
    if not isinstance(raw_inventory, list):
        raise ValueError("candidate inventory is invalid")
    inventory = tuple(_inventory_item(item) for item in raw_inventory)
    if tuple(sorted(inventory, key=lambda item: item.relative_path)) != inventory:
        raise ValueError("candidate inventory is not sorted")
    if len({item.relative_path for item in inventory}) != len(inventory):
        raise ValueError("candidate inventory has duplicate paths")
    return inventory


def _inventory_item(value: object) -> OutputInventory:
    if not isinstance(value, dict) or set(value) != {"relative_path", "sha256", "byte_count"}:
        raise ValueError("candidate inventory item is invalid")
    path, digest, count = value.get("relative_path"), value.get("sha256"), value.get("byte_count")
    if (
        not isinstance(path, str)
        or not _safe_relative_path(path)
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
    ):
        raise ValueError("candidate inventory item is invalid")
    return OutputInventory(path, digest, count)


def _completed_with_inventory(
    runtime: IntelligenceRuntime, run_id: str, inventory: tuple[OutputInventory, ...]
) -> None:
    run = runtime.state.inspect(run_id)
    if run is None or run.state != "completed":
        raise RuntimeError("candidate run is not completed")
    expected = tuple(
        OutputInventory(f"outputs/{run_id}/{item.relative_path}", item.sha256, item.byte_count)
        for item in inventory
    )
    if runtime.state.accepted_outputs(run_id) != expected:
        raise RuntimeError("candidate inventory conflicts with accepted outputs")


def _digest(value: dict[str, object], key: str) -> str:
    result = _text(value, key)
    if _SHA256.fullmatch(result) is None:
        raise ValueError(f"candidate {key} is invalid")
    return result


def _run_id(value: dict[str, object], key: str) -> str:
    result = _text(value, key)
    if result in {".", ".."} or "/" in result or "\\" in result:
        raise ValueError(f"candidate {key} is invalid")
    return result


def _snapshot_id(value: dict[str, object], key: str) -> str:
    result = _text(value, key)
    validate_snapshot_id(result)
    return result


def _safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        value not in {"", ".", ".."}
        and "\\" not in value
        and not path.is_absolute()
        and ".." not in path.parts
        and path.as_posix() == value
    )


def _text(value: dict[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"candidate {key} is invalid")
    return result
