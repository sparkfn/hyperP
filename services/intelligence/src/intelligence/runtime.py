"""Bounded command lifecycle for injected reviewed commands."""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time
from collections.abc import Callable, Mapping
from multiprocessing import get_context
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import NoReturn, Protocol, cast

from intelligence.artifacts import (
    append_run_log,
    publish_inventory,
    quarantine_manifest,
    read_manifest,
    run_log_inventory,
    scan_staged_outputs,
    scan_staged_usage,
    write_manifest,
)
from intelligence.artifacts_manifest import MANIFEST_LIMIT_KEYS, RUNTIME_LIMIT_KEYS
from intelligence.config import RuntimeConfig
from intelligence.models import Health, OutputInventory, Run, TerminalRunState
from intelligence.registry import (
    PRODUCTION_REGISTRY,
    CommandHandler,
    RegisteredCommand,
    Registry,
    SafeRejectionError,
)
from intelligence.state import State

_SETSID = "setsid"
_KILLPG = "killpg"
_SIGKILL = "SIGKILL"
_READY_TIMEOUT_SECONDS = 5
_GROUP_GRACE_SECONDS = 1


class CleanupUnresolvedError(RuntimeError):
    """The child process group was not proven quiescent; durable lock must remain held."""


class PreLaunchError(RuntimeError):
    """A launch setup operation failed before process.start was invoked."""


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
        limits = _effective_limits(self.config, command)
        child_limits = _resource_limits(limits)
        _preflight_resource_enforcement(child_limits)
        run = self.state.create_mutating_run(
            name,
            limits,
            dict(command.public_metadata),
        )
        staging = self.state.layout.staging / run.run_id
        started = time.monotonic()
        process: BaseProcess | None = None
        try:
            staging.mkdir(mode=0o700, parents=True, exist_ok=False)
            self._log(run, "started", {})
            if child_limits is None and not command.rejection_codes:
                process = _start_command(command.execute, staging)
            elif child_limits is None:
                process = _start_command(command.execute, staging, None, command.rejection_codes)
            else:
                process = _start_command(
                    command.execute, staging, child_limits, command.rejection_codes
                )
            started = time.monotonic()
            terminal_state, termination_reason = self._wait_for_command(
                process, run, started, command
            )
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
            persisted_limits = dict(run.limits)
            inventory = scan_staged_outputs(
                self.config.workspace,
                run.run_id,
                persisted_limits["max_output_bytes"],
                persisted_limits["max_output_entries"],
            )
            self.state.begin_publishing(run, inventory)
            self.state.verify_fence(run)
            published = publish_inventory(
                self.config.workspace,
                run.run_id,
                inventory,
                persisted_limits["max_output_bytes"],
                persisted_limits["max_output_entries"],
            )
            self._finish(run, "completed", published, None, publication=True)
            return run.run_id
        except PreLaunchError:
            self.state.mark_execution_quiescent(run)
            self._finish_if_possible(run, "failed", "launch_setup_failed")
            raise
        except CleanupUnresolvedError:
            self.state.mark_cleanup_unresolved(run)
            raise
        except BaseException:
            current = self.state.inspect(run.run_id)
            if current is not None and current.state in {
                "completed",
                "failed",
                "cancelled",
                "timed_out",
                "stale_recovered",
            }:
                raise
            if process is None:
                self.state.mark_execution_quiescent(run)
            else:
                try:
                    _stop_child(process)
                    self.state.mark_execution_quiescent(run)
                except BaseException:
                    self.state.mark_cleanup_unresolved(run)
            terminal_state = self._terminal_state(run, started, failed=True)
            self._finish_if_possible(run, terminal_state, "runtime_error")
            raise

    def _reconcile_startup(self) -> None:
        for recovered in self.state.reconcile_publications():
            manifest_path = self.state.layout.manifests / f"{recovered.run.run_id}.json"
            existing: dict[str, object] | None = None
            if manifest_path.exists() or manifest_path.is_symlink():
                try:
                    existing = read_manifest(
                        manifest_path,
                        expected_run_id=recovered.run.run_id,
                        expected_command=recovered.run.command,
                        expected_state=recovered.state,
                        expected_outputs=recovered.outputs,
                        expected_reason=recovered.reason,
                        expected_created_at=recovered.run.created_at,
                        expected_started_at=recovered.run.started_at,
                        expected_ended_at=recovered.run.ended_at,
                        expected_limits=(
                            dict(recovered.run.limits) if recovered.run.limits else None
                        ),
                        expected_run_log=run_log_inventory(
                            self.config.workspace, recovered.run.run_id
                        ),
                        expected_command_provenance=(
                            None
                            if recovered.run.command_provenance is None
                            else dict(recovered.run.command_provenance)
                        ),
                    )
                except (OSError, ValueError):
                    quarantine_manifest(self.config.workspace, recovered.run.run_id)
            if existing is None:
                self._log(recovered.run, "publication_recovered", {"state": recovered.state})
                manifest = self._manifest(
                    recovered.run, recovered.state, recovered.outputs, recovered.reason
                )
            else:
                manifest = existing
            self.state.finalize_reconciled(recovered.run, manifest, recovered.outputs)

    def _wait_for_command(
        self, process: BaseProcess, run: Run, started: float, command: RegisteredCommand
    ) -> tuple[TerminalRunState, str]:
        """Enforce cancellation/runtime bounds by terminating a reviewed child process."""
        try:
            limits = dict(run.limits)
            while process.is_alive():
                if self.state.is_cancelled(run.run_id):
                    _stop_child(process)
                    self.state.mark_execution_quiescent(run)
                    return "cancelled", "cancellation_requested"
                if time.monotonic() - started >= limits["max_runtime_seconds"]:
                    _stop_child(process)
                    self.state.mark_execution_quiescent(run)
                    return "timed_out", "runtime_limit_exceeded"
                try:
                    scan_staged_usage(
                        self.config.workspace,
                        run.run_id,
                        limits["max_output_bytes"],
                        limits["max_output_entries"],
                    )
                except RuntimeError:
                    _stop_child(process)
                    self.state.mark_execution_quiescent(run)
                    return "failed", "output_limit_exceeded"
                except ValueError:
                    _stop_child(process)
                    self.state.mark_execution_quiescent(run)
                    return "failed", "unsafe_staged_output"
                self.state.heartbeat(run)
                time.sleep(0.1)
            process.join()
            try:
                _quiesce_process_group(process)
            except CleanupUnresolvedError:
                raise
            except BaseException as error:
                raise CleanupUnresolvedError(
                    "reviewed child process group cleanup failed"
                ) from error
            self.state.mark_execution_quiescent(run)
            if process.exitcode == 0:
                return "completed", "completed"
            return "failed", _rejection_reason(process.exitcode, command)
        except CleanupUnresolvedError:
            raise
        except BaseException as error:
            try:
                if process.is_alive():
                    _stop_child(process)
                    self.state.mark_execution_quiescent(run)
                else:
                    _quiesce_process_group(process)
                    self.state.mark_execution_quiescent(run)
            except CleanupUnresolvedError:
                raise
            except BaseException as cleanup_error:
                raise CleanupUnresolvedError("reviewed child cleanup failed") from cleanup_error
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
        try:
            details: dict[str, str] = (
                {"state": str(state)}
                if state == "completed"
                else {
                    "metrics_status": "unavailable",
                    "reason": reason or "command_failed",
                    "state": str(state),
                }
            )
            self._log(run, "terminal", details)
        except (OSError, RuntimeError):
            # Terminal state and parent-owned manifest evidence must not depend
            # on a best-effort terminal log append.
            pass
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
            limits=dict(run.limits)
            or {
                "max_log_bytes": self.config.max_log_bytes,
                "max_output_bytes": self.config.max_output_bytes,
                "max_output_entries": self.config.max_output_entries,
                "max_runtime_seconds": self.config.max_runtime_seconds,
            },
            run_log=run_log_inventory(self.config.workspace, run.run_id),
            legacy_unknown_limits=not bool(run.limits),
            command_provenance=(
                None if run.command_provenance is None else dict(run.command_provenance)
            ),
        )

    def _precreated_manifest_exists(self, run_id: str) -> bool:
        try:
            self.state.layout.manifests.joinpath(f"{run_id}.json").lstat()
        except FileNotFoundError:
            return False
        return True

    def _log(self, run: Run, event: str, details: dict[str, str]) -> None:
        limits = dict(run.limits)
        maximum = limits.get("max_log_bytes", self.config.max_log_bytes)
        append_run_log(
            self.config.workspace,
            run.run_id,
            event,
            details,
            maximum,
            command=run.command,
        )

    def _terminal_state(self, run: Run, started: float, failed: bool = False) -> TerminalRunState:
        if self.state.is_cancelled(run.run_id):
            return "cancelled"
        if time.monotonic() - started >= dict(run.limits).get(
            "max_runtime_seconds", self.config.max_runtime_seconds
        ):
            return "timed_out"
        return "failed" if failed else "completed"


