"""Tamper and status contracts for CRM activity archive evidence."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest
from intelligence.crm.activities import checkpoints
from intelligence.crm.activities.cli import (
    _acceptance_from_checkpoint,
    _assert_registered_snapshot,
    _status,
    add_parser,
)
from intelligence.crm.activities.dispositions import classify
from intelligence.crm.activities.manifests import verify_snapshot, write_snapshot
from intelligence.crm.activities.models import (
    ArchiveRecord,
    ArchiveRequest,
    ParentReference,
    sha256_json,
)
from intelligence.crm.activities.reconciliation import seal
from intelligence.models import OutputInventory


def _record(identity: str = "activity-a") -> ArchiveRecord:
    return ArchiveRecord(
        identity,
        f"record-{identity}",
        "1",
        f"version-{identity}",
        f"hash-{identity}",
        "bitrix-primary",
        "bitrix_chat",
        "crm_history",
        "active",
        "activity",
        "call",
        "bitrix_crm_activity",
        "2",
        "bitrix_crm_activity_v2",
        None,
        "2026-09-08T00:00:00Z",
        None,
        ParentReference(None, "bitrix-primary", "deal-a", "crm_deal", "STORED_PARENT"),
        (),
        (),
        (),
        (),
        None,
    )


def _snapshot(tmp_path: Path) -> tuple[Path, ArchiveRequest]:
    request = ArchiveRequest("checkpoint-a", "bitrix-primary")
    boundary = seal((_record(),), request)
    staging = tmp_path / "staging" / "run-a"
    staging.mkdir(parents=True)
    write_snapshot(staging, boundary, (_record(),), classify((_record(),)))
    return staging / "snapshots" / "crm" / "activities" / boundary.logical_snapshot_id, request


def test_verifier_accepts_complete_canonical_snapshot(tmp_path: Path) -> None:
    snapshot, _ = _snapshot(tmp_path)
    evidence = verify_snapshot(snapshot)
    assert evidence["verified"] is True


@pytest.mark.parametrize("mutation", ("extra", "missing", "noncanonical", "tamper"))
def test_verifier_rejects_inventory_and_content_tampering(tmp_path: Path, mutation: str) -> None:
    snapshot, _ = _snapshot(tmp_path)
    if mutation == "extra":
        (snapshot / "extra.json").write_text("{}", encoding="utf-8")
    elif mutation == "missing":
        (snapshot / "rejected.json").unlink()
    elif mutation == "noncanonical":
        path = snapshot / "manifest.json"
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    else:
        path = snapshot / "cleanup-identities.json"
        content = path.read_text(encoding="utf-8").replace("activity-a", "activity-b")
        path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        verify_snapshot(snapshot)


def test_verifier_rejects_link_evidence_and_cleanup_mismatch(tmp_path: Path) -> None:
    snapshot, _ = _snapshot(tmp_path)
    target = snapshot / "records" / "page-00000001.json"
    link = snapshot / "link.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ValueError, match="unsafe|inventory"):
        verify_snapshot(snapshot)


def test_verifier_rejects_hardlinked_evidence_when_supported(tmp_path: Path) -> None:
    snapshot, _ = _snapshot(tmp_path)
    source = snapshot / "manifest.json"
    target = snapshot / "manifest-copy.json"
    try:
        os.link(source, target)
    except OSError:
        pytest.skip("hardlink creation is unavailable")
    with pytest.raises(ValueError, match="unsafe|inventory"):
        verify_snapshot(snapshot)


def test_status_lifecycle_and_corruption_are_explicit(tmp_path: Path) -> None:
    assert _status(tmp_path, "checkpoint-a")["state"] == "absent"
    run = tmp_path / "staging" / "run-a"
    run.mkdir(parents=True)
    root = checkpoints.checkpoint_root(run, "checkpoint-a")
    request = ArchiveRequest("checkpoint-a", "bitrix-primary")
    checkpoints.initialize(root, request)
    assert _status(tmp_path, "checkpoint-a")["state"]["phase"] == "new"
    boundary = seal((_record(),), request)
    checkpoints.write_record(root, "activity-a", _record().as_dict())
    checkpoints.write_boundary(root, boundary)
    assert _status(tmp_path, "checkpoint-a")["state"]["phase"] == "sealed"
    checkpoints.advance(root, boundary.digest, 1)
    status = _status(tmp_path, "checkpoint-a")
    assert status["source_instance_id"] == "bitrix-primary"
    assert status["state"]["phase"] == "paging"
    assert status["parent_resolution"]["resolved"] == 1
    assert status["person_resolution"]["missing_or_ambiguous"] == 1
    (root / "checkpoint.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt"):
        _status(tmp_path, "checkpoint-a")


def test_status_completed_accepted_and_verified_evidence(tmp_path: Path) -> None:
    run = tmp_path / "staging" / "run-a"
    run.mkdir(parents=True)
    root = checkpoints.checkpoint_root(run, "checkpoint-a")
    request = ArchiveRequest("checkpoint-a", "bitrix-primary")
    boundary = seal((_record(),), request)
    checkpoints.initialize(root, request)
    checkpoints.write_record(root, "activity-a", _record().as_dict())
    checkpoints.write_boundary(root, boundary)
    checkpoints.complete(root, boundary.digest, 1)
    manifest = write_snapshot(run, boundary, (_record(),), classify((_record(),)))
    outcome = {"source_record_pk": "activity-a", "disposition": "accepted", "reason_code": None}
    checkpoints.write_evidence(
        root,
        "dispositions.json",
        {"outcomes": [outcome], "digest": sha256_json([outcome])},
    )
    checkpoints.write_evidence(root, "accepted-manifest.json", manifest)
    checkpoints.write_evidence(
        root,
        "acceptance.json",
        {
            "run_id": "run-1",
            "checkpoint_id": "checkpoint-a",
            "snapshot_id": manifest["snapshot_id"],
            "manifest_digest": manifest["digest"],
            "cleanup_identity_digest": manifest["cleanup_identity_digest"],
            "outputs": [],
        },
    )
    checkpoints.write_evidence(
        root,
        "verification.json",
        {
            "verification_run_id": "run-2",
            "accepted_run_id": "run-1",
            "accepted_manifest_digest": manifest["digest"],
            "artifact": {
                "relative_path": "outputs/run-2/verifications/crm/activities/snapshot.json",
                "sha256": "c" * 64,
                "byte_count": 1,
            },
        },
    )
    status = _status(tmp_path, "checkpoint-a")
    assert status["state"]["phase"] == "completed"
    assert status["accepted_run"]["run_id"] == "run-1"
    assert status["verification"]["verification_run_id"] == "run-2"


def test_accepted_output_inventory_mismatch_is_rejected(tmp_path: Path) -> None:
    snapshot, _ = _snapshot(tmp_path)

    class State:
        def accepted_outputs(self, _run_id: str) -> tuple[OutputInventory, ...]:
            return ()

    class Config:
        workspace = tmp_path

    class Runtime:
        state = State()
        config = Config()

    with pytest.raises(RuntimeError, match="inventory"):
        _assert_registered_snapshot(Runtime(), "run-a", "snapshot-a", snapshot)


def test_status_rejects_unsafe_optional_evidence(tmp_path: Path) -> None:
    run = tmp_path / "staging" / "run-a"
    run.mkdir(parents=True)
    root = checkpoints.checkpoint_root(run, "checkpoint-a")
    checkpoints.initialize(root, ArchiveRequest("checkpoint-a", "bitrix-primary"))
    (root / "dispositions.json").mkdir()
    with pytest.raises(ValueError, match="unsafe"):
        _status(tmp_path, "checkpoint-a")


def test_status_rejects_corrupt_optional_evidence(tmp_path: Path) -> None:
    run = tmp_path / "staging" / "run-a"
    run.mkdir(parents=True)
    root = checkpoints.checkpoint_root(run, "checkpoint-a")
    checkpoints.initialize(root, ArchiveRequest("checkpoint-a", "bitrix-primary"))
    (root / "dispositions.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt"):
        _status(tmp_path, "checkpoint-a")


def test_checkpoint_derived_verification_admission_and_parser(tmp_path: Path) -> None:
    run = tmp_path / "staging" / "run-a"
    run.mkdir(parents=True)
    root = checkpoints.checkpoint_root(run, "checkpoint-a")
    request = ArchiveRequest("checkpoint-a", "bitrix-primary")
    boundary = seal((_record(),), request)
    checkpoints.initialize(root, request)
    manifest = write_snapshot(run, boundary, (_record(),), classify((_record(),)))
    acceptance = {
        "checkpoint_id": "checkpoint-a",
        "run_id": "accepted-run",
        "snapshot_id": manifest["snapshot_id"],
        "manifest_digest": manifest["digest"],
        "cleanup_identity_digest": manifest["cleanup_identity_digest"],
        "outputs": [],
    }
    checkpoints.write_evidence(root, "accepted-manifest.json", manifest)
    checkpoints.write_evidence(root, "acceptance.json", acceptance)
    assert _acceptance_from_checkpoint(tmp_path, "checkpoint-a") == acceptance
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers(dest="crm", required=True))
    parsed = parser.parse_args(("activities", "verify", "--checkpoint-id", "checkpoint-a"))
    assert parsed.checkpoint_id == "checkpoint-a"
