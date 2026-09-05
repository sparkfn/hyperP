"""Bounded command lifecycle for injected reviewed commands."""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time
from collections.abc import Callable
from multiprocessing import get_context
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Protocol, cast

from intelligence.artifacts import (
    append_run_log,
    publish_inventory,
    quarantine_manifest,
    run_log_inventory,
    scan_staged_outputs,
    scan_staged_usage,
    write_manifest,
)
from intelligence.config import RuntimeConfig
from intelligence.models import Health, OutputInventory, Run, TerminalRunState
from intelligence.registry import PRODUCTION_REGISTRY, CommandHandler, Registry
from intelligence.state import State

_SETSID = "setsid"
_KILLPG = "killpg"
_SIGKILL = "SIGKILL"
_READY_TIMEOUT_SECONDS = 5
_GROUP_GRACE_SECONDS = 1


class _ReadyChannel(Protocol):
    def send(self, value: object) -> None: ...

    def close(self) -> None: ...


class IntelligenceRuntime:
    """Runtime which remains idle in production because its registry is intentionally empty."""

    def __init__(self, config: RuntimeConfig, registry: Registry = PRODUCTION_REGISTRY) -> None:
        self.config = config
        self.registry = registry
        self.state = State(config.workspace)
        self._reconcile_startup()

    def close(self) -> None:
        """Release local state resources."""
        self.state.close()

    def health(self) -> Health:
        """Return stale-lock aware health."""
        return self.state.health(self.config.stale_seconds)

    def run(self, name: str) -> str:
        """Run one allowlisted command with durable cancellation, timeout, and publication."""
        command = self.registry.get(name)
        if not command.mutates:
            raise RuntimeError("foundation accepts only bounded mutating command runs")
        if not self.config.mutations_enabled:
            raise RuntimeError("mutating execution is disabled")
        run = self.state.create_mutating_run(name)
        staging = self.state.layout.staging / run.run_id
        staging.mkdir(mode=0o700, parents=True, exist_ok=False)
        started = time.monotonic()
        self._log(run, "started", {})
        try:
            process = _start_command(command.execute, staging)
            started = time.monotonic()
            terminal_state, termination_reason = self._wait_for_command(process, run, started)
            if self._precreated_manifest_exists(run.run_id):
                try:
                    quarantine_manifest(self.config.workspace, run.run_id)
                except (OSError, RuntimeError, ValueError) as error:
                    raise RuntimeError("untrusted manifest quarantine failed") from error
                self._finish(run, "failed", (), "untrusted_manifest_precreated")
                raise RuntimeError("reviewed command pre-created terminal evidence")
            if terminal_state != "completed":
                self._finish(run, terminal_state, (), termination_reason)
                if terminal_state == "failed":
                    raise RuntimeError("reviewed command process failed")
                return run.run_id
            self.state.verify_fence(run)
            if self.state.is_cancelled(run.run_id):
                self._finish(run, "cancelled", (), "cancellation_requested")
                return run.run_id
            inventory = scan_staged_outputs(
                self.config.workspace,
                run.run_id,
                self.config.max_output_bytes,
                self.config.max_output_entries,
            )
            self.state.begin_publishing(run, inventory)
            self.state.verify_fence(run)
            published = publish_inventory(
                self.config.workspace,
                run.run_id,
                inventory,
                self.config.max_output_bytes,
                self.config.max_output_entries,
            )
            self._finish(run, "completed", published, None, publication=True)
            return run.run_id
        except BaseException:
            terminal_state = self._terminal_state(run.run_id, started, failed=True)
            self._finish_if_possible(run, terminal_state, "runtime_error")
            raise

    def _reconcile_startup(self) -> None:
        for recovered in self.state.reconcile_publications():
            self._log(recovered.run, "publication_recovered", {"state": recovered.state})
            manifest = self._manifest(
                recovered.run, recovered.state, recovered.outputs, recovered.reason
            )
            self.state.finalize_reconciled(recovered.run, manifest, recovered.outputs)

    def _wait_for_command(
        self, process: BaseProcess, run: Run, started: float
    ) -> tuple[TerminalRunState, str]:
        """Enforce cancellation/runtime bounds by terminating a reviewed child process."""
        try:
            while process.is_alive():
                if self.state.is_cancelled(run.run_id):
                    _stop_child(process)
                    return "cancelled", "cancellation_requested"
                if time.monotonic() - started >= self.config.max_runtime_seconds:
                    _stop_child(process)
                    return "timed_out", "runtime_limit_exceeded"
                try:
                    scan_staged_usage(
                        self.config.workspace,
                        run.run_id,
                        self.config.max_output_bytes,
                        self.config.max_output_entries,
                    )
                except RuntimeError:
                    _stop_child(process)
                    return "failed", "output_limit_exceeded"
                except ValueError:
                    _stop_child(process)
                    return "failed", "unsafe_staged_output"
                self.state.heartbeat(run)
                time.sleep(0.1)
            process.join()
            _quiesce_process_group(process)
            return (
                ("completed", "completed")
                if process.exitcode == 0
                else ("failed", "command_failed")
            )
        except BaseException as error:
            try:
                if process.is_alive():
                    _stop_child(process)
            except BaseException as cleanup_error:
                raise RuntimeError("reviewed child cleanup failed") from cleanup_error
            raise error

    def _finish(
        self,
        run: Run,
        state: TerminalRunState,
        outputs: tuple[OutputInventory, ...],
        reason: str | None,
        *,
        publication: bool = False,
    ) -> None:
        self._log(run, "terminal", {"state": state})
        manifest = self._manifest(run, state, outputs, reason)
        if publication:
            self.state.complete_publication(run, outputs, manifest)
        else:
            self.state.terminal(run, state, manifest)

    def _finish_if_possible(self, run: Run, state: TerminalRunState, reason: str) -> None:
        """Terminalize once when an error precedes immutable evidence; preserve original errors."""
        current = self.state.inspect(run.run_id)
        if current is None or current.state in {
            "completed",
            "failed",
            "cancelled",
            "timed_out",
            "stale_recovered",
        }:
            return
        try:
            self._finish(run, state, (), reason)
        except RuntimeError:
            return

    def _manifest(
        self,
        run: Run,
        state: TerminalRunState,
        outputs: tuple[OutputInventory, ...],
        reason: str | None,
    ) -> dict[str, object]:
        return write_manifest(
            self.config.workspace,
            run.run_id,
            run.command,
            state,
            outputs=outputs,
            reason=reason,
            created_at=run.created_at,
            started_at=run.started_at,
            limits={
                "max_log_bytes": self.config.max_log_bytes,
                "max_output_bytes": self.config.max_output_bytes,
                "max_output_entries": self.config.max_output_entries,
                "max_runtime_seconds": self.config.max_runtime_seconds,
            },
            run_log=run_log_inventory(self.config.workspace, run.run_id),
        )

    def _precreated_manifest_exists(self, run_id: str) -> bool:
        try:
            self.state.layout.manifests.joinpath(f"{run_id}.json").lstat()
        except FileNotFoundError:
            return False
        return True

    def _log(self, run: Run, event: str, details: dict[str, str]) -> None:
        append_run_log(
            self.config.workspace,
            run.run_id,
            event,
            details,
            self.config.max_log_bytes,
            command=run.command,
        )

    def _terminal_state(
        self, run_id: str, started: float, failed: bool = False
    ) -> TerminalRunState:
        if self.state.is_cancelled(run_id):
            return "cancelled"
        if time.monotonic() - started >= self.config.max_runtime_seconds:
            return "timed_out"
        return "failed" if failed else "completed"


