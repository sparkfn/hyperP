"""CLI-only controls for the Intelligence foundation."""

from __future__ import annotations

import argparse
import json
import signal
import time
from collections.abc import Sequence
from pathlib import Path

from intelligence.artifacts import sha256_file
from intelligence.config import RuntimeConfig
from intelligence.models import OutputInventory, Run
from intelligence.runtime import IntelligenceRuntime


def build_parser() -> argparse.ArgumentParser:
    """Build the fixed command parser; it deliberately takes no shell-like arguments."""
    parser = argparse.ArgumentParser(prog="intelligence")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("health")
    commands.add_parser("idle")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("run_id")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("run_id")
    recover = commands.add_parser("recover-stale")
    recover.add_argument("run_id")
    recover.add_argument("--reason", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("name")
    commands.add_parser("verify-backup").add_argument("name")
    run = commands.add_parser("run")
    run.add_argument("name")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Execute one local control command."""
    arguments = build_parser().parse_args(argv)
    runtime = IntelligenceRuntime(RuntimeConfig.from_environment())
    try:
        if arguments.command == "health":
            health = runtime.health()
            print(json.dumps({"healthy": health.healthy, "reason": health.reason}))
            return 0 if health.healthy else 1
        if arguments.command == "status":
            health = runtime.health()
            active = runtime.state.active_run()
            print(
                json.dumps(
                    {
                        "active_run": None
                        if active is None
                        else {
                            "command": active.command,
                            "run_id": active.run_id,
                            "state": active.state,
                        },
                        "commands": runtime.registry.names(),
                        "health_reason": health.reason,
                        "healthy": health.healthy,
                        "mutations_enabled": runtime.config.mutations_enabled,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "cancel":
            runtime.state.cancel(arguments.run_id)
            return 0
        if arguments.command == "inspect":
            run = runtime.state.inspect(arguments.run_id)
            outputs = () if run is None else runtime.state.accepted_outputs(run.run_id)
            print(
                json.dumps(
                    None if run is None else _inspect_payload(runtime, run, outputs), sort_keys=True
                )
            )
            return 0
        if arguments.command == "recover-stale":
            runtime.state.recover_stale(
                arguments.run_id, arguments.reason, runtime.config.stale_seconds
            )
            return 0
        if arguments.command == "backup":
            target = runtime.config.workspace / "backups" / _backup_name(arguments.name)
            runtime.state.backup(target)
            return 0
        if arguments.command == "verify-backup":
            target = runtime.config.workspace / "backups" / _backup_name(arguments.name)
            runtime.state.verify_backup(target)
            return 0
        if arguments.command == "run":
            print(json.dumps({"run_id": runtime.run(arguments.name)}))
            return 0
        return _idle()
    finally:
        runtime.close()


def _idle() -> int:
    stopping = [False]
    signal.signal(signal.SIGTERM, lambda _signal, _frame: stopping.__setitem__(0, True))
    signal.signal(signal.SIGINT, lambda _signal, _frame: stopping.__setitem__(0, True))
    while not stopping[0]:
        time.sleep(1)
    return 0


def _backup_name(value: str) -> str:
    """Reject paths rather than silently changing an operator-selected backup name."""
    path = Path(value)
    if path.is_absolute() or len(path.parts) != 1 or value in {"", ".", ".."}:
        raise ValueError("backup name must be one safe relative file name")
    if path.suffix == ".sqlite3":
        raise ValueError("backup name must identify a bundle, not a SQLite file")
    return value


def _inspect_payload(
    runtime: IntelligenceRuntime, run: Run, outputs: Sequence[OutputInventory]
) -> dict[str, object]:
    """Return durable terminal evidence locations/checksums without configuration or secrets."""
    path = runtime.state.layout.manifests / f"{run.run_id}.json"
    manifest: dict[str, object] | None = None
    if path.exists() and not path.is_symlink() and path.is_file():
        manifest = {
            "path": f"runs/manifests/{run.run_id}.json",
            "sha256": sha256_file(path),
        }
    return {
        "manifest": manifest,
        "outputs": [item.__dict__ for item in outputs],
        "run": run.__dict__,
    }
