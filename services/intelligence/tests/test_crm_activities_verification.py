"""Tamper, bounded preflight, and status contracts for CRM activity evidence."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from intelligence.artifacts import canonical_json
from intelligence.crm.activities import checkpoints
from intelligence.crm.activities.bounded import ReadLimits, verify_snapshot_input
from intelligence.crm.activities.checkpoint_limits import CheckpointLimits
from intelligence.crm.activities.dispositions import classify
from intelligence.crm.activities.manifests import write_snapshot
from intelligence.crm.activities.models import (
    ArchiveRecord,
    ArchiveRequest,
    Disposition,
    ParentReference,
    sha256_json,
)
from intelligence.crm.activities.reconciliation import seal
from intelligence.crm.activities.snapshot_verifier import verify_snapshot
from intelligence.crm.activities.status import status
from intelligence.state import State
from intelligence.state_readonly import ReadOnlyState

_LIMITS = CheckpointLimits(max_bytes=1_000_000, max_entries=100)


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
    assert verify_snapshot(snapshot)["verified"] is True


def test_verifier_accepts_empty_current_schema_snapshot(tmp_path: Path) -> None:
    request = ArchiveRequest(
        "checkpoint-a",
        "bitrix-primary",
        database_identity="neo4j-primary",
        selection_contract_version="crm-activities-selection-v2",
        max_references_per_record=42,
    )
    boundary = seal((), request)
    staging = tmp_path / "staging" / "empty-run"
    staging.mkdir(parents=True)
    write_snapshot(staging, boundary, (), ())
    snapshot = staging / "snapshots" / "crm" / "activities" / boundary.logical_snapshot_id
    assert verify_snapshot(snapshot)["verified"] is True


def test_verifier_rejects_accepted_unknown_lifecycle(tmp_path: Path) -> None:
    record = replace(_record(), lifecycle_status="unrecognized_lifecycle")
    boundary = seal((record,), ArchiveRequest("checkpoint-a", "bitrix-primary"))
    staging = tmp_path / "staging" / "unknown-accepted"
    staging.mkdir(parents=True)
    write_snapshot(
        staging,
        boundary,
        (record,),
        (Disposition(record.source_record_pk, "accepted", None),),
    )
    snapshot = staging / "snapshots" / "crm" / "activities" / boundary.logical_snapshot_id
    with pytest.raises(ValueError, match="closed classification"):
        verify_snapshot(snapshot)


def test_verifier_rejects_accepted_invalid_companion(tmp_path: Path) -> None:
    activity = _record("activity-a")
    child = ParentReference(
        "activity-a",
        "bitrix-primary",
        "record-activity-a",
        "crm_history",
        "CHILD_OF",
    )
    conflicting_details = ParentReference(
        "activity-b",
        "bitrix-primary",
        "record-activity-b",
        "crm_history",
        "DETAILS_HISTORY_ITEM",
    )
    call = replace(
        _record("call-a"),
        record_type="call",
        child_parents=(child,),
        details_parents=(conflicting_details,),
        stored_parent=ParentReference(
            None,
            "bitrix-primary",
            "record-activity-a",
            "crm_history",
            "STORED_PARENT",
        ),
    )
    records = (activity, call)
    boundary = seal(records, ArchiveRequest("checkpoint-a", "bitrix-primary"))
    staging = tmp_path / "staging" / "invalid-accepted-companion"
    staging.mkdir(parents=True)
    write_snapshot(
        staging,
        boundary,
        records,
        tuple(Disposition(record.source_record_pk, "accepted", None) for record in records),
    )
    snapshot = staging / "snapshots" / "crm" / "activities" / boundary.logical_snapshot_id
    with pytest.raises(ValueError, match="closed classification"):
        verify_snapshot(snapshot)


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
    if mutation == "missing":
        with pytest.raises(ValueError, match="missing"):
            verify_snapshot(snapshot)
    else:
        with pytest.raises(ValueError):
            verify_snapshot(snapshot)


def test_verifier_rejects_closed_classification_tamper(tmp_path: Path) -> None:
    record = replace(_record(), lifecycle_status="unrecognized_lifecycle")
    boundary = seal((record,), ArchiveRequest("checkpoint-a", "bitrix-primary"))
    staging = tmp_path / "staging" / "unknown-lifecycle"
    staging.mkdir(parents=True)
    write_snapshot(staging, boundary, (record,), classify((record,)))
    snapshot = staging / "snapshots" / "crm" / "activities" / boundary.logical_snapshot_id
    rejected_path = snapshot / "rejected.json"
    rejected = json.loads(rejected_path.read_text(encoding="utf-8"))
    rejected["records"][0]["reason_code"] = "wrong_reason"
    rejected["digest"] = sha256_json(rejected["records"])
    rejected_path.write_bytes(canonical_json(rejected).encode("utf-8"))
    with pytest.raises(ValueError, match="closed classification"):
        verify_snapshot(snapshot)


def test_verifier_rejects_link_evidence_when_supported(tmp_path: Path) -> None:
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


def test_untrusted_manifest_cannot_expand_preverification_row_ceiling(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "manifest.json").write_bytes(
        canonical_json({"selected_count": 1_000_000}).encode("utf-8")
    )
    (snapshot / "records.json").write_bytes(
        canonical_json({"records": [{"row": 1}, {"row": 2}]}).encode("utf-8")
    )
    with pytest.raises(RuntimeError, match="row ceiling"):
        verify_snapshot_input(snapshot, ReadLimits(10_000, 10, 1), 0)


def test_descriptor_proven_row_ceiling_allows_2501_rows(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    rows = [{"source_record_pk": f"record-{index:04d}"} for index in range(2_501)]
    (snapshot / "manifest.json").write_bytes(canonical_json({"selected_count": 1}).encode("utf-8"))
    (snapshot / "records.json").write_bytes(canonical_json({"records": rows}).encode("utf-8"))
    verify_snapshot_input(snapshot, ReadLimits(10_000_000, 10, 1), 2_501)


def test_status_new_and_corrupt_evidence_are_explicit(tmp_path: Path) -> None:
    assert status(tmp_path, "checkpoint-a")["state"] == "absent"
    run = tmp_path / "staging" / "run-a"
    run.mkdir(parents=True)
    root = checkpoints.checkpoint_root(run, "checkpoint-a", _LIMITS)
    request = ArchiveRequest("checkpoint-a", "bitrix-primary")
    checkpoints.initialize(root, request, _LIMITS)
    checkpoints.write_evidence(root, "duplicate-deliveries.json", {"count": 3}, _LIMITS)
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    result = status(tmp_path, "checkpoint-a")
    after = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    assert result["state"]["phase"] == "new"
    assert result["state_store"] == "absent"
    assert result["duplicate_delivery_count"] == 3
    assert after == before
    (root / "dispositions.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt"):
        status(tmp_path, "checkpoint-a")


def test_status_existing_state_does_not_create_wal_sidecars(tmp_path: Path) -> None:
    state = State(tmp_path)
    state.close()
    database = tmp_path / "state" / "state.sqlite3"
    sidecars = tuple(database.with_name(f"{database.name}{suffix}") for suffix in ("-wal", "-shm"))
    for path in sidecars:
        if path.exists():
            path.unlink()
    run = tmp_path / "staging" / "run-a"
    run.mkdir(parents=True)
    root = checkpoints.checkpoint_root(run, "checkpoint-a", _LIMITS)
    request = ArchiveRequest("checkpoint-a", "bitrix-primary")
    checkpoints.initialize(root, request, _LIMITS)
    assert status(tmp_path, "checkpoint-a")["state_store"] == "available"
    assert database.exists()
    assert not any(path.exists() for path in sidecars)


def test_immutable_readonly_state_detects_database_change_before_close(tmp_path: Path) -> None:
    state = State(tmp_path)
    state.close()
    database = tmp_path / "state" / "state.sqlite3"
    sidecars = tuple(database.with_name(f"{database.name}{suffix}") for suffix in ("-wal", "-shm"))
    for path in sidecars:
        if path.exists():
            path.unlink()
    reader = ReadOnlyState.open(tmp_path)
    writer = State(tmp_path)
    writer.create_mutating_run("concurrent-test")
    writer.close()
    with pytest.raises(RuntimeError, match="changed during status read"):
        reader.close()
    assert not any(path.exists() for path in sidecars)


def test_status_rejects_unsafe_optional_evidence(tmp_path: Path) -> None:
    run = tmp_path / "staging" / "run-a"
    run.mkdir(parents=True)
    root = checkpoints.checkpoint_root(run, "checkpoint-a", _LIMITS)
    checkpoints.initialize(root, ArchiveRequest("checkpoint-a", "bitrix-primary"), _LIMITS)
    (root / "dispositions.json").mkdir()
    with pytest.raises(ValueError, match="unsafe"):
        status(tmp_path, "checkpoint-a")