def _effective_limits(config: RuntimeConfig, command: RegisteredCommand) -> dict[str, int]:
    """Intersect runtime configuration with repository-reviewed command caps."""
    limits = {
        "max_log_bytes": config.max_log_bytes,
        "max_output_bytes": config.max_output_bytes,
        "max_output_entries": config.max_output_entries,
        "max_runtime_seconds": config.max_runtime_seconds,
    }
    if command.runtime_limits is not None:
        for key, value in command.runtime_limits.items():
            limits[key] = min(limits[key], value)
    if command.child_limits is not None:
        limits.update(command.child_limits)
    if set(limits) not in {RUNTIME_LIMIT_KEYS, MANIFEST_LIMIT_KEYS}:
        raise RuntimeError("effective runtime limits are invalid")
    return dict(sorted(limits.items()))


def _resource_limits(limits: Mapping[str, int]) -> Mapping[str, int] | None:
    """Select exactly the persisted POSIX limits, never ambient configuration."""
    resource_keys = {"max_cpu_seconds", "max_address_space_bytes"}
    if not resource_keys.issubset(limits):
        return None
    return {key: limits[key] for key in sorted(resource_keys)}


def _preflight_resource_enforcement(child_limits: Mapping[str, int] | None) -> None:
    """Reject unavailable or impossible required resource enforcement before admission."""
    if child_limits is None:
        return
    if os.name == "nt":
        raise RuntimeError("reviewed resource enforcement is unavailable")
    try:
        resource = __import__("resource")
        setrlimit_name = "setrlimit"
        getrlimit_name = "getrlimit"
        cpu_name = "RLIMIT_CPU"
        address_space_name = "RLIMIT_AS"
        infinity_name = "RLIM_INFINITY"
        setrlimit = getattr(resource, setrlimit_name)
        getrlimit = getattr(resource, getrlimit_name)
        cpu = getattr(resource, cpu_name)
        address_space = getattr(resource, address_space_name)
        infinity = getattr(resource, infinity_name)
        if not callable(setrlimit) or not callable(getrlimit):
            raise TypeError("resource limits are unavailable")
        for limit, constant in (
            (child_limits["max_cpu_seconds"], cpu),
            (child_limits["max_address_space_bytes"], address_space),
        ):
            current = getrlimit(constant)
            if (
                not isinstance(limit, int)
                or isinstance(limit, bool)
                or limit < 1
                or not isinstance(current, tuple)
                or len(current) != 2
                or not isinstance(current[1], int)
                or (current[1] != infinity and limit > current[1])
            ):
                raise RuntimeError("resource limits are unavailable")
    except (ImportError, AttributeError, KeyError, OSError, TypeError, ValueError) as error:
        raise RuntimeError("reviewed resource enforcement is unavailable") from error


