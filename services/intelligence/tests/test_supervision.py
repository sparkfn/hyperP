"""Supervised child lifecycle and bounded output regression coverage."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from intelligence import runtime as runtime_module
from intelligence.artifacts import workspace_layout
from intelligence.runtime import CleanupUnresolvedError
from test_corrections import (
    _runtime,
    descriptor_noisy_failure_handler,
    grandchild_handler,
    growing_handler,
    noisy_failure_handler,
    successful_descendant_handler,
)


def test_live_output_limit_terminalizes_without_publication(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, growing_handler, output_bytes=128)
    try:
        with pytest.raises(RuntimeError, match="failed"):
            runtime.run("reviewed")
        run_id = str(runtime.state.connection.execute("SELECT id FROM runs").fetchone()[0])
        run = runtime.state.inspect(run_id)
        assert run is not None and run.state == "failed"
        assert runtime.state.accepted_outputs(run_id) == ()
        manifest = (runtime.state.layout.manifests / f"{run_id}.json").read_text(encoding="utf-8")
        assert '"reason":"output_limit_exceeded"' in manifest
    finally:
        runtime.close()


def test_child_output_and_traceback_are_contained(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = _runtime(tmp_path, noisy_failure_handler)
    try:
        with pytest.raises(RuntimeError, match="failed"):
            runtime.run("reviewed")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
        run_id = str(runtime.state.connection.execute("SELECT id FROM runs").fetchone()[0])
        log = (runtime.state.layout.logs / f"{run_id}.ndjson").read_text(encoding="utf-8")
        assert "secret-token" not in log
        assert "secret-password" not in log
    finally:
        runtime.close()


def test_child_descriptor_and_descendant_output_are_contained(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    runtime = _runtime(tmp_path, descriptor_noisy_failure_handler)
    try:
        with pytest.raises(RuntimeError, match="failed"):
            runtime.run("reviewed")
        captured = capfd.readouterr()
        assert captured.out == ""
        assert captured.err == ""
    finally:
        runtime.close()


@pytest.mark.skipif(os.name == "nt", reason="process groups are a POSIX contract")
def test_supervisor_exception_cleans_up_live_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path, grandchild_handler, timeout=10)

    def fail_heartbeat(_run: object) -> None:
        raise RuntimeError("induced supervisor failure")

    monkeypatch.setattr(runtime.state, "heartbeat", fail_heartbeat)
    try:
        with pytest.raises(RuntimeError, match="induced supervisor failure"):
            runtime.run("reviewed")
        run_id = str(runtime.state.connection.execute("SELECT id FROM runs").fetchone()[0])
        marker = workspace_layout(tmp_path).staging / run_id / "grandchild-survived"
        time.sleep(1.3)
        assert not marker.exists()
        assert runtime.state.active_run() is None
    finally:
        runtime.close()


@pytest.mark.skipif(os.name == "nt", reason="process groups are a POSIX contract")
def test_unresolved_group_cleanup_keeps_lock_for_stale_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-exit quiescence fault must not release a lock with live descendants unknown."""
    runtime = _runtime(tmp_path, successful_descendant_handler, timeout=5)

    def fail_quiescence(_process: object) -> None:
        raise CleanupUnresolvedError("injected quiescence failure")

    monkeypatch.setattr(runtime_module, "_quiesce_process_group", fail_quiescence)
    try:
        with pytest.raises(CleanupUnresolvedError, match="quiescence"):
            runtime.run("reviewed")
        run_id = str(runtime.state.connection.execute("SELECT id FROM runs").fetchone()[0])
        run = runtime.state.inspect(run_id)
        assert run is not None and run.state == "running"
        assert runtime.state.active_run() is not None
        runtime.state.connection.execute("UPDATE mutation_lock SET heartbeat_at = 0")
        assert not runtime.health().healthy
    finally:
        runtime.close()


@pytest.mark.skipif(os.name == "nt", reason="process groups are a POSIX contract")
def test_generic_group_cleanup_error_keeps_lock_after_child_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown group-probe failures fail closed even after the leader has exited."""
    runtime = _runtime(tmp_path, successful_descendant_handler, timeout=5)

    def fail_quiescence(_process: object) -> None:
        raise OSError("injected group probe failure")

    monkeypatch.setattr(runtime_module, "_quiesce_process_group", fail_quiescence)
    try:
        with pytest.raises(CleanupUnresolvedError, match="cleanup"):
            runtime.run("reviewed")
        run_id = str(runtime.state.connection.execute("SELECT id FROM runs").fetchone()[0])
        run = runtime.state.inspect(run_id)
        assert run is not None and run.state == "running"
        assert runtime.state.active_run() is not None
        runtime.state.connection.execute("UPDATE mutation_lock SET heartbeat_at = 0")
        assert not runtime.health().healthy
    finally:
        runtime.close()


@pytest.mark.skipif(os.name == "nt", reason="process groups are a POSIX contract")
def test_timeout_terminates_descendant_before_lock_release(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, grandchild_handler, timeout=1)
    try:
        run_id = runtime.run("reviewed")
        marker = workspace_layout(tmp_path).staging / run_id / "grandchild-survived"
        time.sleep(1.3)
        assert not marker.exists()
        assert runtime.state.active_run() is None
    finally:
        runtime.close()


@pytest.mark.skipif(os.name == "nt", reason="process groups are a POSIX contract")
def test_successful_return_quiesces_descendant_before_publication(tmp_path: Path) -> None:
    """A handler cannot bypass group cleanup by returning before its descendant."""
    runtime = _runtime(tmp_path, successful_descendant_handler, timeout=5)
    try:
        run_id = runtime.run("reviewed")
        marker = workspace_layout(tmp_path).staging / run_id / "successful-descendant-survived"
        time.sleep(1.3)
        assert not marker.exists()
        run = runtime.state.inspect(run_id)
        assert run is not None and run.state == "completed"
        assert runtime.state.active_run() is None
    finally:
        runtime.close()
