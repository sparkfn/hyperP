"""Supervised artifact verification candidate writer."""

from __future__ import annotations

from pathlib import Path

from intelligence.artifacts_staging import scan_staged_outputs
from intelligence.crm.activities import checkpoints
from intelligence.crm.activities.bounded import (
    ReadLimits,
    accepted_snapshot,
    verify_snapshot_input,
)
from intelligence.crm.activities.checkpoint_limits import CheckpointLimits
from intelligence.crm.activities.manifests import verify_snapshot, write_verification
from intelligence.crm.activities.models import validate_snapshot_id


def verify_accepted(
    run_staging: Path,
    checkpoint_id: str,
    accepted_run_id: str,
    snapshot_id: str,
    accepted_manifest_digest: str,
    *,
    input_limits: ReadLimits,
    output_maximum_bytes: int,
    output_maximum_entries: int,
    checkpoint_limits: CheckpointLimits,
) -> None:
    """Fully verify bounded input under supervision and publish a child-written candidate."""
    validate_snapshot_id(checkpoint_id)
    validate_snapshot_id(snapshot_id)
    _run_id(accepted_run_id)
    _digest(accepted_manifest_digest)
    if output_maximum_bytes < 1 or output_maximum_entries < 1:
        raise ValueError("verification output limits must be positive")
    workspace = run_staging.parent.parent
    snapshot = accepted_snapshot(workspace, accepted_run_id, snapshot_id)
    verify_snapshot_input(snapshot, input_limits)
    evidence = dict(verify_snapshot(snapshot))
    if evidence.get("manifest_digest") != accepted_manifest_digest:
        raise ValueError("accepted manifest changed before verification")
    evidence["accepted_run_id"] = accepted_run_id
    evidence["accepted_manifest_digest"] = accepted_manifest_digest
    write_verification(run_staging, evidence)
    inventory = scan_staged_outputs(
        workspace,
        run_staging.name,
        output_maximum_bytes,
        output_maximum_entries,
    )
    checkpoint = checkpoints.checkpoint_root(run_staging, checkpoint_id, checkpoint_limits)
    checkpoints.write_evidence(
        checkpoint,
        "verification-candidate.json",
        {
            "checkpoint_id": checkpoint_id,
            "run_id": run_staging.name,
            "accepted_run_id": accepted_run_id,
            "snapshot_id": snapshot_id,
            "accepted_manifest_digest": accepted_manifest_digest,
            "inventory": [item.__dict__ for item in inventory],
        },
        checkpoint_limits,
    )


def _run_id(value: str) -> None:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("accepted run identity is unsafe")


def _digest(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("accepted manifest digest is invalid")
