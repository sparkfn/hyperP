"""Manifest ownership, quarantine, and recovery regression coverage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from intelligence import runtime as runtime_module
from intelligence.registry import CommandHandler
from intelligence.state import State
from test_corrections import (
    _runtime,
    _stale_publishing,
    precreated_directory_handler,
    precreated_extra_secret_handler,
    precreated_mismatched_outputs_handler,
    precreated_noncanonical_handler,
    precreated_omitted_outputs_handler,
    precreated_symlink_handler,
    precreated_wrong_end_time_handler,
    precreated_wrong_limits_handler,
    precreated_wrong_log_digest_handler,
    precreated_wrong_schema_handler,
)


@pytest.mark.parametrize(
    ("handler", "marker"),
    (
        (precreated_wrong_limits_handler, '"max_output_bytes":1,'),
        (precreated_wrong_end_time_handler, '"ended_at":1.0'),
    ),
)
def test_live_runtime_rejects_handler_precreated_manifest(
    tmp_path: Path, handler: CommandHandler, marker: str
) -> None:
    """Handler-owned manifest bytes never become authoritative runtime evidence."""
    runtime = _runtime(tmp_path, handler)
    try:
        with pytest.raises(RuntimeError):
            runtime.run("reviewed")
        run_id = str(runtime.state.connection.execute("SELECT id FROM runs").fetchone()[0])
        manifest_path = runtime.state.layout.manifests / f"{run_id}.json"
        manifest = manifest_path.read_text(encoding="utf-8")
        assert marker not in manifest
        assert "untrusted_manifest_precreated" in manifest
        assert "secret" not in manifest.lower()
        assert manifest == json.dumps(json.loads(manifest), sort_keys=True, separators=(",", ":"))
        quarantined = tuple(
            (runtime.state.layout.rejected_manifests / run_id).glob(".rejected-manifest-*.json")
        )
        assert len(quarantined) == 1
        run = runtime.state.inspect(run_id)
        assert run is not None and run.state == "failed"
        assert runtime.state.accepted_outputs(run_id) == ()
        assert runtime.state.active_run() is None
        assert runtime.health().healthy
    finally:
        runtime.close()
    reopened = State(tmp_path)
    try:
        assert reopened.active_run() is None
        assert reopened.inspect(run_id) is not None
        assert reopened.health(60).healthy
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "handler",
    (
        precreated_extra_secret_handler,
        precreated_mismatched_outputs_handler,
        precreated_omitted_outputs_handler,
        precreated_wrong_schema_handler,
        precreated_wrong_log_digest_handler,
        precreated_noncanonical_handler,
    ),
)
def test_live_runtime_rejects_all_other_adversarial_manifests(
    tmp_path: Path, handler: CommandHandler
) -> None:
    """Malformed handler-owned evidence never becomes authoritative terminal state."""
    runtime = _runtime(tmp_path, handler)
    try:
        with pytest.raises(RuntimeError):
            runtime.run("reviewed")
        run_id = str(runtime.state.connection.execute("SELECT id FROM runs").fetchone()[0])
        manifest_path = runtime.state.layout.manifests / f"{run_id}.json"
        manifest = manifest_path.read_text(encoding="utf-8")
        row = runtime.state.connection.execute(
            "SELECT state, manifest_json FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert str(row[0]) == "failed"
        assert row[1] == manifest
        assert "untrusted_manifest_precreated" in manifest
        assert "secret" not in manifest.lower()
        assert runtime.state.accepted_outputs(run_id) == ()
        assert runtime.state.active_run() is None
        assert runtime.health().healthy
        assert tuple(
            (runtime.state.layout.rejected_manifests / run_id).glob(".rejected-manifest-*.json")
        )
    finally:
        runtime.close()


def test_live_runtime_quarantines_directory_manifest_entry(tmp_path: Path) -> None:
    """A handler-created directory cannot strand the parent-owned terminal outcome."""
    runtime = _runtime(tmp_path, precreated_directory_handler)
    try:
        with pytest.raises(RuntimeError):
            runtime.run("reviewed")
        run_id = str(runtime.state.connection.execute("SELECT id FROM runs").fetchone()[0])
        manifest = (runtime.state.layout.manifests / f"{run_id}.json").read_text(encoding="utf-8")
        assert "untrusted_manifest_precreated" in manifest
        quarantine = tuple(
            (runtime.state.layout.rejected_manifests / run_id).glob(".rejected-manifest-*.json")
        )
        assert len(quarantine) == 1 and quarantine[0].is_dir()
        run = runtime.state.inspect(run_id)
        assert run is not None and run.state == "failed"
        assert runtime.state.accepted_outputs(run_id) == ()
        assert runtime.state.active_run() is None
        assert runtime.health().healthy
    finally:
        runtime.close()
    reopened = State(tmp_path)
    try:
        assert reopened.active_run() is None
        assert reopened.health(60).healthy
    finally:
        reopened.close()


def test_live_runtime_quarantines_symlink_manifest_entry(tmp_path: Path) -> None:
    """A handler-created symlink is moved as a link and never followed as evidence."""
    probe_target = tmp_path / "symlink-probe-target"
    probe = tmp_path / "symlink-probe"
    try:
        probe_target.write_text("probe", encoding="utf-8")
        probe.symlink_to(probe_target)
    except OSError:
        pytest.skip("symbolic links are unavailable in this test environment")
    finally:
        probe.unlink(missing_ok=True)
        probe_target.unlink(missing_ok=True)

    runtime = _runtime(tmp_path, precreated_symlink_handler)
    try:
        with pytest.raises(RuntimeError):
            runtime.run("reviewed")
        run_id = str(runtime.state.connection.execute("SELECT id FROM runs").fetchone()[0])
        manifest = (runtime.state.layout.manifests / f"{run_id}.json").read_text(encoding="utf-8")
        assert "untrusted_manifest_precreated" in manifest
        quarantine = tuple(
            (runtime.state.layout.rejected_manifests / run_id).glob(".rejected-manifest-*.json")
        )
        assert len(quarantine) == 1 and quarantine[0].is_symlink()
        run = runtime.state.inspect(run_id)
        assert run is not None and run.state == "failed"
        assert runtime.state.accepted_outputs(run_id) == ()
        assert runtime.state.active_run() is None
        assert runtime.health().healthy
    finally:
        runtime.close()
    reopened = State(tmp_path)
    try:
        assert reopened.active_run() is None
        assert reopened.health(60).healthy
    finally:
        reopened.close()


def test_stale_recovery_quarantines_invalid_preexisting_manifest(tmp_path: Path) -> None:
    state, run_id = _stale_publishing(tmp_path, publish=False)
    try:
        manifest_path = state.layout.manifests / f"{run_id}.json"
        manifest_path.write_text('{"secret_token":"attacker"}', encoding="utf-8")
        state.connection.execute("UPDATE mutation_lock SET heartbeat_at = 0 WHERE singleton = 1")
        state.recover_stale(run_id, "operator recovery", 1)
        run = state.inspect(run_id)
        assert run is not None and run.state == "failed"
        manifest = manifest_path.read_text(encoding="utf-8")
        assert "publication_recovery_invalid" in manifest
        assert "attacker" not in manifest
        assert state.accepted_outputs(run_id) == ()
        assert tuple((state.layout.rejected_manifests / run_id).glob(".rejected-manifest-*.json"))
    finally:
        state.close()


@pytest.mark.parametrize("publish", (False, True))
def test_stale_recovery_preserves_admission_limits(tmp_path: Path, publish: bool) -> None:
    """Recovery evidence records the exact limits persisted at admission."""
    limits = {
        "max_log_bytes": 101,
        "max_output_bytes": 202,
        "max_output_entries": 3,
        "max_runtime_seconds": 4,
    }
    state, run_id = _stale_publishing(tmp_path, publish, limits)
    try:
        state.connection.execute("UPDATE mutation_lock SET heartbeat_at = 0 WHERE singleton = 1")
        state.recover_stale(run_id, "operator recovery", 1)
        manifest = json.loads(
            (state.layout.manifests / f"{run_id}.json").read_text(encoding="utf-8")
        )
        assert manifest["limits"] == limits
    finally:
        state.close()


def test_manifest_quarantine_failure_keeps_lock_for_stale_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quarantine fault fails closed, then normal stale recovery can retry it."""
    runtime = _runtime(tmp_path, precreated_wrong_limits_handler)

    def fail_quarantine(_workspace: Path, _run_id: str) -> Path | None:
        raise OSError("injected quarantine failure")

    monkeypatch.setattr(runtime_module, "quarantine_manifest", fail_quarantine)
    try:
        with pytest.raises(RuntimeError, match="quarantine"):
            runtime.run("reviewed")
        run_id = str(runtime.state.connection.execute("SELECT id FROM runs").fetchone()[0])
        run = runtime.state.inspect(run_id)
        assert run is not None and run.state == "running"
        assert runtime.state.active_run() is not None
    finally:
        runtime.close()

    reopened = State(tmp_path)
    try:
        reopened.connection.execute("UPDATE mutation_lock SET heartbeat_at = 0 WHERE singleton = 1")
        reopened.recover_stale(run_id, "operator retry", 1)
        recovered = reopened.inspect(run_id)
        assert recovered is not None and recovered.state == "stale_recovered"
        assert reopened.active_run() is None
        assert reopened.health(60).healthy
        recovered_manifest = (reopened.layout.manifests / f"{run_id}.json").read_text(
            encoding="utf-8"
        )
        assert '"max_output_bytes":1,' not in recovered_manifest
    finally:
        reopened.close()
