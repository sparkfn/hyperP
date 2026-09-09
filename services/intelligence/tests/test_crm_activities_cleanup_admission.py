"""State-backed, bounded admission tests for accepted CRM activity archives."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from intelligence.artifacts import canonical_json
from intelligence.artifacts_staging import scan_staged_outputs
from intelligence.crm.activities.acceptance import (
    parse_descriptor,
    publication_descriptor,
    write_publication_descriptor,
)
from intelligence.crm.activities.cleanup import admission
from intelligence.crm.activities.cleanup.models import (
    CleanupAuthorization,
    CleanupRequest,
    CleanupTarget,
)
from intelligence.crm.activities.dispositions import classify
from intelligence.crm.activities.manifests import write_snapshot
from intelligence.crm.activities.models import ArchiveRecord, ArchiveRequest, ParentReference
from intelligence.crm.activities.reconciliation import seal
from intelligence.models import OutputInventory


def _record() -> ArchiveRecord:
    return ArchiveRecord(
        "activity-a",
        "record-a",
        "1",
        "version-a",
        "hash-a",
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


def _published_archive(
    tmp_path: Path,
) -> tuple[object, object, CleanupRequest, tuple[OutputInventory, ...]]:
    request = ArchiveRequest("checkpoint-a", "bitrix-primary")
    record = _record()
    boundary = seal((record,), request)
    staging = tmp_path / "staging" / "archive-run"
    staging.mkdir(parents=True)
    manifest = write_snapshot(staging, boundary, (record,), classify((record,)))
    inventory = scan_staged_outputs(tmp_path, staging.name, 1_000_000, 100)
    descriptor = publication_descriptor(
        request.snapshot_id,
        request,
        boundary.digest,
        staging.name,
        "crm_activities_extract",
        boundary.logical_snapshot_id,
        str(manifest["digest"]),
        str(manifest["cleanup_identity_digest"]),
        inventory,
    )
    pointer = write_publication_descriptor(staging, descriptor)
    shutil.copytree(staging, tmp_path / "outputs" / staging.name)
    outputs = tuple(
        OutputInventory(
            f"outputs/{staging.name}/{item.relative_path}", item.sha256, item.byte_count
        )
        for item in scan_staged_outputs(tmp_path, staging.name, 1_000_000, 100)
    )
    cleanup = CleanupRequest(
        CleanupAuthorization(
            request.snapshot_id, staging.name, boundary.logical_snapshot_id, str(manifest["digest"])
        ),
        CleanupTarget("environment-a", "database-a"),
        1,
    )
    return parse_descriptor(descriptor), pointer, cleanup, outputs


def _runtime(tmp_path: Path, outputs: tuple[OutputInventory, ...]) -> object:
    return SimpleNamespace(
        config=SimpleNamespace(workspace=tmp_path),
        state=SimpleNamespace(accepted_outputs=lambda _run: outputs),
    )


def test_admission_accepts_real_354_snapshot_descriptor_and_state_inventory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    descriptor, pointer, request, outputs = _published_archive(tmp_path)
    monkeypatch.setattr(
        admission, "accepted_publication_for_run", lambda *_args: (descriptor, pointer)
    )
    admitted = admission.admit(_runtime(tmp_path, outputs), request)
    assert admitted.descriptor == descriptor
    assert admitted.pointer == pointer
    assert [item["source_record_pk"] for item in admitted.identities] == ["activity-a"]


def test_admission_rejects_locator_conflict_before_snapshot_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    descriptor = SimpleNamespace(snapshot_id="snapshot-a", manifest_digest="b" * 64)
    monkeypatch.setattr(
        admission, "accepted_publication_for_run", lambda *_args: (descriptor, object())
    )
    request = CleanupRequest(
        CleanupAuthorization("checkpoint-a", "run-a", "snapshot-a", "a" * 64),
        CleanupTarget("environment-a", "database-a"),
        1,
    )
    with pytest.raises(RuntimeError, match="locators conflict"):
        admission.admit(
            SimpleNamespace(config=SimpleNamespace(workspace=tmp_path), state=object()), request
        )


@pytest.mark.parametrize("canonical", (True, False))
def test_admission_rejects_oversized_artifact_before_unbounded_verification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, canonical: bool
) -> None:
    descriptor, pointer, request, outputs = _published_archive(tmp_path)
    monkeypatch.setattr(
        admission, "accepted_publication_for_run", lambda *_args: (descriptor, pointer)
    )
    snapshot = (
        tmp_path
        / "outputs"
        / descriptor.run_id
        / "snapshots"
        / "crm"
        / "activities"
        / descriptor.snapshot_id
    )
    target = snapshot / "records" / "page-00000001.json"
    target.write_bytes(
        canonical_json({"records": ["x" * 20_000]}).encode("utf-8") if canonical else b"x" * 20_000
    )
    monkeypatch.setattr(
        admission,
        "verify_snapshot",
        lambda _snapshot: (_ for _ in ()).throw(AssertionError("unbounded verifier must not run")),
    )
    with pytest.raises(RuntimeError, match="byte ceiling"):
        admission.admit(_runtime(tmp_path, outputs), request)