def _start_command(handler: CommandHandler, staging: Path) -> BaseProcess:
    """Start only a reviewed registry callable; no caller executable or shell is accepted."""
    context = get_context("spawn")
    parent_ready, child_ready = context.Pipe(duplex=False)
    process = context.Process(target=_child_entry, args=(handler, staging, child_ready))
    process.start()
    child_ready.close()
    try:
        if not parent_ready.poll(_READY_TIMEOUT_SECONDS):
            raise RuntimeError("reviewed child did not establish its process group")
        if not bool(parent_ready.recv()):
            raise RuntimeError("reviewed child failed before readiness")
    except (EOFError, OSError) as error:
        _stop_child(process, group_ready=False)
        raise RuntimeError("reviewed child readiness failed") from error
    except BaseException:
        _stop_child(process, group_ready=False)
        raise
    finally:
        parent_ready.close()
    return process


def _child_entry(handler: CommandHandler, staging: Path, ready: _ReadyChannel) -> None:
    """Run a handler in a private session with raw output and tracebacks suppressed."""
    if os.name != "nt":
        setsid = cast(object, getattr(os, _SETSID, None))
        if not callable(setsid):
            raise RuntimeError("POSIX session isolation is unavailable")
        cast(Callable[[], None], setsid)()
    with open(os.devnull, "w", encoding="utf-8") as sink:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            try:
                ready.send(True)
                ready.close()
                handler(staging, _not_cancelled)
            except BaseException:
                sys.exit(1)


def _not_cancelled() -> bool:
    """Child cancellation is enforced by the parent supervisor rather than trusted cooperation."""
    return False


def _stop_child(process: BaseProcess, *, group_ready: bool = True) -> None:
    """Terminate, then kill if necessary, before releasing a bounded-run mutation lock."""
    if os.name != "nt" and process.pid is not None and group_ready:
        if not _signal_process_group(process.pid, signal.SIGTERM):
            process.terminate()
    else:
        process.terminate()
    process.join(timeout=_GROUP_GRACE_SECONDS)
    if process.is_alive():
        if os.name != "nt" and process.pid is not None and group_ready:
            if not _signal_process_group(process.pid, _sigkill()):
                process.kill()
        else:
            process.kill()
        process.join(timeout=_GROUP_GRACE_SECONDS)
    if group_ready:
        _quiesce_process_group(process)
    if process.is_alive():
        raise RuntimeError("reviewed child process did not stop after forced termination")


def _signal_process_group(pid: int, signum: int) -> bool:
    try:
        killpg = cast(object, getattr(os, _KILLPG, None))
        if not callable(killpg):
            return False
        cast(Callable[[int, int], None], killpg)(pid, signum)
    except OSError:
        return False
    return True


def _process_group_exists(pid: int) -> bool:
    try:
        killpg = cast(object, getattr(os, _KILLPG, None))
        if not callable(killpg):
            return False
        cast(Callable[[int, int], None], killpg)(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _sigkill() -> int:
    value = cast(object, getattr(signal, _SIGKILL, None))
    if not isinstance(value, int):
        raise RuntimeError("SIGKILL is unavailable")
    return value


def _quiesce_process_group(process: BaseProcess) -> None:
    """Ensure no descendant remains in a ready POSIX process group."""
    if os.name == "nt" or process.pid is None:
        return
    pid = process.pid
    if not _process_group_exists(pid):
        return
    _signal_process_group(pid, signal.SIGTERM)
    deadline = time.monotonic() + _GROUP_GRACE_SECONDS
    while _process_group_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    if _process_group_exists(pid):
        _signal_process_group(pid, _sigkill())
        deadline = time.monotonic() + _GROUP_GRACE_SECONDS
        while _process_group_exists(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
    if _process_group_exists(pid):
        raise RuntimeError("reviewed child process group did not stop")
