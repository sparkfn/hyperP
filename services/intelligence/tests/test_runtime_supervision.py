"""Spawn-supervisor acceptance tests with module-level picklable reviewed handlers."""

from __future__ import annotations

import multiprocessing
import os
import signal
import time
from multiprocessing.process import BaseProcess
from pathlib import Path
from threading import Thread

import pytest
from intelligence import runtime as runtime_module
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


def abrupt_survivor_handler(directory: Path, cancelled: Cancelled) -> None:
    """Record the supervised child PID, then remain alive after its parent dies."""
    del cancelled
    (directory / "child.pid").write_text(str(os.getpid()), encoding="utf-8")
    while True:
        time.sleep(0.01)


def never_ready_child(handler: CommandHandler, staging: Path, ready: object) -> None:
    """Simulate a child that starts but never proves process-group readiness."""
    del handler, staging, ready
    time.sleep(30)


def abrupt_supervisor(workspace: str) -> None:
    """Run a real supervisor until the test kills this process abruptly."""
    runtime = _runtime(Path(workspace), abrupt_survivor_handler, timeout=30)
    runtime.run("approved")


def _runtime(tmp_path: Path, handler: CommandHandler, timeout: int = 5) -> IntelligenceRuntime:
    command = RegisteredCommand("approved", True, handler, {})
    return IntelligenceRuntime(
        RuntimeConfig(tmp_path, mutations_enabled=True, max_runtime_seconds=timeout),
        Registry((command,)),
    )


def test_staging_setup_failure_terminalizes_without_live_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admission setup failures prove quiescence because no child was launched."""
    runtime = _runtime(tmp_path, success_handler)
    original_mkdir = Path.mkdir
    staging_root = runtime.state.layout.staging

    def fail_run_staging(path: Path, *args: object, **kwargs: object) -> None:
        if path.parent == staging_root:
            raise OSError("injected staging allocation failure")
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_run_staging)
    try:
        with pytest.raises(OSError, match="allocation"):
            runtime.run("approved")
        run_id = str(runtime.state.connection.execute("SELECT id FROM runs").fetchone()[0])
        run = runtime.state.inspect(run_id)
        assert run is not None and run.state == "failed"
        assert not run.execution_may_be_alive
        assert runtime.state.active_run() is None
        assert runtime.health().healthy
    finally:
        runtime.close()


def test_initial_log_failure_terminalizes_without_live_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed admission log append cannot strand a lock for an unlaunched run."""
    runtime = _runtime(tmp_path, success_handler)

    def fail_log(*args: object, **kwargs: object) -> None:
        raise OSError("injected initial log failure")

    monkeypatch.setattr(runtime_module, "append_run_log", fail_log)
    try:
        with pytest.raises(OSError, match="initial log"):
            runtime.run("approved")
        run_id = str(runtime.state.connection.execute("SELECT id FROM runs").fetchone()[0])
        run = runtime.state.inspect(run_id)
        assert run is not None and run.state == "failed"
        assert not run.execution_may_be_alive
        assert runtime.state.active_run() is None
        assert runtime.health().healthy
    finally:
        runtime.close()


