"""Formal-review regression coverage for supervision, recovery, and lock fencing."""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
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
        write_manifest(tmp_path, run_id, "reviewed", "completed", outputs=published)
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
