"""Read-only CRM activity checkpoint status and evidence validation."""

from __future__ import annotations

import stat
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from intelligence.artifacts import canonical_json
from intelligence.crm.activities import checkpoints
from intelligence.crm.activities.models import (
    Disposition,
    DispositionKind,
    SealedBoundary,
    parse_boundary,
    record_from_mapping,
    sha256_json,
    validate_snapshot_id,
)


def status(workspace: Path, checkpoint_id: str) -> dict[str, object]:
    validate_snapshot_id(checkpoint_id)
    root = workspace / "staging" / ".crm-activities" / checkpoint_id
    if root.is_symlink() or not root.is_dir():
        return {"checkpoint_id": checkpoint_id, "state": "absent"}
    state = checkpoints.state(root)
    boundary = (
        parse_boundary(checkpoints.load_boundary(root))
        if (root / "boundary.json").exists()
        else None
    )
    dispositions = safe_evidence(root, "dispositions.json")
    acceptance = safe_evidence(root, "acceptance.json")
    verification = safe_evidence(root, "verification.json")
    manifest = safe_evidence(root, "accepted-manifest.json")
    outcomes = _validate_dispositions(dispositions, boundary)
    _validate_manifest(manifest, boundary)
    _validate_acceptance(acceptance, manifest, checkpoint_id)
    _validate_verification(verification, acceptance)
    outcome_counts = {
        kind: sum(
            1 for item in outcomes if isinstance(item, dict) and item.get("disposition") == kind
        )
        for kind in ("accepted", "rejected", "quarantined")
    }
    records = tuple(record_from_mapping(item) for item in checkpoints.records(root))
    parent_summary = {
        "missing": sum(1 for item in records if item.stored_parent.source_record_id is None),
        "resolved": sum(1 for item in records if item.stored_parent.source_record_id is not None),
    }
    person_summary = {
        "missing_or_ambiguous": sum(1 for item in records if len(item.people) != 1),
        "missing_revision": sum(
            1 for item in records if len(item.people) == 1 and item.people[0].revision is None
        ),
    }
    source_instance = None if boundary is None else boundary.request.source_instance_id
    return {
        "checkpoint_id": checkpoint_id,
        "snapshot_id": None if manifest is None else manifest.get("snapshot_id"),
        "source_instance_id": source_instance,
        "state": state,
        "boundary_digest": None if boundary is None else boundary.digest,
        "disposition_counts": outcome_counts,
        "duplicate_delivery_count": 0,
        "parent_resolution": parent_summary,
        "person_resolution": person_summary,
        "accepted_run": acceptance,
        "manifest_digest": None if manifest is None else manifest.get("digest"),
        "cleanup_identity_digest": (
            None if manifest is None else manifest.get("cleanup_identity_digest")
        ),
        "verification": verification,
    }


def safe_evidence(root: Path, name: str) -> dict[str, object] | None:
    path = root / name
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ValueError("optional checkpoint evidence is unsafe")
    value = checkpoints.read_evidence(root, name)
    raw = path.read_bytes()
    if raw != canonical_json(dict(value)).encode("utf-8"):
        raise ValueError("optional checkpoint evidence is noncanonical")
    return dict(value)


def _validate_dispositions(
    evidence: dict[str, object] | None, boundary: SealedBoundary | None
) -> list[dict[str, object]]:
    if evidence is None:
        return []
    if set(evidence) != {"outcomes", "digest"}:
        raise ValueError("disposition evidence schema is invalid")
    outcomes = evidence.get("outcomes")
    if not isinstance(outcomes, list) or evidence.get("digest") != sha256_json(outcomes):
        raise ValueError("disposition evidence digest is invalid")
    parsed: list[dict[str, object]] = []
    for item in outcomes:
        if not isinstance(item, dict):
            raise ValueError("disposition evidence item is invalid")
        disposition = Disposition(
            _required_text(item, "source_record_pk"),
            cast(DispositionKind, _required_text(item, "disposition")),
            item.get("reason_code") if isinstance(item.get("reason_code"), str) else None,
        )
        if item != disposition.__dict__:
            raise ValueError("disposition evidence item is noncanonical")
        parsed.append(dict(item))
    ids = [item["source_record_pk"] for item in parsed]
    if ids != sorted(set(cast(list[str], ids))):
        raise ValueError("disposition evidence is not sorted unique")
    if boundary is not None and set(ids) != {entry.source_record_pk for entry in boundary.entries}:
        raise ValueError("disposition evidence does not equal the sealed boundary")
    return parsed