def _start_command(
    handler: CommandHandler,
    staging: Path,
    child_limits: Mapping[str, int] | None = None,
    rejection_codes: tuple[str, ...] = (),
) -> BaseProcess:
    """Start only a reviewed registry callable; no caller executable or shell is accepted."""
    parent_ready: _ReadyChannel | None = None
    child_ready: _ReadyChannel | None = None
    try:
        context = get_context("spawn")
        parent_ready, child_ready = context.Pipe(duplex=False)
        process = context.Process(
            target=_child_entry, args=(handler, staging, child_ready, child_limits, rejection_codes)
        )
    except BaseException as error:
        _close_endpoint(child_ready)
        _close_endpoint(parent_ready)
        raise PreLaunchError("reviewed child launch setup failed") from error
    # No exception after this point may be treated as proof that no child exists:
    # multiprocessing may create an OS child before reporting a start failure.
    try:
        process.start()
    except BaseException as error:
        _abort_unready_child(process, error)
    try:
        try:
            child_ready.close()
        except BaseException as error:
            _abort_unready_child(process, error)
        if not parent_ready.poll(_READY_TIMEOUT_SECONDS):
            raise RuntimeError("reviewed child did not establish its process group")
        if not bool(parent_ready.recv()):
            raise RuntimeError("reviewed child failed before readiness")
    except BaseException as error:
        _abort_unready_child(process, error)
    finally:
        try:
            parent_ready.close()
        except BaseException as error:
            _abort_unready_child(process, error)
    return process


