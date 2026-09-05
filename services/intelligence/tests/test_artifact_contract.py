"""Artifact limits, log evidence, and secret-boundary contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from intelligence import artifacts, artifacts_core, artifacts_manifest
from intelligence.artifacts import (
    append_run_log,
    publish_file,
    read_manifest,
    scan_staged_outputs,
    validate_manifest,
    workspace_layout,
    write_manifest,
)


def test_output_limit_and_symlink_are_rejected(tmp_path: Path) -> None:
    """Staging accepts neither oversized output nor a symbolic-link escape."""
    staging = tmp_path / "staging" / "run"
    staging.mkdir(parents=True)
    (staging / "large").write_bytes(b"12345")
    with pytest.raises(RuntimeError, match="byte limit"):
        scan_staged_outputs(tmp_path, "run", 4)
    (staging / "large").unlink()
    target = tmp_path / "outside"
    target.write_text("outside", encoding="utf-8")
    try:
        (staging / "link").symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable in this Windows test environment")
    with pytest.raises(ValueError, match="symbolic"):
        scan_staged_outputs(tmp_path, "run", 100)


def test_live_usage_rejects_too_many_entries_without_hashing(tmp_path: Path) -> None:
    """The live guard bounds entry count independently of final checksum inventory."""
    staging = tmp_path / "staging" / "run"
    staging.mkdir(parents=True)
    for index in range(4):
        (staging / f"{index}.json").write_bytes(b"")
    from intelligence.artifacts import scan_staged_usage

    with pytest.raises(RuntimeError, match="entry limit"):
        scan_staged_usage(tmp_path, "run", 100, 3)


def test_live_usage_does_not_hash_and_accepts_exact_byte_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incremental live scan counts bytes without calculating output checksums."""
    staging = tmp_path / "staging" / "run"
    staging.mkdir(parents=True)
    (staging / "exact.bin").write_bytes(b"x" * 17)

    def fail_hash(_path: Path) -> str:
        raise AssertionError("live usage scanning must not hash")

    monkeypatch.setattr(artifacts, "sha256_file", fail_hash)
    artifacts.scan_staged_usage(tmp_path, "run", 17, 1)
    with pytest.raises(RuntimeError, match="byte limit"):
        artifacts.scan_staged_usage(tmp_path, "run", 16, 1)


def test_no_replace_output_collision(tmp_path: Path) -> None:
    """A previously accepted path cannot be overwritten by a later producer."""
    staging = tmp_path / "staging" / "run"
    staging.mkdir(parents=True)
    source = staging / "same.json"
    source.write_text("{}", encoding="utf-8")
    publish_file(tmp_path, "run", source, 100)
    staging.mkdir(parents=True, exist_ok=True)
    source.write_text("{}", encoding="utf-8")
    with pytest.raises(FileExistsError):
        publish_file(tmp_path, "run", source, 100)


def test_log_has_required_safe_fields_and_stops_at_bound(tmp_path: Path) -> None:
    """NDJSON carries identity/severity/timestamp evidence and respects its cap."""
    append_run_log(tmp_path, "run", "started", {"count": 1}, 180, command="approved")
    append_run_log(tmp_path, "run", "detail", {"count": 2}, 180, command="approved")
    content = (workspace_layout(tmp_path).logs / "run.ndjson").read_text(encoding="utf-8")
    assert '"command":"approved"' in content
    assert '"event":"started"' in content
    assert '"run_id":"run"' in content
    assert '"severity"' in content
    with pytest.raises(ValueError, match="safe"):
        append_run_log(tmp_path, "run", "detail", {"api_secret": "x"}, 180, command="approved")


def test_log_records_one_truncation_marker_then_stops(tmp_path: Path) -> None:
    """A bounded log records its own truncation once without persisting oversized detail."""
    append_run_log(tmp_path, "run", "started", {}, 320, command="approved")
    append_run_log(tmp_path, "run", "detail", {"message": "x" * 180}, 320, command="approved")
    append_run_log(tmp_path, "run", "again", {"message": "y" * 180}, 320, command="approved")
    content = (workspace_layout(tmp_path).logs / "run.ndjson").read_text(encoding="utf-8")
    assert content.count('"event":"log_truncated"') == 1
    assert "x" * 180 not in content
    assert "y" * 180 not in content


