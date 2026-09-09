"""Exact State-backed admission of one accepted CRM activity archive output."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from intelligence.crm.activities.acceptance import (
    AcceptanceDescriptor,
    PublicationPointer,
    RuntimeReader,
    accepted_publication_for_run,
)
from intelligence.crm.activities.bounded import (
    ReadBudget,
    ReadLimits,
    accepted_snapshot,
    read_published_evidence,
    verify_snapshot_input,
)
from intelligence.crm.activities.cleanup.models import CleanupRequest
from intelligence.crm.activities.snapshot_verifier import snapshot_inventory, verify_snapshot
from intelligence.models import OutputInventory


@dataclass(frozen=True)
class AdmittedArchive:
    descriptor: AcceptanceDescriptor
    pointer: PublicationPointer
    identities: tuple[dict[str, object], ...]


def admit(runtime: RuntimeReader, request: CleanupRequest) -> AdmittedArchive:
    """Fail closed on every locator, State inventory, snapshot, and identity mismatch."""
    descriptor, pointer = accepted_publication_for_run(
        runtime, request.authorization.checkpoint_id, request.authorization.accepted_run_id
    )
    if (
        descriptor.snapshot_id != request.authorization.snapshot_id
        or descriptor.manifest_digest != request.authorization.manifest_digest
    ):
        raise RuntimeError("cleanup authorization locators conflict with accepted archive")
    workspace = runtime.config.workspace
    snapshot = accepted_snapshot(workspace, descriptor.run_id, descriptor.snapshot_id)
    expected = _expected_snapshot_inventory(descriptor)
    _require_registered_inventory(runtime, descriptor, pointer, expected)
    limits = _snapshot_limits(descriptor)
    selected_count = _trusted_selected_count(workspace, descriptor, expected, limits)
    verify_snapshot_input(snapshot, limits, selected_count)
    verified = verify_snapshot(snapshot)
    if verified.get("manifest_digest") != descriptor.manifest_digest:
        raise RuntimeError("accepted snapshot manifest conflicts with descriptor")
    actual = tuple(
        OutputInventory(
            f"outputs/{descriptor.run_id}/snapshots/crm/activities/{descriptor.snapshot_id}/{path}",
            digest,
            count,
        )
        for path, digest, count in snapshot_inventory(snapshot)
    )
    if actual != expected:
        raise RuntimeError("accepted snapshot bytes conflict with trusted State inventory")
    return AdmittedArchive(
        descriptor, pointer, _identities(workspace, descriptor, expected, limits)
    )


def _expected_snapshot_inventory(descriptor: AcceptanceDescriptor) -> tuple[OutputInventory, ...]:
    """Normalize descriptor paths to the published State inventory namespace."""
    return tuple(
        OutputInventory(
            f"outputs/{descriptor.run_id}/{item.relative_path}", item.sha256, item.byte_count
        )
        for item in descriptor.snapshot_inventory
    )


def _require_registered_inventory(
    runtime: RuntimeReader,
    descriptor: AcceptanceDescriptor,
    pointer: PublicationPointer,
    expected: tuple[OutputInventory, ...],
) -> None:
    descriptor_item = OutputInventory(
        f"outputs/{descriptor.run_id}/{pointer.descriptor_relative_path}",
        pointer.descriptor_sha256,
        pointer.descriptor_byte_count,
    )
    registered = runtime.state.accepted_outputs(descriptor.run_id)
    expected_registered = tuple(
        sorted((descriptor_item, *expected), key=lambda item: item.relative_path)
    )
    if registered != expected_registered:
        raise RuntimeError("State inventory conflicts with canonical accepted snapshot")


def _snapshot_limits(descriptor: AcceptanceDescriptor) -> ReadLimits:
    """Derive finite input ceilings solely from the accepted descriptor inventory."""
    inventory = descriptor.snapshot_inventory
    total_bytes = sum(item.byte_count for item in inventory)
    if total_bytes < 1 or len(inventory) < 1:
        raise RuntimeError("accepted descriptor snapshot inventory is invalid")
    return ReadLimits(total_bytes, len(inventory) + 1, descriptor.request.max_rows)


def _trusted_selected_count(
    workspace: Path,
    descriptor: AcceptanceDescriptor,
    expected: tuple[OutputInventory, ...],
    limits: ReadLimits,
) -> int:
    """Read the State-hashed manifest before it may set the bounded row ceiling."""
    relative = f"snapshots/crm/activities/{descriptor.snapshot_id}/manifest.json"
    manifest, raw = read_published_evidence(
        workspace, descriptor.run_id, relative, ReadBudget(limits)
    )
    _require_inventory_bytes(expected, descriptor.run_id, relative, raw)
    selected_count = manifest.get("selected_count")
    if (
        manifest.get("snapshot_id") != descriptor.snapshot_id
        or manifest.get("digest") != descriptor.manifest_digest
        or not isinstance(selected_count, int)
        or isinstance(selected_count, bool)
        or selected_count < 0
    ):
        raise RuntimeError("accepted descriptor manifest is invalid")
    return selected_count


def _identities(
    workspace: Path,
    descriptor: AcceptanceDescriptor,
    expected: tuple[OutputInventory, ...],
    limits: ReadLimits,
) -> tuple[dict[str, object], ...]:
    """Join identities only from canonical, State-hashed, bounded archive pages."""
    budget = ReadBudget(limits)
    cleanup_relative = f"snapshots/crm/activities/{descriptor.snapshot_id}/cleanup-identities.json"
    cleanup, cleanup_raw = read_published_evidence(
        workspace, descriptor.run_id, cleanup_relative, budget
    )
    _require_inventory_bytes(expected, descriptor.run_id, cleanup_relative, cleanup_raw)
    raw_identities = cleanup.get("identities")
    if not isinstance(raw_identities, list):
        raise RuntimeError("cleanup identity artifact is invalid")
    records: dict[str, dict[str, object]] = {}
    prefix = (
        f"outputs/{descriptor.run_id}/snapshots/crm/activities/{descriptor.snapshot_id}/records/"
    )
    pages = tuple(item.relative_path for item in expected if item.relative_path.startswith(prefix))
    for full_relative in pages:
        relative = full_relative.removeprefix(f"outputs/{descriptor.run_id}/")
        page, page_raw = read_published_evidence(workspace, descriptor.run_id, relative, budget)
        _require_inventory_bytes(expected, descriptor.run_id, relative, page_raw)
        raw_records = page.get("records")
        if not isinstance(raw_records, list):
            raise RuntimeError("accepted record page is invalid")
        for record in raw_records:
            if isinstance(record, dict) and isinstance(record.get("source_record_pk"), str):
                records[record["source_record_pk"]] = record
    result: list[dict[str, object]] = []
    for identity in raw_identities:
        if not isinstance(identity, dict) or not isinstance(identity.get("source_record_pk"), str):
            raise RuntimeError("cleanup identity is invalid")
        record = records.get(identity["source_record_pk"])
        if record is None or any(
            identity.get(key) != record.get(key)
            for key in ("record_type", "source_record_version", "record_hash", "lifecycle_status")
        ):
            raise RuntimeError("cleanup identity does not join accepted record evidence")
        result.append(dict(record))
    if [str(value["source_record_pk"]) for value in result] != sorted(
        str(value["source_record_pk"]) for value in result
    ):
        raise RuntimeError("cleanup identities are not canonical")
    return tuple(result)


def _require_inventory_bytes(
    expected: tuple[OutputInventory, ...], run_id: str, relative: str, raw: bytes
) -> None:
    matching = tuple(
        item for item in expected if item.relative_path == f"outputs/{run_id}/{relative}"
    )
    if len(matching) != 1:
        raise RuntimeError("accepted snapshot evidence is not descriptor registered")
    item = matching[0]
    if len(raw) != item.byte_count or sha256(raw).hexdigest() != item.sha256:
        raise RuntimeError("accepted snapshot bytes conflict with trusted State inventory")
