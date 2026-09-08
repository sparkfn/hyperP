"""Safety contracts for bounded, durable CRM activity checkpoints."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from intelligence.artifacts import canonical_json
from intelligence.crm.activities import checkpoint_storage, checkpoints
from intelligence.crm.activities.checkpoint_limits import CheckpointLimits
from intelligence.crm.activities.models import (
    ArchiveRecord,
    ArchiveRequest,
    Disposition,
    ParentReference,
)
from intelligence.crm.activities.reconciliation import seal


def _limits() -> CheckpointLimits:
    return CheckpointLimits(1_000_000, 100)


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
        "2026-09-01T00:00:00Z",
        None,
        ParentReference(None, "bitrix-primary", "deal-a", "crm_deal", "STORED_PARENT"),
        (),
        (),
        (),
        (),
    )


def _root(tmp_path: Path, limits: CheckpointLimits | None = None) -> tuple[Path, ArchiveRequest]:
    run = tmp_path / "staging" / "run-a"
    run.mkdir(parents=True)
    request = ArchiveRequest("checkpoint-a", "bitrix-primary")
    return checkpoints.checkpoint_root(run, request.snapshot_id, limits or _limits()), request


def _usage(root: Path) -> tuple[int, int]:
    paths = tuple(root.rglob("*"))
    return sum(path.lstat().st_size for path in paths if path.is_file()), len(paths)


def _page(boundary_digest: str, record: ArchiveRecord) -> dict[str, object]:
    page: dict[str, object] = {
        "schema_version": "crm-activities-checkpoint-page-v1",
        "request_digest": checkpoints.sha256_json(
            ArchiveRequest("checkpoint-a", "bitrix-primary").as_public_dict()
        ),
        "boundary_digest": boundary_digest,
        "identities": [record.source_record_pk],
        "records": [record.as_dict()],
        "dispositions": [Disposition(record.source_record_pk, "accepted", None).__dict__],
    }
    page["digest"] = checkpoints.sha256_json(page)
    return page


def test_exact_limit_success_and_projected_byte_and_entry_rejections(tmp_path: Path) -> None:
    root, request = _root(tmp_path)
    request_payload = canonical_json(request.as_public_dict()).encode("utf-8")
    state_payload = canonical_json(
        {
            "schema_version": "crm-activities-checkpoint-v1",
            "phase": "new",
            "request_digest": checkpoints.sha256_json(request.as_public_dict()),
        }
    ).encode("utf-8")
    exact = CheckpointLimits(len(request_payload) + len(state_payload), 4)
    checkpoints.initialize(root, request, exact)
    checkpoints.bounded_usage(root, exact)

    payload = {"value": "bounded"}
    payload_bytes = len(canonical_json(payload).encode("utf-8"))
    bytes_used, entries_used = _usage(root)
    byte_limited = CheckpointLimits(bytes_used + payload_bytes - 1, entries_used + 1)
    with pytest.raises(RuntimeError, match="byte ceiling"):
        checkpoints.write_evidence(root, "byte-rejected.json", payload, byte_limited)
    assert not (root / "byte-rejected.json").exists()

    entry_limited = CheckpointLimits(1_000_000, entries_used)
    with pytest.raises(RuntimeError, match="entry ceiling"):
        checkpoints.write_evidence(root, "entry-rejected.json", payload, entry_limited)
    assert not (root / "entry-rejected.json").exists()


def test_reads_reject_existing_over_budget_checkpoint(tmp_path: Path) -> None:
    root, request = _root(tmp_path)
    checkpoints.initialize(root, request, _limits())
    (root / "existing-evidence.json").write_text("x" * 100, encoding="utf-8")
    with pytest.raises(RuntimeError, match="byte ceiling"):
        checkpoints.load_request(root, CheckpointLimits(1, 100))


def test_failed_publish_has_no_final_file_and_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, request = _root(tmp_path)
    limits = _limits()
    checkpoints.initialize(root, request, limits)
    original_link = os.link

    def fail_link(
        source: str | bytes | Path, destination: str | bytes | Path, *args: object
    ) -> None:
        del source, destination, args
        raise OSError("forced link failure")

    monkeypatch.setattr(checkpoint_storage.os, "link", fail_link)
    with pytest.raises(OSError, match="forced link failure"):
        checkpoints.write_evidence(root, "retry.json", {"value": "retry"}, limits)
    assert not (root / "retry.json").exists()
    assert checkpoints.state(root, limits)["phase"] == "new"
    monkeypatch.setattr(checkpoint_storage.os, "link", original_link)
    checkpoints.write_evidence(root, "retry.json", {"value": "retry"}, limits)
    assert checkpoints.read_evidence(root, "retry.json", limits) == {"value": "retry"}


def test_recognized_abandoned_temp_is_removed_but_unsafe_temp_is_rejected(tmp_path: Path) -> None:
    root, request = _root(tmp_path)
    limits = _limits()
    checkpoints.initialize(root, request, limits)
    abandoned = root / f".request.json.{uuid.uuid4().hex}.tmp"
    abandoned.write_text("partial", encoding="utf-8")
    checkpoints.bounded_usage(root, limits)
    assert not abandoned.exists()

    unsafe = root / f".request.json.{uuid.uuid4().hex}.tmp"
    unsafe.mkdir()
    with pytest.raises(ValueError, match="unsafe temporary"):
        checkpoints.bounded_usage(root, limits)


def test_intermediate_symlink_is_rejected_when_supported(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    target = tmp_path / "target"
    target.mkdir()
    workspace.mkdir()
    try:
        (workspace / "staging").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")
    with pytest.raises(ValueError, match="unsafe"):
        checkpoints.checkpoint_root(
            workspace / "staging" / "run-a",
            "checkpoint-a",
            _limits(),
        )


def test_resume_rejects_contradictory_page_inventory_and_state(tmp_path: Path) -> None:
    root, request = _root(tmp_path)
    limits = _limits()
    record = _record()
    boundary = seal((record,), request)
    checkpoints.initialize(root, request, limits)
    checkpoints.write_record(root, record.source_record_pk, record.as_dict(), limits)
    checkpoints.write_boundary(root, boundary, limits)
    checkpoints.write_page(root, 1, _page(boundary.digest, record), limits)

    with pytest.raises(RuntimeError, match="inventory is incomplete"):
        checkpoints.validate_resume(root, request, boundary, limits)
