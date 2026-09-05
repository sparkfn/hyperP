"""Bounded secret-free NDJSON run logs."""

from __future__ import annotations

import stat
import time
from collections.abc import Mapping
from pathlib import Path

from intelligence.artifacts_core import (
    _append_durable,
    _contains_secret,
    _validate_public_text,
    _validate_run_id,
    canonical_json,
    sha256_file,
    workspace_layout,
)
from intelligence.models import RunLogInventory

_SECRET_MARKERS: tuple[str, ...] = ("secret", "token", "password", "credential", "authorization")
_LogValue = str | int | float | bool | None


def append_run_log(
    workspace: Path,
    run_id: str,
    event: str,
    fields: Mapping[str, _LogValue],
    maximum_bytes: int,
    *,
    severity: str = "info",
    command: str | None = None,
) -> None:
    """Append bounded secret-free typed NDJSON, with at most one truncation event."""
    _validate_run_id(run_id)
    if not event.isidentifier() or severity not in {"debug", "info", "warning", "error"}:
        raise ValueError("log event or severity is invalid")
    if command is not None:
        _validate_public_text(command, "log command")
    _validate_log_details(fields)
    path = workspace_layout(workspace).logs / f"{run_id}.ndjson"
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise ValueError("run log is unsafe")
    payload: dict[str, object] = {
        "timestamp": time.time(),
        "severity": severity,
        "event": event,
        "run_id": run_id,
        "command": command,
        "details": dict(sorted(fields.items())),
    }
    encoded = (canonical_json(payload) + "\n").encode("utf-8")
    existing = path.stat().st_size if path.exists() else 0
    if existing + len(encoded) <= maximum_bytes:
        _append_durable(path, encoded)
        return
    if _log_is_truncated(path):
        return
    truncated = {
        "timestamp": time.time(),
        "severity": "warning",
        "event": "log_truncated",
        "run_id": run_id,
        "command": command,
        "details": {"maximum_bytes": maximum_bytes},
    }
    marker = (canonical_json(truncated) + "\n").encode("utf-8")
    if existing + len(marker) <= maximum_bytes:
        _append_durable(path, marker)


def run_log_inventory(workspace: Path, run_id: str) -> RunLogInventory | None:
    """Return checksummed log evidence only for a safe regular file."""
    path = workspace_layout(workspace).logs / f"{run_id}.ndjson"
    if not path.exists():
        return None
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("run log is unsafe")
    return RunLogInventory(f"runs/logs/{run_id}.ndjson", sha256_file(path), metadata.st_size)


def _log_is_truncated(path: Path) -> bool:
    return path.exists() and b'"event":"log_truncated"' in path.read_bytes()


def _validate_log_details(fields: Mapping[str, _LogValue]) -> None:
    for key, value in fields.items():
        if not key.isidentifier() or _contains_secret(key):
            raise ValueError("log detail names must be safe")
        if not isinstance(value, (str, int, float, bool)) and value is not None:
            raise ValueError("log detail values must be scalar")
        if isinstance(value, str) and _contains_secret(value):
            raise ValueError("log detail values must not contain secrets")
