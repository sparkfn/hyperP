"""Bounded command lifecycle for injected reviewed commands."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import time
from collections.abc import Callable
from multiprocessing import get_context
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import cast

from intelligence.artifacts import (
    append_run_log,
    publish_inventory,
    run_log_inventory,
    scan_staged_outputs,
    write_manifest,
)
from intelligence.config import RuntimeConfig
from intelligence.models import Health, OutputInventory, Run, TerminalRunState
from intelligence.registry import PRODUCTION_REGISTRY, CommandHandler, Registry
from intelligence.state import State

_SETSID = "setsid"
_KILLPG = "killpg"
_SIGKILL = "SIGKILL"


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
            terminal_state, termination_reason = self._wait_for_command(process, run, started)
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
                self.config.workspace, run.run_id, self.config.max_output_bytes
            )
            self.state.begin_publishing(run, inventory)
            self.state.verify_fence(run)
            published = publish_inventory(
                self.config.workspace, run.run_id, inventory, self.config.max_output_bytes
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
                    scan_staged_outputs(
                        self.config.workspace, run.run_id, self.config.max_output_bytes
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
        path = self.state.layout.manifests / f"{run.run_id}.json"
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise ValueError("terminal manifest is unsafe")
            existing = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(existing, dict)
                and existing.get("run_id") == run.run_id
                and existing.get("command") == run.command
                and existing.get("state") == state
            ):
                return existing
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
                "max_runtime_seconds": self.config.max_runtime_seconds,
            },
            run_log=run_log_inventory(self.config.workspace, run.run_id),
        )

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
    process = context.Process(target=_child_entry, args=(handler, staging))
    process.start()
    return process


def _child_entry(handler: CommandHandler, staging: Path) -> None:
    """Run a handler in a private session with raw output and tracebacks suppressed."""
    if os.name != "nt":
        cast(Callable[[], None], getattr(os, _SETSID))()
    with open(os.devnull, "w", encoding="utf-8") as sink:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            try:
                handler(staging, _not_cancelled)
            except BaseException:
                sys.exit(1)


def _not_cancelled() -> bool:
    """Child cancellation is enforced by the parent supervisor rather than trusted cooperation."""
    return False


def _stop_child(process: BaseProcess) -> None:
    """Terminate, then kill if necessary, before releasing a bounded-run mutation lock."""
    if os.name != "nt" and process.pid is not None:
        try:
            cast(Callable[[int, int], None], getattr(os, _KILLPG))(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        process.terminate()
    process.join(timeout=5)
    if process.is_alive():
        if os.name != "nt" and process.pid is not None:
            try:
                cast(Callable[[int, int], None], getattr(os, _KILLPG))(
                    process.pid, cast(int, getattr(signal, _SIGKILL))
                )
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.join(timeout=5)
    if process.is_alive():
        raise RuntimeError("reviewed child process did not stop after forced termination")
