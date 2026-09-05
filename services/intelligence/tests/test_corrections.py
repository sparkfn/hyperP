"""Formal-review regression coverage for supervision, recovery, and lock fencing."""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from intelligence import runtime as runtime_module
from intelligence.artifacts import (
    publish_inventory,
    scan_staged_outputs,
    sha256_file,
    workspace_layout,
    write_manifest,
)
from intelligence.config import RuntimeConfig
from intelligence.models import OutputInventory
from intelligence.registry import Cancelled, CommandHandler, RegisteredCommand, Registry
from intelligence.runtime import IntelligenceRuntime
from intelligence.state import State


def growing_handler(directory: Path, cancelled: Cancelled) -> None:
    """Exceed the aggregate live staging budget without returning."""
    del cancelled
    with (directory / "growing.bin").open("wb") as handle:
        while True:
            handle.write(b"x" * 32)
            handle.flush()
            time.sleep(0.01)


def noisy_failure_handler(directory: Path, cancelled: Cancelled) -> None:
    """Attempt unbounded secret-shaped output before failing."""
    del directory, cancelled
    print("secret-token=should-not-escape" * 10000, flush=True)
    raise RuntimeError("secret-password=should-not-escape")


def descriptor_noisy_failure_handler(directory: Path, cancelled: Cancelled) -> None:
    """Write secret-shaped bytes through descriptors and a descendant process."""
    del directory, cancelled
    os.write(1, b"secret-direct-stdout\n")
    os.write(2, b"secret-direct-stderr\n")
    code = (
        "import os; os.write(1, b'secret-child-stdout\\n'); os.write(2, b'secret-child-stderr\\n')"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
    raise RuntimeError("secret-failure")


def grandchild_handler(directory: Path, cancelled: Cancelled) -> None:
    """Leave a delayed marker only if a descendant survives group termination."""
    del cancelled
    marker = directory / "grandchild-survived"
    code = (
        "import time; from pathlib import Path; time.sleep(1); "
        f"Path({str(marker)!r}).write_text('survived', encoding='utf-8')"
    )
    subprocess.Popen([sys.executable, "-c", code])
    while True:
        time.sleep(0.01)


def successful_descendant_handler(directory: Path, cancelled: Cancelled) -> None:
    """Return immediately after creating a descendant that must be reaped by the parent."""
    del cancelled
    marker = directory / "successful-descendant-survived"
    code = (
        "import time; from pathlib import Path; time.sleep(1); "
        f"Path({str(marker)!r}).write_text('survived', encoding='utf-8')"
    )
    subprocess.Popen([sys.executable, "-c", code])
    (directory / "result.json").write_text("{}", encoding="utf-8")


def precreated_wrong_limits_handler(directory: Path, cancelled: Cancelled) -> None:
    """Attempt to plant a canonical-looking manifest with attacker-selected limits."""
    del cancelled
    root = directory.parent.parent
    run_id = directory.name
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "command": "reviewed",
        "created_at": 0.0,
        "started_at": 0.0,
        "ended_at": 9999999999.0,
        "state": "completed",
        "limits": {
            "max_log_bytes": 1,
            "max_output_bytes": 1,
            "max_output_entries": 1,
            "max_runtime_seconds": 1,
        },
        "outputs": [],
        "run_log": None,
    }
    (root / "runs" / "manifests" / f"{run_id}.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    (directory / "result.json").write_text("{}", encoding="utf-8")


def precreated_wrong_end_time_handler(directory: Path, cancelled: Cancelled) -> None:
    """Attempt to plant a manifest with an attacker-selected terminal timestamp."""
    del cancelled
    root = directory.parent.parent
    run_id = directory.name
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "command": "reviewed",
        "created_at": 0.0,
        "started_at": 0.0,
        "ended_at": 1.0,
        "state": "completed",
        "limits": {
            "max_log_bytes": 1000000,
            "max_output_bytes": 100000,
            "max_output_entries": 10000,
            "max_runtime_seconds": 5,
        },
        "outputs": [],
        "run_log": None,
    }
    (root / "runs" / "manifests" / f"{run_id}.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    (directory / "result.json").write_text("{}", encoding="utf-8")


def _write_adversarial_manifest(directory: Path, mutation: str) -> None:
    """Plant one malformed live manifest variant before the parent can finalize it."""
    root = directory.parent.parent
    run_id = directory.name
    manifest: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_id,
        "command": "reviewed",
        "created_at": 0.0,
        "started_at": 0.0,
        "ended_at": 9999999999.0,
        "state": "completed",
        "limits": {
            "max_log_bytes": 1000000,
            "max_output_bytes": 100000,
            "max_output_entries": 10000,
            "max_runtime_seconds": 5,
        },
        "outputs": [],
        "run_log": None,
    }
    if mutation == "extra_secret":
        manifest["secret_token"] = "must-not-be-evidence"
    elif mutation == "mismatched_outputs":
        manifest["outputs"] = [{"byte_count": 1, "path": "outputs/fake.txt", "sha256": "0" * 64}]
    elif mutation == "omitted_outputs":
        del manifest["outputs"]
    elif mutation == "wrong_schema":
        manifest["schema_version"] = 999
    elif mutation == "wrong_log_digest":
        manifest["run_log"] = {
            "byte_count": 1,
            "path": f"runs/logs/{run_id}.ndjson",
            "sha256": "0" * 64,
        }
    elif mutation == "noncanonical":
        manifest_path = root / "runs" / "manifests" / f"{run_id}.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (directory / "result.json").write_text("{}", encoding="utf-8")
        return
    else:
        raise AssertionError(f"unknown manifest mutation: {mutation}")
    (root / "runs" / "manifests" / f"{run_id}.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    (directory / "result.json").write_text("{}", encoding="utf-8")


def precreated_extra_secret_handler(directory: Path, cancelled: Cancelled) -> None:
    del cancelled
    _write_adversarial_manifest(directory, "extra_secret")


def precreated_mismatched_outputs_handler(directory: Path, cancelled: Cancelled) -> None:
    del cancelled
    _write_adversarial_manifest(directory, "mismatched_outputs")


def precreated_omitted_outputs_handler(directory: Path, cancelled: Cancelled) -> None:
    del cancelled
    _write_adversarial_manifest(directory, "omitted_outputs")


def precreated_wrong_schema_handler(directory: Path, cancelled: Cancelled) -> None:
    del cancelled
    _write_adversarial_manifest(directory, "wrong_schema")


def precreated_wrong_log_digest_handler(directory: Path, cancelled: Cancelled) -> None:
    del cancelled
    _write_adversarial_manifest(directory, "wrong_log_digest")


def precreated_noncanonical_handler(directory: Path, cancelled: Cancelled) -> None:
    del cancelled
    _write_adversarial_manifest(directory, "noncanonical")


def precreated_directory_handler(directory: Path, cancelled: Cancelled) -> None:
    """Attempt to strand the run with a directory at the manifest path."""
    del cancelled
    root = directory.parent.parent
    (root / "runs" / "manifests" / directory.name).with_suffix(".json").mkdir()
    (directory / "result.json").write_text("{}", encoding="utf-8")


def precreated_symlink_handler(directory: Path, cancelled: Cancelled) -> None:
    """Attempt to strand the run with a symlink at the manifest path."""
    del cancelled
    root = directory.parent.parent
    manifest = root / "runs" / "manifests" / f"{directory.name}.json"
    target = directory / "attacker-target"
    target.write_text("attacker", encoding="utf-8")
    manifest.symlink_to(target)
    (directory / "result.json").write_text("{}", encoding="utf-8")


def lock_contender(workspace: str, result: multiprocessing.Queue[str]) -> None:
    """Spawned-process lock contender used to prove cross-process exclusion."""
    state = State(Path(workspace))
    try:
        try:
            state.create_mutating_run("contender")
        except RuntimeError:
            result.put("blocked")
        else:
            result.put("acquired")
    finally:
        state.close()


def _runtime(
    workspace: Path,
    handler: CommandHandler,
    *,
    timeout: int = 5,
    output_bytes: int = 100_000,
) -> IntelligenceRuntime:
    command = RegisteredCommand("reviewed", True, handler, {})
    return IntelligenceRuntime(
        RuntimeConfig(
            workspace,
            mutations_enabled=True,
            max_runtime_seconds=timeout,
            max_output_bytes=output_bytes,
        ),
        Registry((command,)),
    )


def _stale_publishing(workspace: Path, publish: bool) -> tuple[State, str]:
    state = State(workspace)
    run = state.create_mutating_run("reviewed")
    staging = state.layout.staging / run.run_id
    staging.mkdir()
    (staging / "result.json").write_text("{}", encoding="utf-8")
    inventory = scan_staged_outputs(workspace, run.run_id, 100)
    state.begin_publishing(run, inventory)
    if publish:
        publish_inventory(workspace, run.run_id, inventory, 100)
    state.connection.execute("UPDATE mutation_lock SET heartbeat_at = 0 WHERE singleton = 1")
    return state, run.run_id


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
            (runtime.state.layout.staging / run_id).glob(".rejected-manifest-*.json")
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
        assert tuple((runtime.state.layout.staging / run_id).glob(".rejected-manifest-*.json"))
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
            (runtime.state.layout.staging / run_id).glob(".rejected-manifest-*.json")
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
            (runtime.state.layout.staging / run_id).glob(".rejected-manifest-*.json")
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
        assert tuple((state.layout.staging / run_id).glob(".rejected-manifest-*.json"))
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


def test_publishing_is_an_explicit_non_cancellable_commit_point(tmp_path: Path) -> None:
    state = State(tmp_path)
    try:
        run = state.create_mutating_run("reviewed")
        state.begin_publishing(run, ())
        second = State(tmp_path)
        try:
            with pytest.raises(RuntimeError, match="cancellation commit point"):
                second.cancel(run.run_id)
        finally:
            second.close()
        state.connection.execute("UPDATE mutation_lock SET heartbeat_at = 0")
        state.recover_stale(run.run_id, "operator recovery", 1)
        recovered = state.inspect(run.run_id)
        assert recovered is not None and recovered.state == "failed"
    finally:
        state.close()


def test_publication_aware_stale_recovery_before_and_after_rename(tmp_path: Path) -> None:
    invalid, invalid_id = _stale_publishing(tmp_path / "before", publish=False)
    try:
        invalid.recover_stale(invalid_id, "operator recovery", 1)
        run = invalid.inspect(invalid_id)
        assert run is not None and run.state == "failed"
        assert invalid.accepted_outputs(invalid_id) == ()
    finally:
        invalid.close()
    valid, valid_id = _stale_publishing(tmp_path / "after", publish=True)
    try:
        valid.recover_stale(valid_id, "operator recovery", 1)
        run = valid.inspect(valid_id)
        assert run is not None and run.state == "completed"
        assert len(valid.accepted_outputs(valid_id)) == 1
    finally:
        valid.close()


def test_stale_recovery_is_idempotent_after_manifest_creation(tmp_path: Path) -> None:
    state, run_id = _stale_publishing(tmp_path, publish=True)
    try:
        output_path = workspace_layout(tmp_path).outputs / run_id / "result.json"
        published = tuple(
            (
                OutputInventory(
                    f"outputs/{run_id}/result.json",
                    sha256_file(output_path),
                    output_path.stat().st_size,
                ),
            )
        )
        run = state.inspect(run_id)
        assert run is not None
        write_manifest(
            tmp_path,
            run_id,
            "reviewed",
            "completed",
            outputs=published,
            created_at=run.created_at,
            started_at=run.started_at,
        )
        state.connection.execute("UPDATE mutation_lock SET heartbeat_at = 0 WHERE singleton = 1")
        state.recover_stale(run_id, "operator recovery", 1)
        recovered = state.inspect(run_id)
        assert recovered is not None and recovered.state == "completed"
        assert len(state.accepted_outputs(run_id)) == 1
    finally:
        state.close()


def test_stale_recovery_repairs_interrupted_accepted_registration(tmp_path: Path) -> None:
    state, run_id = _stale_publishing(tmp_path, publish=True)
    try:
        output_path = workspace_layout(tmp_path).outputs / run_id / "result.json"
        state.connection.execute(
            "INSERT INTO accepted_outputs(relative_path, run_id, sha256, byte_count) "
            "VALUES (?, ?, ?, ?)",
            (f"outputs/{run_id}/result.json", run_id, "0" * 64, 999),
        )
        state.connection.execute("UPDATE mutation_lock SET heartbeat_at = 0 WHERE singleton = 1")
        state.recover_stale(run_id, "operator recovery", 1)
        accepted = state.accepted_outputs(run_id)
        assert accepted == (
            OutputInventory(
                f"outputs/{run_id}/result.json",
                sha256_file(output_path),
                output_path.stat().st_size,
            ),
        )
    finally:
        state.close()


def test_cross_process_mutation_lock_is_exclusive(tmp_path: Path) -> None:
    owner = State(tmp_path)
    run = owner.create_mutating_run("owner")
    context = multiprocessing.get_context("spawn")
    result: multiprocessing.Queue[str] = context.Queue()
    process = context.Process(target=lock_contender, args=(str(tmp_path), result))
    process.start()
    process.join(timeout=10)
    try:
        assert process.exitcode == 0
        assert result.get(timeout=2) == "blocked"
    finally:
        result.close()
        owner.connection.execute(
            "UPDATE mutation_lock SET heartbeat_at = 0 WHERE run_id = ?", (run.run_id,)
        )
        owner.recover_stale(run.run_id, "cleanup", 1)
        owner.close()
