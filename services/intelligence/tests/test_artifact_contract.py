"""Artifact limits, log evidence, and secret-boundary contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from intelligence.artifacts import (
    append_run_log,
    publish_file,
    scan_staged_outputs,
    workspace_layout,
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