def test_manifest_temp_write_failure_leaves_no_partial_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed temp write leaves no final/temp artifact and a later write succeeds."""
    original = artifacts_core.os.fsync

    def fail_sync(_descriptor: int) -> None:
        raise OSError("injected temp-write failure")

    monkeypatch.setattr(artifacts_core.os, "fsync", fail_sync)
    with pytest.raises(OSError, match="injected"):
        write_manifest(tmp_path, "fault", "approved", "completed")
    layout = workspace_layout(tmp_path)
    assert not (layout.manifests / "fault.json").exists()
    assert tuple(layout.manifests.glob(".fault.json.tmp-*")) == ()
    monkeypatch.setattr(artifacts_core.os, "fsync", original)
    write_manifest(tmp_path, "fault", "approved", "completed")
    assert (layout.manifests / "fault.json").is_file()


def test_manifest_link_failure_leaves_no_partial_and_rejects_different_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hard-link installation faults are clean, and final evidence is no-replace."""
    original = artifacts_core.os.link

    def fail_link(source: str, destination: str) -> None:
        del source, destination
        raise OSError("injected link failure")

    monkeypatch.setattr(artifacts_core.os, "link", fail_link)
    with pytest.raises(OSError, match="injected"):
        write_manifest(tmp_path, "link-fault", "approved", "completed")
    layout = workspace_layout(tmp_path)
    assert not (layout.manifests / "link-fault.json").exists()
    assert tuple(layout.manifests.glob(".link-fault.json.tmp-*")) == ()
    monkeypatch.setattr(artifacts_core.os, "link", original)
    first = write_manifest(tmp_path, "link-fault", "approved", "completed")
    write_manifest(
        tmp_path,
        "link-fault",
        "approved",
        "completed",
        created_at=float(first["created_at"]),
        started_at=float(first["started_at"]),
        ended_at=float(first["ended_at"]),
    )
    with pytest.raises(RuntimeError, match="different content"):
        write_manifest(tmp_path, "link-fault", "different", "completed")


def test_manifest_strict_expected_limits_and_end_time_are_enforced(tmp_path: Path) -> None:
    """Recovery callers can require exact parent-selected limits and terminal time."""
    first = write_manifest(
        tmp_path,
        "strict",
        "approved",
        "completed",
        created_at=10.0,
        started_at=11.0,
        ended_at=12.0,
        limits={
            "max_log_bytes": 100,
            "max_output_bytes": 200,
            "max_output_entries": 3,
            "max_runtime_seconds": 4,
        },
    )
    path = workspace_layout(tmp_path).manifests / "strict.json"
    expected_limits = {
        "max_log_bytes": 100,
        "max_output_bytes": 200,
        "max_output_entries": 3,
        "max_runtime_seconds": 4,
    }
    read_manifest(
        path,
        expected_run_id="strict",
        expected_command="approved",
        expected_state="completed",
        expected_created_at=10.0,
        expected_started_at=11.0,
        expected_ended_at=12.0,
        expected_limits=expected_limits,
    )
    with pytest.raises(ValueError, match="limits"):
        read_manifest(
            path,
            expected_run_id="strict",
            expected_command="approved",
            expected_state="completed",
            expected_limits={**expected_limits, "max_output_bytes": 201},
        )
    with pytest.raises(ValueError, match="time"):
        read_manifest(
            path,
            expected_run_id="strict",
            expected_command="approved",
            expected_state="completed",
            expected_ended_at=float(first["ended_at"]) + 1.0,
        )


@pytest.mark.parametrize(
    "limits",
    (
        {"max_log_bytes": 1},
        {"max_log_bytes": 1, "max_output_bytes": 2},
        {
            "max_log_bytes": 1,
            "max_output_bytes": 2,
            "max_runtime_seconds": 3,
            "max_output_entries": 4,
            "unexpected": 5,
        },
    ),
)
def test_legacy_manifest_rejects_non_prior_limit_shapes(limits: dict[str, int]) -> None:
    """Schema-v1 compatibility accepts only the recorded empty or exact three-key shapes."""
    value: dict[str, object] = {
        "schema_version": 1,
        "run_id": "legacy",
        "command": "approved",
        "created_at": 1.0,
        "started_at": 1.0,
        "ended_at": 2.0,
        "state": "completed",
        "limits": limits,
        "outputs": [],
        "run_log": None,
    }
    with pytest.raises(ValueError, match="limits"):
        validate_manifest(
            value,
            expected_run_id="legacy",
            expected_command="approved",
            expected_state="completed",
        )


def test_quarantine_fsyncs_both_directory_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quarantine persists both source removal and destination installation."""
    layout = workspace_layout(tmp_path)
    run_id = "fsync-run"
    (layout.manifests / f"{run_id}.json").write_text("attacker", encoding="utf-8")
    calls: list[Path] = []
    original = artifacts_manifest._fsync_directory

    def record(path: Path) -> None:
        calls.append(path)
        original(path)

    monkeypatch.setattr(artifacts_manifest, "_fsync_directory", record)
    artifacts_manifest.quarantine_manifest(tmp_path, run_id)
    assert layout.manifests in calls
    assert layout.rejected_manifests / run_id in calls
