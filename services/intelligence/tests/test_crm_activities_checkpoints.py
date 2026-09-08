"""Safety contracts for bounded, durable CRM activity checkpoints."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from intelligence.artifacts import canonical_json
from intelligence.crm.activities import checkpoint_atomic, checkpoint_usage, checkpoints
from intelligence.crm.activities.checkpoint_limits import CheckpointLimits
from intelligence.crm.activities.models import (
    ArchiveRecord,
    ArchiveRequest,
    Disposition,
    ParentReference,
)
from intelligence.crm.activities.reconciliation import seal


def _limits() -> CheckpointLimits:
    return CheckpointLimits(max_bytes=1_000_000, max_entries=100)


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


def _root(
    tmp_path: Path,
    limits: CheckpointLimits | None = None,
    request: ArchiveRequest | None = None,
) -> tuple[Path, ArchiveRequest]:
    run = tmp_path / "staging" / "run-a"
    run.mkdir(parents=True)
    archive_request = request or ArchiveRequest("checkpoint-a", "bitrix-primary")
    return (
        checkpoints.checkpoint_root(run, archive_request.snapshot_id, limits or _limits()),
        archive_request,
    )


def _usage(root: Path) -> tuple[int, int]:
    paths = tuple(root.rglob("*"))
    return sum(path.lstat().st_size for path in paths if path.is_file()), len(paths)


def _page(
    boundary_digest: str,
    record: ArchiveRecord,
    request: ArchiveRequest | None = None,
) -> dict[str, object]:
    archive_request = request or ArchiveRequest("checkpoint-a", "bitrix-primary")
    page: dict[str, object] = {
        "schema_version": "crm-activities-checkpoint-page-v1",
        "request_digest": checkpoints.sha256_json(archive_request.as_public_dict()),
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
    exact = CheckpointLimits(max_bytes=len(request_payload) + len(state_payload), max_entries=4)
    checkpoints.initialize(root, request, exact)
    checkpoints.bounded_usage(root, exact)

    payload = {"value": "bounded"}
    payload_bytes = len(canonical_json(payload).encode("utf-8"))
    bytes_used, entries_used = _usage(root)
    byte_limited = CheckpointLimits(
        max_bytes=bytes_used + payload_bytes - 1,
        max_entries=entries_used + 1,
    )
    with pytest.raises(RuntimeError, match="byte ceiling"):
        checkpoints.write_evidence(root, "byte-rejected.json", payload, byte_limited)
    assert not (root / "byte-rejected.json").exists()

    entry_limited = CheckpointLimits(max_bytes=1_000_000, max_entries=entries_used)
    with pytest.raises(RuntimeError, match="entry ceiling"):
        checkpoints.write_evidence(root, "entry-rejected.json", payload, entry_limited)
    assert not (root / "entry-rejected.json").exists()


def test_reads_reject_existing_over_budget_checkpoint(tmp_path: Path) -> None:
    root, request = _root(tmp_path)
    checkpoints.initialize(root, request, _limits())
    (root / "existing-evidence.json").write_text("x" * 100, encoding="utf-8")
    with pytest.raises(RuntimeError, match="byte ceiling"):
        checkpoints.load_request(root, CheckpointLimits(max_bytes=1, max_entries=100))


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

    monkeypatch.setattr(checkpoint_atomic.os, "link", fail_link)
    with pytest.raises(OSError, match="forced link failure"):
        checkpoints.write_evidence(root, "retry.json", {"value": "retry"}, limits)
    assert not (root / "retry.json").exists()
    assert checkpoints.state(root, limits)["phase"] == "new"
    monkeypatch.setattr(checkpoint_atomic.os, "link", original_link)
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
    (root / "pages" / "page-00000002.json").write_bytes(
        canonical_json(_page(boundary.digest, record)).encode("utf-8")
    )

    with pytest.raises(RuntimeError, match="inventory is out of order"):
        checkpoints.validate_resume(root, request, boundary, limits)


def test_resume_accepts_one_durable_unadvanced_page_and_never_regresses_cursor(
    tmp_path: Path,
) -> None:
    request = ArchiveRequest("checkpoint-a", "bitrix-primary", page_size=1)
    root, request = _root(tmp_path, request=request)
    limits = _limits()
    first = _record("activity-a")
    second = _record("activity-b")
    boundary = seal((first, second), request)
    checkpoints.initialize(root, request, limits)
    checkpoints.write_record(root, first.source_record_pk, first.as_dict(), limits)
    checkpoints.write_record(root, second.source_record_pk, second.as_dict(), limits)
    checkpoints.write_boundary(root, boundary, limits)
    checkpoints.write_page(root, 1, _page(boundary.digest, first, request), limits)
    first_page = (root / "pages" / "page-00000001.json").read_bytes()

    assert checkpoints.validate_resume(root, request, boundary, limits) == 1
    assert checkpoints.state(root, limits)["phase"] == "sealed"
    assert checkpoints.resume_cursor(root, request, boundary, limits) == 1
    assert checkpoints.state(root, limits)["pages"] == 1
    assert (root / "pages" / "page-00000001.json").read_bytes() == first_page

    checkpoints.advance(root, boundary.digest, 1, limits)
    checkpoints.write_page(root, 2, _page(boundary.digest, second, request), limits)
    checkpoints.advance(root, boundary.digest, 2, limits)
    with pytest.raises(RuntimeError, match="cannot regress"):
        checkpoints.advance(root, boundary.digest, 1, limits)


def test_resume_rejects_more_than_one_unadvanced_page(tmp_path: Path) -> None:
    request = ArchiveRequest("checkpoint-a", "bitrix-primary", page_size=1)
    root, request = _root(tmp_path, request=request)
    limits = _limits()
    first = _record("activity-a")
    second = _record("activity-b")
    boundary = seal((first, second), request)
    checkpoints.initialize(root, request, limits)
    checkpoints.write_record(root, first.source_record_pk, first.as_dict(), limits)
    checkpoints.write_record(root, second.source_record_pk, second.as_dict(), limits)
    checkpoints.write_boundary(root, boundary, limits)
    checkpoints.write_page(root, 1, _page(boundary.digest, first, request), limits)
    (root / "pages" / "page-00000002.json").write_bytes(
        canonical_json(_page(boundary.digest, second, request)).encode("utf-8")
    )

    with pytest.raises(RuntimeError, match="inventory is incomplete"):
        checkpoints.validate_resume(root, request, boundary, limits)


@pytest.mark.parametrize("checkpoint_phase", ("sealed", "paging", "completed"))
def test_sealed_progress_without_boundary_never_permits_recapture(
    tmp_path: Path,
    checkpoint_phase: str,
) -> None:
    root, request = _root(tmp_path)
    limits = _limits()
    record = _record()
    boundary = seal((record,), request)
    checkpoints.initialize(root, request, limits)
    checkpoints.write_record(root, record.source_record_pk, record.as_dict(), limits)
    checkpoints.write_boundary(root, boundary, limits)
    if checkpoint_phase == "paging":
        checkpoints.write_page(root, 1, _page(boundary.digest, record), limits)
        checkpoints.advance(root, boundary.digest, 1, limits)
    if checkpoint_phase == "completed":
        checkpoints.complete(root, boundary.digest, 0, limits)
    (root / "boundary.json").unlink()

    with pytest.raises(RuntimeError, match="missing its boundary"):
        checkpoints.boundary_capture_allowed(root, limits)


def test_recovery_preserves_only_the_exact_interrupted_link_pair(tmp_path: Path) -> None:
    root, request = _root(tmp_path)
    limits = _limits()
    checkpoints.initialize(root, request, limits)
    final = root / "recovered.json"
    temporary = root / f".recovered.json.{uuid.uuid4().hex}.tmp"
    payload = canonical_json({"value": "recovered"}).encode("utf-8")
    temporary.write_bytes(payload)
    os.link(temporary, final)

    checkpoints.bounded_usage(root, limits)

    assert final.read_bytes() == payload
    assert not temporary.exists()
    assert final.lstat().st_nlink == 1


def test_recovery_rejects_arbitrary_hardlinks_and_bounded_temp_scans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _root(tmp_path)
    limits = _limits()
    temporary = root / f".expected.json.{uuid.uuid4().hex}.tmp"
    temporary.write_text("{}", encoding="utf-8")
    os.link(temporary, root / "different.json")
    with pytest.raises(ValueError, match="unsafe temporary"):
        checkpoints.bounded_usage(root, limits)

    limited_root, _ = _root(
        tmp_path / "entry-limit",
        CheckpointLimits(max_bytes=1_000_000, max_entries=2),
    )
    for name in ("first.json", "second.json"):
        (limited_root / f".{name}.{uuid.uuid4().hex}.tmp").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="recovery exceeds entry ceiling"):
        checkpoints.bounded_usage(
            limited_root,
            CheckpointLimits(max_bytes=1_000_000, max_entries=2),
        )

    byte_root, _ = _root(
        tmp_path / "byte-limit",
        CheckpointLimits(max_bytes=1, max_entries=10),
    )
    (byte_root / f".large.json.{uuid.uuid4().hex}.tmp").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="recovery exceeds byte ceiling"):
        checkpoints.bounded_usage(byte_root, CheckpointLimits(max_bytes=1, max_entries=10))

    time_root, _ = _root(tmp_path / "time-limit")
    monotonic_values = iter((0.0, 2.0))
    monkeypatch.setattr(checkpoint_usage.time, "monotonic", lambda: next(monotonic_values))
    with pytest.raises(RuntimeError, match="recovery exceeds time ceiling"):
        checkpoints.bounded_usage(time_root, _limits())
