"""Safe staged-output handling, immutable manifests, and bounded NDJSON logs."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from intelligence.models import OutputInventory, RunLogInventory, TerminalRunState, WorkspaceLayout

MANIFEST_SCHEMA_VERSION = 1
_SECRET_MARKERS: tuple[str, ...] = ("secret", "token", "password", "credential", "authorization")
_LogValue = str | int | float | bool | None


def workspace_layout(workspace: Path) -> WorkspaceLayout:
    """Create and return the fixed private workspace layout without following child links."""
    root = workspace.resolve()
    _ensure_directory(root)
    state_directory = root / "state"
    staging = root / "staging"
    runs = root / "runs"
    manifests = runs / "manifests"
    logs = runs / "logs"
    outputs = root / "outputs"
    backups = root / "backups"
    for path in (state_directory, staging, runs, manifests, logs, outputs, backups):
        _ensure_directory(path)
    return WorkspaceLayout(
        root=root,
        state_directory=state_directory,
        state_database=state_directory / "state.sqlite3",
        staging=staging,
        runs=runs,
        manifests=manifests,
        logs=logs,
        outputs=outputs,
        backups=backups,
    )


def scan_staged_outputs(
    workspace: Path, run_id: str, maximum_bytes: int
) -> tuple[OutputInventory, ...]:
    """Return a sorted inventory after rejecting unsafe staged filesystem entries."""
    layout = workspace_layout(workspace)
    _validate_run_id(run_id)
    staging = layout.staging / run_id
    if staging.is_symlink() or not staging.is_dir():
        raise ValueError("run staging directory is missing or unsafe")
    total = 0
    inventory: list[OutputInventory] = []
    for candidate in sorted(staging.rglob("*"), key=lambda path: path.as_posix()):
        relative = candidate.relative_to(staging)
        _validate_relative_path(relative)
        metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("staged outputs must not contain symbolic links")
        if candidate.is_dir():
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("staged outputs must contain regular files only")
        byte_count = metadata.st_size
        total += byte_count
        if byte_count > maximum_bytes or total > maximum_bytes:
            raise RuntimeError("staged outputs exceed configured byte limit")
        inventory.append(OutputInventory(relative.as_posix(), sha256_file(candidate), byte_count))
    return tuple(inventory)


def publish_inventory(
    workspace: Path, run_id: str, inventory: Sequence[OutputInventory], maximum_bytes: int
) -> tuple[OutputInventory, ...]:
    """Atomically publish a complete pre-scanned run directory without replacement."""
    layout = workspace_layout(workspace)
    _validate_run_id(run_id)
    staging = layout.staging / run_id
    expected = tuple(sorted(inventory, key=lambda item: item.relative_path))
    actual = scan_staged_outputs(layout.root, run_id, maximum_bytes)
    if actual != expected:
        raise RuntimeError("staged output inventory changed before publication")
    destination = layout.outputs / run_id
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("accepted output directory already exists")
    try:
        os.rename(staging, destination)
    except FileExistsError as error:
        raise FileExistsError("accepted output directory already exists") from error
    except OSError as error:
        raise RuntimeError("output publication requires the workspace same volume") from error
    return tuple(
        OutputInventory(f"outputs/{run_id}/{item.relative_path}", item.sha256, item.byte_count)
        for item in expected
    )


def publish_file(workspace: Path, run_id: str, source: Path, maximum_bytes: int) -> OutputInventory:
    """Publish a one-file staging directory through the public compatibility seam."""
    layout = workspace_layout(workspace)
    try:
        relative = source.relative_to(layout.staging / run_id)
    except ValueError as error:
        raise ValueError("output must be inside this run's staging directory") from error
    inventory = scan_staged_outputs(layout.root, run_id, maximum_bytes)
    matches = tuple(item for item in inventory if item.relative_path == relative.as_posix())
    if len(matches) != 1:
        raise ValueError("output must be one regular file inside this run's staging directory")
    published = publish_inventory(layout.root, run_id, inventory, maximum_bytes)
    return published[
        tuple(item.relative_path for item in inventory).index(matches[0].relative_path)
    ]


def write_manifest(
    workspace: Path,
    run_id: str,
    command: str,
    state: TerminalRunState,
    output: OutputInventory | None = None,
    outputs: Sequence[OutputInventory] = (),
    reason: str | None = None,
    *,
    created_at: float | None = None,
    started_at: float | None = None,
    ended_at: float | None = None,
    limits: Mapping[str, int] | None = None,
    run_log: RunLogInventory | None = None,
) -> dict[str, object]:
    """Write one canonical secret-free terminal manifest, accepting identical retries."""
    _validate_run_id(run_id)
    _validate_public_text(command, "command")
    if reason is not None:
        _validate_public_text(reason, "manifest reason")
    all_outputs = tuple(outputs) if outputs else (() if output is None else (output,))
    now = time.time() if ended_at is None else ended_at
    value: dict[str, object] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "command": command,
        "created_at": created_at if created_at is not None else now,
        "started_at": started_at
        if started_at is not None
        else created_at
        if created_at is not None
        else now,
        "ended_at": now,
        "state": state,
        "limits": dict(sorted((limits or {}).items())),
        "outputs": [
            {"byte_count": item.byte_count, "path": item.relative_path, "sha256": item.sha256}
            for item in sorted(all_outputs, key=lambda item: item.relative_path)
        ],
        "run_log": None
        if run_log is None
        else {
            "byte_count": run_log.byte_count,
            "path": run_log.relative_path,
            "sha256": run_log.sha256,
        },
    }
    if reason is not None:
        value["reason"] = reason
    path = workspace_layout(workspace).manifests / f"{run_id}.json"
    encoded = canonical_json(value)
    try:
        _write_new_file(path, encoded)
    except FileExistsError:
        if _read_regular_text(path) != encoded:
            raise RuntimeError("terminal manifest already exists with different content") from None
    return value


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


def sha256_file(path: Path) -> str:
    """Calculate a SHA-256 digest for a regular local file."""
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("artifact must be a regular file")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: object) -> str:
    """Encode deterministic JSON used for immutable evidence."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _ensure_directory(path: Path) -> None:
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise ValueError("workspace layout path is unsafe")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


def _write_new_file(path: Path, content: str) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _append_durable(path: Path, content: bytes) -> None:
    with path.open("ab") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _read_regular_text(path: Path) -> str:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("manifest is unsafe")
    return path.read_text(encoding="utf-8")


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


def _validate_public_text(value: str, field: str) -> None:
    if not value or _contains_secret(value):
        raise ValueError(f"{field} must be non-empty and secret-free")


def _validate_relative_path(path: Path) -> None:
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("output path must be a safe relative path")


def _validate_run_id(run_id: str) -> None:
    if not run_id or "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
        raise ValueError("run identifier is unsafe")


def _contains_secret(value: str) -> bool:
    return any(marker in value.lower() for marker in _SECRET_MARKERS)