def _validate_manifest(
    manifest: dict[str, object] | None,
    boundary: SealedBoundary | None,
) -> None:
    if manifest is None:
        return
    expected = {
        "schema_version",
        "snapshot_id",
        "boundary_digest",
        "selected_count",
        "accepted_count",
        "rejected_count",
        "quarantined_count",
        "unexplained_remainder",
        "record_page_digests",
        "cleanup_identity_count",
        "cleanup_identity_digest",
        "provenance",
        "digest",
    }
    if set(manifest) != expected or boundary is None or manifest.get("digest") is None:
        raise ValueError("accepted manifest linkage is invalid")
    unsigned = dict(manifest)
    digest = unsigned.pop("digest")
    if not isinstance(digest, str) or sha256_json(unsigned) != digest:
        raise ValueError("accepted manifest digest is invalid")
    if (
        manifest.get("snapshot_id") != boundary.logical_snapshot_id
        or manifest.get("boundary_digest") != boundary.digest
    ):
        raise ValueError("accepted manifest boundary linkage is invalid")


def _validate_acceptance(
    acceptance: dict[str, object] | None, manifest: dict[str, object] | None, checkpoint_id: str
) -> None:
    if acceptance is None:
        return
    required = {
        "checkpoint_id",
        "run_id",
        "snapshot_id",
        "manifest_digest",
        "cleanup_identity_digest",
        "outputs",
    }
    if set(acceptance) != required or acceptance.get("checkpoint_id") != checkpoint_id:
        raise ValueError("acceptance linkage schema is invalid")
    if (
        manifest is None
        or acceptance.get("snapshot_id") != manifest.get("snapshot_id")
        or acceptance.get("manifest_digest") != manifest.get("digest")
        or acceptance.get("cleanup_identity_digest") != manifest.get("cleanup_identity_digest")
    ):
        raise ValueError("acceptance linkage is inconsistent")
    if not isinstance(acceptance.get("run_id"), str) or not isinstance(
        acceptance.get("outputs"), list
    ):
        raise ValueError("acceptance linkage is invalid")
    for output in cast(list[object], acceptance["outputs"]):
        expected_keys = {"relative_path", "sha256", "byte_count"}
        if not isinstance(output, dict) or set(output) != expected_keys:
            raise ValueError("acceptance output inventory is invalid")
        if (
            not isinstance(output.get("relative_path"), str)
            or not isinstance(output.get("sha256"), str)
            or not isinstance(output.get("byte_count"), int)
        ):
            raise ValueError("acceptance output inventory is invalid")


def _validate_verification(
    verification: dict[str, object] | None, acceptance: dict[str, object] | None
) -> None:
    if verification is None:
        return
    required = {"verification_run_id", "accepted_run_id", "accepted_manifest_digest", "artifact"}
    if set(verification) != required or acceptance is None:
        raise ValueError("verification linkage schema is invalid")
    if verification.get("accepted_run_id") != acceptance.get("run_id") or verification.get(
        "accepted_manifest_digest"
    ) != acceptance.get("manifest_digest"):
        raise ValueError("verification linkage is inconsistent")
    if not isinstance(verification.get("verification_run_id"), str) or not isinstance(
        verification.get("artifact"), dict
    ):
        raise ValueError("verification linkage is invalid")
    artifact = cast(dict[str, object], verification["artifact"])
    if set(artifact) != {"relative_path", "sha256", "byte_count"}:
        raise ValueError("verification artifact linkage is invalid")
    if (
        not isinstance(artifact.get("relative_path"), str)
        or not isinstance(artifact.get("sha256"), str)
        or not isinstance(artifact.get("byte_count"), int)
    ):
        raise ValueError("verification artifact linkage is invalid")


def _acceptance_from_checkpoint(workspace: Path, checkpoint_id: str) -> dict[str, object]:
    root = workspace / "staging" / ".crm-activities" / checkpoint_id
    if root.is_symlink() or not root.is_dir():
        raise ValueError("checkpoint is missing or unsafe")
    acceptance = safe_evidence(root, "acceptance.json")
    manifest = safe_evidence(root, "accepted-manifest.json")
    _validate_acceptance(acceptance, manifest, checkpoint_id)
    if acceptance is None:
        raise RuntimeError("checkpoint has no accepted archive linkage")
    return acceptance


def _required_text(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"{key} is required")
    return result
