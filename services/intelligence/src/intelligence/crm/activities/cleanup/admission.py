"""Exact State-backed admission of one accepted CRM activity archive output."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from intelligence.crm.activities.acceptance import (
    AcceptanceDescriptor,
    PublicationPointer,
    RuntimeReader,
    accepted_publication_for_run,
)
from intelligence.crm.activities.bounded import accepted_snapshot
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
    verified = verify_snapshot(snapshot)
    if verified.get("manifest_digest") != descriptor.manifest_digest:
        raise RuntimeError("accepted snapshot manifest conflicts with descriptor")
    actual = tuple(
        OutputInventory(f"outputs/{descriptor.run_id}/{path}", digest, count)
        for path, digest, count in snapshot_inventory(snapshot)
    )
    expected = tuple(
        OutputInventory(
            f"outputs/{descriptor.run_id}/{item.relative_path}", item.sha256, item.byte_count
        )
        for item in descriptor.snapshot_inventory
    )
    if actual != expected or runtime.state.accepted_outputs(descriptor.run_id) != tuple(
        sorted(
            (
                OutputInventory(
                    f"outputs/{descriptor.run_id}/{pointer.descriptor_relative_path}",
                    pointer.descriptor_sha256,
                    pointer.descriptor_byte_count,
                ),
                *expected,
            ),
            key=lambda item: item.relative_path,
        )
    ):
        raise RuntimeError("State inventory conflicts with canonical accepted snapshot")
    return AdmittedArchive(descriptor, pointer, _identities(snapshot))


def _identities(snapshot: Path) -> tuple[dict[str, object], ...]:
    cleanup = json.loads((snapshot / "cleanup-identities.json").read_text(encoding="utf-8"))
    if not isinstance(cleanup, dict) or not isinstance(cleanup.get("identities"), list):
        raise RuntimeError("cleanup identity artifact is invalid")
    records: dict[str, dict[str, object]] = {}
    for path in sorted((snapshot / "records").glob("page-*.json")):
        page = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(page, dict) or not isinstance(page.get("records"), list):
            raise RuntimeError("accepted record page is invalid")
        for record in page["records"]:
            if isinstance(record, dict) and isinstance(record.get("source_record_pk"), str):
                records[record["source_record_pk"]] = record
    result: list[dict[str, object]] = []
    for identity in cleanup["identities"]:
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