def _close_endpoint(endpoint: _ReadyChannel | None) -> None:
    if endpoint is None:
        return
    try:
        endpoint.close()
    except BaseException:
        pass


def _child_entry(
    handler: CommandHandler,
    staging: Path,
    ready: _ReadyChannel,
    child_limits: Mapping[str, int] | None,
    rejection_codes: tuple[str, ...],
) -> None:
    """Run a handler in a private session with raw output and tracebacks suppressed."""
    if os.name != "nt":
        setsid = cast(object, getattr(os, _SETSID, None))
        if not callable(setsid):
            raise RuntimeError("POSIX session isolation is unavailable")
        cast(Callable[[], None], setsid)()
    _apply_child_limits(child_limits)
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
            except SafeRejectionError as error:
                try:
                    index = rejection_codes.index(error.code)
                except ValueError:
                    sys.exit(1)
                sys.exit(20 + index)
            except BaseException:
                sys.exit(1)


def _rejection_reason(exitcode: int | None, command: RegisteredCommand) -> str:
    if exitcode is not None and 20 <= exitcode < 20 + len(command.rejection_codes):
        return command.rejection_codes[exitcode - 20]
    return "command_failed"


def _not_cancelled() -> bool:
    """Child cancellation is enforced by the parent supervisor rather than trusted cooperation."""
    return False


def _apply_child_limits(child_limits: Mapping[str, int] | None) -> None:
    """Apply reviewed POSIX CPU/address-space caps before untrusted-size materialization."""
    if child_limits is None:
        return
    if os.name == "nt":
        raise RuntimeError("reviewed resource enforcement is unavailable")
    try:
        resource = __import__("resource")
        setrlimit_name, cpu_name, address_space_name = "setrlimit", "RLIMIT_CPU", "RLIMIT_AS"
        setrlimit = cast(Callable[[int, tuple[int, int]], None], getattr(resource, setrlimit_name))
        cpu = cast(int, getattr(resource, cpu_name))
        address_space = cast(int, getattr(resource, address_space_name))
        setrlimit(cpu, (child_limits["max_cpu_seconds"],) * 2)
        setrlimit(
            address_space,
            (child_limits["max_address_space_bytes"],) * 2,
        )
    except (ImportError, AttributeError, KeyError, OSError, TypeError, ValueError) as error:
        raise RuntimeError("reviewed resource enforcement is unavailable") from error


def _abort_unready_child(process: BaseProcess, error: BaseException) -> NoReturn:
    """Retain the durable lock when readiness leaves process-group ownership uncertain."""
    try:
        _stop_child(process, group_ready=False)
    except BaseException as cleanup_error:
        raise CleanupUnresolvedError("unready child cleanup could not be proven") from cleanup_error
    raise CleanupUnresolvedError("reviewed child process-group readiness was not proven") from error


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
        raise CleanupUnresolvedError("reviewed child process did not stop after forced termination")


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
        raise CleanupUnresolvedError("reviewed child process group did not stop")
