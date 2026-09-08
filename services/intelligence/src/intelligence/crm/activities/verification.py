"""Artifact-only verification; it never reads or mutates Neo4j."""

from __future__ import annotations

from pathlib import Path

from intelligence.crm.activities.manifests import verify_snapshot, write_verification
from intelligence.crm.activities.models import validate_snapshot_id


def verify_accepted(
    run_staging: Path,
    accepted_run_id: str,
    snapshot_id: str,
    accepted_manifest_digest: str,
) -> None:
    validate_snapshot_id(snapshot_id)
    if not accepted_run_id or "/" in accepted_run_id or "\\" in accepted_run_id:
        raise ValueError("accepted run identity is unsafe")
    workspace = run_staging.parent.parent
    snapshot = (
        workspace / "outputs" / accepted_run_id / "snapshots" / "crm" / "activities" / snapshot_id
    )
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise ValueError("accepted activity snapshot is missing or unsafe")
    evidence = dict(verify_snapshot(snapshot))
    if evidence.get("manifest_digest") != accepted_manifest_digest:
        raise ValueError("accepted manifest changed before verification")
    evidence["accepted_run_id"] = accepted_run_id
    evidence["accepted_manifest_digest"] = accepted_manifest_digest
    write_verification(run_staging, evidence)