def test_pipe_setup_failure_is_proven_prelaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pipe allocation failure cannot have created a child and releases the lock."""
    runtime = _runtime(tmp_path, success_handler)

    class Context:
        def Pipe(self, *, duplex: bool) -> object:  # noqa: N802
            del duplex
            raise OSError("injected pipe failure")

    monkeypatch.setattr(runtime_module, "get_context", lambda _name: Context())
    try:
        with pytest.raises(runtime_module.PreLaunchError, match="setup"):
            runtime.run("approved")
        run_id = str(runtime.state.connection.execute("SELECT id FROM runs").fetchone()[0])
        run = runtime.state.inspect(run_id)
        assert run is not None and run.state == "failed"
        assert runtime.state.active_run() is None
        assert runtime.health().healthy
    finally:
        runtime.close()


def test_process_setup_failure_closes_pipe_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Process construction failure closes both endpoints before terminalization."""
    runtime = _runtime(tmp_path, success_handler)

    class Endpoint:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    parent, child = Endpoint(), Endpoint()

    class Context:
        def Pipe(self, *, duplex: bool) -> tuple[Endpoint, Endpoint]:  # noqa: N802
            del duplex
            return parent, child

        def Process(self, **kwargs: object) -> object:  # noqa: N802
            del kwargs
            raise OSError("injected process construction failure")

    monkeypatch.setattr(runtime_module, "get_context", lambda _name: Context())
    try:
        with pytest.raises(runtime_module.PreLaunchError, match="setup"):
            runtime.run("approved")
        assert parent.closed and child.closed
        run_id = str(runtime.state.connection.execute("SELECT id FROM runs").fetchone()[0])
        run = runtime.state.inspect(run_id)
        assert run is not None and run.state == "failed"
        assert runtime.state.active_run() is None
        assert runtime.health().healthy
    finally:
        runtime.close()


def test_start_exception_without_pid_retains_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception from process.start is uncertain even when pid is still None."""
    runtime = _runtime(tmp_path, success_handler)

    class Endpoint:
        def close(self) -> None:
            return None

        def poll(self, _timeout: float) -> bool:
            return False

    class Child:
        pid: int | None = None

        def start(self) -> None:
            raise OSError("injected start failure")

        def terminate(self) -> None:
            return None

        def join(self, timeout: float | None = None) -> None:
            del timeout

        def is_alive(self) -> bool:
            return False

    class Context:
        def Pipe(self, *, duplex: bool) -> tuple[Endpoint, Endpoint]:  # noqa: N802
            del duplex
            return Endpoint(), Endpoint()

        def Process(self, **kwargs: object) -> Child:  # noqa: N802
            del kwargs
            return Child()

    monkeypatch.setattr(runtime_module, "get_context", lambda _name: Context())
    try:
        with pytest.raises(runtime_module.CleanupUnresolvedError):
            runtime.run("approved")
        run_id = str(runtime.state.connection.execute("SELECT id FROM runs").fetchone()[0])
        run = runtime.state.inspect(run_id)
        assert run is not None and run.state == "running"
        assert run.execution_may_be_alive and run.cleanup_unresolved
        assert runtime.state.active_run() is not None
        assert not runtime.health().healthy
    finally:
        runtime.close()


def test_post_start_generic_failure_retains_lock_when_cleanup_is_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real started child cannot be terminalized after cleanup proof fails."""
    runtime = _runtime(tmp_path, uncooperative_handler, timeout=10)
    original_stop = runtime_module._stop_child

    def fail_wait(*args: object, **kwargs: object) -> tuple[str, str]:
        del args, kwargs
        raise RuntimeError("injected post-start failure")

    def fail_after_cleanup(process: BaseProcess, *, group_ready: bool = True) -> None:
        original_stop(process, group_ready=group_ready)
        raise OSError("injected cleanup uncertainty")

    monkeypatch.setattr(runtime, "_wait_for_command", fail_wait)
    monkeypatch.setattr(runtime_module, "_stop_child", fail_after_cleanup)
    try:
        with pytest.raises(RuntimeError, match="post-start"):
            runtime.run("approved")
        run_id = str(runtime.state.connection.execute("SELECT id FROM runs").fetchone()[0])
        run = runtime.state.inspect(run_id)
        assert run is not None and run.state == "running"
        assert run.execution_may_be_alive and run.cleanup_unresolved
        assert runtime.state.active_run() is not None
        assert not runtime.health().healthy
        with pytest.raises(RuntimeError, match="already active"):
            runtime.state.create_mutating_run("blocked")
    finally:
        runtime.close()


