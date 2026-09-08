"""Artifact-only supervised verification for accepted CRM activity snapshots."""

from __future__ import annotations

from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path

from intelligence.artifacts_staging import scan_staged_outputs
from intelligence.crm.activities import checkpoints
from intelligence.crm.activities.acceptance import (
    AcceptanceDescriptor,
    PublicationPointer,
    verification_candidate_name,
)
from intelligence.crm.activities.bounded import (
    ReadBudget,
    ReadLimits,
    accepted_snapshot,
    read_published_evidence,
    verify_snapshot_input,
)
from intelligence.crm.activities.checkpoint_limits import CheckpointLimits
from intelligence.crm.activities.manifests import write_verification
from intelligence.crm.activities.models import sha256_json
from intelligence.crm.activities.snapshot_verifier import snapshot_inventory, verify_snapshot
from intelligence.models import OutputInventory


def verify_accepted(
    run_staging: Path,
    descriptor: AcceptanceDescriptor,
    publication_pointer: PublicationPointer,
    trusted_accepted_outputs: Sequence[OutputInventory],
    *,
    input_limits: ReadLimits,
    output_maximum_bytes: int,
    output_maximum_entries: int,
    checkpoint_limits: CheckpointLimits,
) -> None:
    """Verify artifact bytes against State-provided inventory without source access.

    The caller resolves and validates the descriptor from State before spawning
    this handler. The handler requires no Neo4j configuration or credentials.
    """
    if descriptor.run_id != publication_pointer.run_id:
        raise ValueError("verification descriptor and publication pointer differ")
    if output_maximum_bytes < 1 or output_maximum_entries < 1:
        raise ValueError("verification output limits must be positive")
    workspace = run_staging.parent.parent
    snapshot = accepted_snapshot(workspace, descriptor.run_id, descriptor.snapshot_id)
    selected_count = _trusted_selected_count(
        workspace,
        descriptor,
        trusted_accepted_outputs,
        input_limits,
    )
    verify_snapshot_input(snapshot, input_limits, selected_count)
    _verify_trusted_snapshot_inventory(descriptor, trusted_accepted_outputs, snapshot)
    evidence = dict(verify_snapshot(snapshot))
    if evidence.get("manifest_digest") != descriptor.manifest_digest:
        raise ValueError("accepted manifest changed before verification")
    evidence["accepted_run_id"] = descriptor.run_id
    evidence["accepted_manifest_digest"] = descriptor.manifest_digest
    evidence["accepted_descriptor_sha256"] = publication_pointer.descriptor_sha256
    write_verification(run_staging, evidence)
    inventory = scan_staged_outputs(
        workspace,
        run_staging.name,
        output_maximum_bytes,
        output_maximum_entries,
    )
    checkpoint = checkpoints.checkpoint_root(
        run_staging, descriptor.checkpoint_id, checkpoint_limits
    )
    checkpoints.write_evidence(
        checkpoint,
        verification_candidate_name(run_staging.name),
        {
            "checkpoint_id": descriptor.checkpoint_id,
            "run_id": run_staging.name,
            "accepted_run_id": descriptor.run_id,
            "snapshot_id": descriptor.snapshot_id,
            "accepted_manifest_digest": descriptor.manifest_digest,
            "accepted_descriptor_sha256": publication_pointer.descriptor_sha256,
            "inventory": [_inventory_dict(item) for item in inventory],
        },
        checkpoint_limits,
    )


def _trusted_selected_count(
    workspace: Path,
    descriptor: AcceptanceDescriptor,
    trusted_outputs: Sequence[OutputInventory],
    limits: ReadLimits,
) -> int:
    """Read the descriptor-bound manifest before it may set a row ceiling."""
    relative_path = f"snapshots/crm/activities/{descriptor.snapshot_id}/manifest.json"
    expected_path = f"outputs/{descriptor.run_id}/{relative_path}"
    matches = tuple(item for item in trusted_outputs if item.relative_path == expected_path)
    if len(matches) != 1:
        raise RuntimeError("accepted State inventory lacks one descriptor manifest")
    expected = matches[0]
    metadata, raw = read_published_evidence(
        workspace,
        descriptor.run_id,
        relative_path,
        ReadBudget(limits),
    )
    if len(raw) != expected.byte_count or _bytes_digest(raw) != expected.sha256:
        raise RuntimeError("accepted manifest bytes conflict with trusted State inventory")
    unsigned = dict(metadata)
    manifest_digest = unsigned.pop("digest", None)
    selected_count = metadata.get("selected_count")
    if (
        not isinstance(manifest_digest, str)
        or manifest_digest != descriptor.manifest_digest
        or sha256_json(unsigned) != manifest_digest
        or metadata.get("snapshot_id") != descriptor.snapshot_id
        or metadata.get("boundary_digest") != descriptor.boundary_digest
        or not isinstance(selected_count, int)
        or isinstance(selected_count, bool)
        or selected_count < 0
    ):
        raise RuntimeError("accepted descriptor manifest is invalid")
    return selected_count


def _bytes_digest(value: bytes) -> str:

    return sha256(value).hexdigest()


def _verify_trusted_snapshot_inventory(
    descriptor: AcceptanceDescriptor,
    trusted_outputs: Sequence[OutputInventory],
    snapshot: Path,
) -> None:
    prefix = f"outputs/{descriptor.run_id}/snapshots/crm/activities/{descriptor.snapshot_id}/"
    expected = tuple(
        OutputInventory(
            f"outputs/{descriptor.run_id}/{item.relative_path}", item.sha256, item.byte_count
        )
        for item in descriptor.snapshot_inventory
    )
    registered = tuple(item for item in trusted_outputs if item.relative_path.startswith(prefix))
    if registered != expected:
        raise RuntimeError("accepted State inventory conflicts with acceptance descriptor")
    actual = tuple(
        OutputInventory(f"{prefix}{relative_path}", digest, byte_count)
        for relative_path, digest, byte_count in snapshot_inventory(snapshot)
    )
    if actual != expected:
        raise RuntimeError("accepted snapshot bytes conflict with trusted State inventory")


def _inventory_dict(item: OutputInventory) -> dict[str, object]:
    return {
        "relative_path": item.relative_path,
        "sha256": item.sha256,
        "byte_count": item.byte_count,
    }
