"""Accepted-output admission and post-publication linkage helpers."""

from __future__ import annotations

import json
from pathlib import Path

from intelligence.artifacts import canonical_json, sha256_file
from intelligence.config import RuntimeConfig
from intelligence.crm.activities import checkpoints
from intelligence.crm.activities.manifests import snapshot_inventory, verify_snapshot
from intelligence.runtime import IntelligenceRuntime


def accepted_manifest(
    config: RuntimeConfig, accepted_run_id: str, snapshot_id: str
) -> dict[str, object]:
    runtime = IntelligenceRuntime(config)
    try:
        return require_accepted_snapshot(runtime, accepted_run_id, snapshot_id)
    finally:
        runtime.close()


def require_accepted_snapshot(
    runtime: IntelligenceRuntime, accepted_run_id: str, snapshot_id: str
) -> dict[str, object]:
    run = runtime.state.inspect(accepted_run_id)
    if run is None or run.state != "completed":
        raise RuntimeError("verification requires a completed accepted Intelligence run")
    snapshot = (
        runtime.config.workspace
        / "outputs"
        / accepted_run_id
        / "snapshots"
        / "crm"
        / "activities"
        / snapshot_id
    )
    evidence = verify_snapshot(snapshot)
    assert_registered_snapshot(runtime, accepted_run_id, snapshot_id, snapshot)
    if not evidence.get("verified"):
        raise RuntimeError("accepted activity snapshot verification failed")
    return dict(evidence)


def assert_registered_snapshot(
    runtime: IntelligenceRuntime,
    run_id: str,
    snapshot_id: str,
    snapshot: Path,
) -> None:
    expected = {
        (f"outputs/{run_id}/snapshots/crm/activities/{snapshot_id}/{path}", digest, count)
        for path, digest, count in snapshot_inventory(snapshot)
    }
    actual = {
        (item.relative_path, item.sha256, item.byte_count)
        for item in runtime.state.accepted_outputs(run_id)
        if item.relative_path.startswith(
            f"outputs/{run_id}/snapshots/crm/activities/{snapshot_id}/"
        )
    }
    if actual != expected:
        raise RuntimeError(
            "accepted output inventory does not cover the requested activity snapshot"
        )


def record_acceptance(
    runtime: IntelligenceRuntime, checkpoint_id: str, run_id: str
) -> dict[str, object]:
    run = runtime.state.inspect(run_id)
    if run is None or run.state != "completed":
        raise RuntimeError("archive publication did not complete")
    root = runtime.config.workspace / "staging" / ".crm-activities" / checkpoint_id
    manifest = checkpoints.read_evidence(root, "accepted-manifest.json")
    snapshot_id = manifest.get("snapshot_id")
    if not isinstance(snapshot_id, str):
        raise RuntimeError("accepted domain manifest has no snapshot identity")
    snapshot = (
        runtime.config.workspace
        / "outputs"
        / run_id
        / "snapshots"
        / "crm"
        / "activities"
        / snapshot_id
    )
    verify_snapshot(snapshot)
    assert_registered_snapshot(runtime, run_id, snapshot_id, snapshot)
    outputs = runtime.state.accepted_outputs(run_id)
    acceptance = {
        "checkpoint_id": checkpoint_id,
        "run_id": run_id,
        "snapshot_id": snapshot_id,
        "manifest_digest": manifest.get("digest"),
        "cleanup_identity_digest": manifest.get("cleanup_identity_digest"),
        "outputs": [item.__dict__ for item in outputs],
    }
    checkpoints.write_evidence(
        root,
        "acceptance.json",
        acceptance,
    )
    return acceptance


def record_verification(
    runtime: IntelligenceRuntime,
    checkpoint_id: str,
    accepted_run_id: str,
    accepted_manifest_digest: str,
    run_id: str,
) -> None:
    run = runtime.state.inspect(run_id)
    if run is None or run.state != "completed":
        raise RuntimeError("verification publication did not complete")
    root = runtime.config.workspace / "staging" / ".crm-activities" / checkpoint_id
    evidence = checkpoints.read_evidence(root, "acceptance.json")
    snapshot_id = evidence.get("snapshot_id")
    if not isinstance(snapshot_id, str):
        raise RuntimeError("accepted checkpoint linkage is corrupt")
    artifact = (
        runtime.config.workspace
        / "outputs"
        / run_id
        / "verifications"
        / "crm"
        / "activities"
        / f"{snapshot_id}.json"
    )
    if artifact.is_symlink() or not artifact.is_file():
        raise RuntimeError("verification artifact is absent")
    parsed = json.loads(artifact.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict) or artifact.read_bytes() != canonical_json(parsed).encode(
        "utf-8"
    ):
        raise RuntimeError("verification artifact is corrupt")
    if (
        parsed.get("accepted_run_id") != accepted_run_id
        or parsed.get("accepted_manifest_digest") != accepted_manifest_digest
    ):
        raise RuntimeError("verification artifact is not bound to accepted evidence")
    expected = f"outputs/{run_id}/verifications/crm/activities/{snapshot_id}.json"
    outputs = runtime.state.accepted_outputs(run_id)
    matching = [item for item in outputs if item.relative_path == expected]
    if (
        len(matching) != 1
        or matching[0].sha256 != sha256_file(artifact)
        or matching[0].byte_count != artifact.stat().st_size
    ):
        raise RuntimeError("verification artifact is not registered")
    checkpoints.write_evidence(
        root,
        "verification.json",
        {
            "verification_run_id": run_id,
            "accepted_run_id": accepted_run_id,
            "accepted_manifest_digest": accepted_manifest_digest,
            "artifact": matching[0].__dict__,
        },
    )
