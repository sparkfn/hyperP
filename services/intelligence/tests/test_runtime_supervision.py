"""Spawn-supervisor acceptance tests with module-level picklable reviewed handlers."""

from __future__ import annotations

import time
from pathlib import Path
from threading import Thread

import pytest
from intelligence.artifacts import workspace_layout
from intelligence.config import RuntimeConfig
from intelligence.registry import Cancelled, CommandHandler, RegisteredCommand, Registry
from intelligence.runtime import IntelligenceRuntime
from intelligence.state import State


def success_handler(directory: Path, cancelled: Cancelled) -> None:
    """Write one accepted output from a spawn-picklable reviewed handler."""
    del cancelled
    (directory / "output.json").write_text("{}", encoding="utf-8")


def failure_handler(directory: Path, cancelled: Cancelled) -> None:
    """Fail predictably in the child process."""
    del directory, cancelled
    raise RuntimeError("expected failure")


def uncooperative_handler(directory: Path, cancelled: Cancelled) -> None:
    """Ignore cancellation to prove parent timeout termination."""
    del directory, cancelled
    while True:
        time.sleep(0.01)


def cancellation_handler(directory: Path, cancelled: Cancelled) -> None:
    """Remain alive until the parent proves durable cancellation is enforced."""
    (directory / "child-started").write_text("started", encoding="utf-8")
    while True:
        time.sleep(0.01)


def _runtime(tmp_path: Path, handler: CommandHandler, timeout: int = 1) -> IntelligenceRuntime:
    command = RegisteredCommand("approved", True, handler, {})
    return IntelligenceRuntime(
        RuntimeConfig(tmp_path, mutations_enabled=True, max_runtime_seconds=timeout),
        Registry((command,)),
    )


def test_spawn_success_publishes_output_and_terminal_evidence(tmp_path: Path) -> None:
    """A reviewed child output becomes accepted state plus terminal evidence."""
    runtime = _runtime(tmp_path, success_handler)
    run_id = runtime.run("approved")
    run = runtime.state.inspect(run_id)
    assert run is not None and run.state == "completed"
    outputs = runtime.state.accepted_outputs(run_id)
    assert len(outputs) == 1
    assert outputs[0].relative_path == f"outputs/{run_id}/output.json"
    manifest = runtime.state.layout.manifests / f"{run_id}.json"
    log = runtime.state.layout.logs / f"{run_id}.ndjson"
    assert manifest.is_file()
    assert log.is_file()
    assert '"state":"completed"' in manifest.read_text(encoding="utf-8")
    assert '"event":"terminal"' in log.read_text(encoding="utf-8")
    runtime.close()


def test_spawn_failure_terminalizes_failed(tmp_path: Path) -> None:
    """A child failure cannot strand the lock or report success."""
    runtime = _runtime(tmp_path, failure_handler)
    with pytest.raises(RuntimeError, match="failed"):
        runtime.run("approved")
    runs = tuple(runtime.state.connection.execute("SELECT id FROM runs"))
    assert len(runs) == 1
    run = runtime.state.inspect(str(runs[0][0]))
    assert run is not None and run.state == "failed"
    assert run.recovery_reason is None
    assert (runtime.state.layout.manifests / f"{run.run_id}.json").is_file()
    assert runtime.health().healthy
    runtime.close()


def test_spawn_timeout_terminates_uncooperative_handler(tmp_path: Path) -> None:
    """Configured timeout is parent-enforced even when handler never returns."""
    runtime = _runtime(tmp_path, uncooperative_handler)
    started = time.monotonic()
    run_id = runtime.run("approved")
    assert time.monotonic() - started < 4
    run = runtime.state.inspect(run_id)
    assert run is not None and run.state == "timed_out"
    assert runtime.state.active_run() is None
    runtime.close()


def test_second_connection_cancellation_terminates_child(tmp_path: Path) -> None:
    """A distinct durable connection can cancel a live spawned child process."""
    results: list[str] = []
    failures: list[BaseException] = []

    def invoke() -> None:
        runtime = _runtime(tmp_path, cancellation_handler, timeout=10)
        try:
            results.append(runtime.run("approved"))
        except BaseException as error:  # pragma: no cover - assertion below reports it.
            failures.append(error)
        finally:
            runtime.close()

    worker = Thread(target=invoke)
    worker.start()
    observer = State(tmp_path)
    try:
        deadline = time.monotonic() + 5
        active = observer.active_run()
        while active is None and time.monotonic() < deadline:
            time.sleep(0.05)
            active = observer.active_run()
        assert active is not None
        child_marker = workspace_layout(tmp_path).staging / active.run_id / "child-started"
        while not child_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert child_marker.is_file()
        observer.cancel(active.run_id)
        worker.join(timeout=5)
        assert not worker.is_alive(), "parent supervisor did not terminate its child"
        assert not failures
        assert results == [active.run_id]
        terminal = observer.inspect(active.run_id)
        assert terminal is not None and terminal.state == "cancelled"
        assert observer.active_run() is None
    finally:
        observer.close()