def test_real_readiness_timeout_retains_liveness_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A started child with no readiness proof becomes cleanup-unresolved."""
    runtime = _runtime(tmp_path, success_handler)
    monkeypatch.setattr(runtime_module, "_child_entry", never_ready_child)
    monkeypatch.setattr(runtime_module, "_READY_TIMEOUT_SECONDS", 0.1)
    try:
        with pytest.raises(runtime_module.CleanupUnresolvedError, match="readiness"):
            runtime.run("approved")
        run_id = str(runtime.state.connection.execute("SELECT id FROM runs").fetchone()[0])
        run = runtime.state.inspect(run_id)
        assert run is not None and run.state == "running"
        assert run.execution_may_be_alive and run.cleanup_unresolved
        assert runtime.state.active_run() is not None
        assert not runtime.health().healthy
    finally:
        runtime.close()


def test_unproven_start_readiness_retains_liveness_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A classified partial-start failure cannot terminalize or release the lock."""
    runtime = _runtime(tmp_path, success_handler)

    def fail_start(_handler: CommandHandler, _staging: Path) -> object:
        raise runtime_module.CleanupUnresolvedError("readiness was not proven")

    monkeypatch.setattr(runtime_module, "_start_command", fail_start)
    try:
        with pytest.raises(runtime_module.CleanupUnresolvedError, match="readiness"):
            runtime.run("approved")
        run_id = str(runtime.state.connection.execute("SELECT id FROM runs").fetchone()[0])
        run = runtime.state.inspect(run_id)
        assert run is not None and run.state == "running"
        assert run.execution_may_be_alive and run.cleanup_unresolved
        assert runtime.state.active_run() is not None
        assert not runtime.health().healthy
        with pytest.raises(RuntimeError, match="already active"):
            runtime.state.create_mutating_run("blocked")
    finally:
        runtime.close()


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
    runtime = _runtime(tmp_path, uncooperative_handler, timeout=1)
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

    observer = State(tmp_path)
    worker = Thread(target=invoke)
    worker.start()
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


@pytest.mark.skipif(os.name == "nt", reason="PID namespaces and process groups are POSIX contracts")
def test_abrupt_supervisor_death_keeps_same_epoch_recovery_fenced(tmp_path: Path) -> None:
    """An abruptly killed supervisor cannot release a live child's durable fence."""
    context = multiprocessing.get_context("spawn")
    supervisor = context.Process(target=abrupt_supervisor, args=(str(tmp_path),))
    supervisor.start()
    observer = State(tmp_path)
    child_pid: int | None = None
    try:
        deadline = time.monotonic() + 10
        active = observer.active_run()
        while active is None and time.monotonic() < deadline:
            time.sleep(0.05)
            active = observer.active_run()
        assert active is not None
        pid_path = workspace_layout(tmp_path).staging / active.run_id / "child.pid"
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pid_path.is_file()
        child_pid = int(pid_path.read_text(encoding="utf-8"))
        assert os.getpgid(child_pid) == child_pid

        os.kill(supervisor.pid, signal.SIGKILL)
        supervisor.join(timeout=5)
        assert not supervisor.is_alive()
        assert supervisor.exitcode != 0
        assert os.getpgid(child_pid) == child_pid

        observer.connection.execute("UPDATE mutation_lock SET heartbeat_at = 0")
        with pytest.raises(RuntimeError, match="already active"):
            observer.create_mutating_run("blocked")
        with pytest.raises(RuntimeError, match="execution-domain"):
            observer.recover_stale(active.run_id, "same epoch", 1)
        run = observer.inspect(active.run_id)
        assert run is not None and run.state == "running" and run.execution_may_be_alive

        recreated = State(tmp_path, runtime_epoch="recreated-container")
        try:
            recreated.recover_stale(active.run_id, "container recreated", 1)
            recovered = recreated.inspect(active.run_id)
            assert recovered is not None and recovered.state == "stale_recovered"
            assert recreated.active_run() is None
        finally:
            recreated.close()
    finally:
        if supervisor.is_alive():
            os.kill(supervisor.pid, signal.SIGKILL)
            supervisor.join(timeout=5)
        if child_pid is not None:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        observer.close()
