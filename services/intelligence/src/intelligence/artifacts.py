"""Safe staged-output handling, immutable manifests, and bounded NDJSON logs."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from intelligence.models import OutputInventory, RunLogInventory, TerminalRunState, WorkspaceLayout

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_LIMIT_KEYS = frozenset(
    {"max_log_bytes", "max_output_bytes", "max_output_entries", "max_runtime_seconds"}
)
DEFAULT_MANIFEST_LIMITS: dict[str, int] = {
    "max_log_bytes": 1_000_000,
    "max_output_bytes": 100_000_000,
    "max_output_entries": 10_000,
    "max_runtime_seconds": 3_600,
}
_MANIFEST_BASE_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "command",
        "created_at",
        "started_at",
        "ended_at",
        "state",
        "limits",
        "outputs",
        "run_log",
    }
)
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
    workspace: Path, run_id: str, maximum_bytes: int, maximum_entries: int = 10_000
) -> tuple[OutputInventory, ...]:
    """Return a sorted inventory after rejecting unsafe staged filesystem entries."""
    layout = workspace_layout(workspace)
    _validate_run_id(run_id)
    staging = layout.staging / run_id
    if staging.is_symlink() or not staging.is_dir():
        raise ValueError("run staging directory is missing or unsafe")
    scan_staged_usage(workspace, run_id, maximum_bytes, maximum_entries)
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
    workspace: Path,
    run_id: str,
    inventory: Sequence[OutputInventory],
    maximum_bytes: int,
    maximum_entries: int = 10_000,
) -> tuple[OutputInventory, ...]:
    """Atomically publish a complete pre-scanned run directory without replacement."""
    layout = workspace_layout(workspace)
    _validate_run_id(run_id)
    staging = layout.staging / run_id
    expected = tuple(sorted(inventory, key=lambda item: item.relative_path))
    actual = scan_staged_outputs(layout.root, run_id, maximum_bytes, maximum_entries)
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
    _fsync_directory(destination.parent)
    return tuple(
        OutputInventory(f"outputs/{run_id}/{item.relative_path}", item.sha256, item.byte_count)
        for item in expected
    )


def publish_file(
    workspace: Path,
    run_id: str,
    source: Path,
    maximum_bytes: int,
    maximum_entries: int = 10_000,
) -> OutputInventory:
    """Publish a one-file staging directory through the public compatibility seam."""
    layout = workspace_layout(workspace)
    try:
        relative = source.relative_to(layout.staging / run_id)
    except ValueError as error:
        raise ValueError("output must be inside this run's staging directory") from error
    inventory = scan_staged_outputs(layout.root, run_id, maximum_bytes, maximum_entries)
    matches = tuple(item for item in inventory if item.relative_path == relative.as_posix())
    if len(matches) != 1:
        raise ValueError("output must be one regular file inside this run's staging directory")
    published = publish_inventory(layout.root, run_id, inventory, maximum_bytes, maximum_entries)
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
        "limits": dict(sorted((limits or DEFAULT_MANIFEST_LIMITS).items())),
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
    created_value = _manifest_number(value["created_at"], "created_at")
    started_value = _manifest_number(value["started_at"], "started_at")
    validate_manifest(
        value,
        expected_run_id=run_id,
        expected_command=command,
        expected_state=state,
        expected_outputs=all_outputs,
        expected_reason=reason,
        expected_created_at=created_value,
        expected_started_at=started_value,
        expected_run_log=run_log,
    )
    path = workspace_layout(workspace).manifests / f"{run_id}.json"
    encoded = canonical_json(value)
    try:
        _write_new_file(path, encoded)
        _fsync_directory(path.parent)
    except FileExistsError:
        if _read_regular_text(path) != encoded:
            raise RuntimeError("terminal manifest already exists with different content") from None
    return value


def quarantine_manifest(workspace: Path, run_id: str) -> Path | None:
    """Move an untrusted pre-existing manifest aside without reading or following it."""
    _validate_run_id(run_id)
    layout = workspace_layout(workspace)
    source = layout.manifests / f"{run_id}.json"
    try:
        metadata = source.lstat()
    except FileNotFoundError:
        return None
    destination_directory = layout.staging / run_id
    if destination_directory.is_symlink() or not destination_directory.is_dir():
        raise ValueError("run staging directory is missing or unsafe")
    for _ in range(3):
        destination = destination_directory / f".rejected-manifest-{uuid.uuid4().hex}.json"
        if destination.exists() or destination.is_symlink():
            continue
        if stat.S_ISREG(metadata.st_mode):
            os.link(source, destination, follow_symlinks=False)
            try:
                source.unlink()
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
        else:
            os.rename(source, destination)
        return destination
    raise RuntimeError("could not reserve a safe manifest quarantine path")


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


def scan_staged_usage(
    workspace: Path, run_id: str, maximum_bytes: int, maximum_entries: int = 10_000
) -> None:
    """Bound live staged usage without hashing or materializing the complete tree."""
    layout = workspace_layout(workspace)
    _validate_run_id(run_id)
    if maximum_bytes < 1 or maximum_entries < 1:
        raise ValueError("staged output limits must be positive")
    staging = layout.staging / run_id
    if staging.is_symlink() or not staging.is_dir():
        raise ValueError("run staging directory is missing or unsafe")
    total_bytes = 0
    entries = 0
    pending = [staging]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    entries += 1
                    if entries > maximum_entries:
                        raise RuntimeError("staged outputs exceed configured entry limit")
                    candidate = Path(entry.path)
                    relative = candidate.relative_to(staging)
                    _validate_relative_path(relative)
                    try:
                        metadata = os.stat(entry.path, follow_symlinks=False)
                    except OSError as error:
                        raise ValueError("staged output could not be inspected") from error
                    if stat.S_ISLNK(metadata.st_mode):
                        raise ValueError("staged outputs must not contain symbolic links")
                    if stat.S_ISDIR(metadata.st_mode):
                        pending.append(candidate)
                        continue
                    if not stat.S_ISREG(metadata.st_mode):
                        raise ValueError("staged outputs must contain regular files only")
                    total_bytes += metadata.st_size
                    if total_bytes > maximum_bytes:
                        raise RuntimeError("staged outputs exceed configured byte limit")
        except OSError as error:
            raise ValueError("staged output directory could not be inspected") from error


def canonical_json(value: object) -> str:
    """Encode deterministic JSON used for immutable evidence."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def read_manifest(
    path: Path,
    *,
    expected_run_id: str,
    expected_command: str,
    expected_state: TerminalRunState,
    expected_outputs: Sequence[OutputInventory] = (),
    expected_reason: str | None = None,
    expected_created_at: float | None = None,
    expected_started_at: float | None = None,
    expected_ended_at: float | None = None,
    expected_limits: Mapping[str, int] | None = None,
    expected_run_log: RunLogInventory | None = None,
) -> dict[str, object]:
    """Read and strictly validate immutable parent-owned terminal evidence."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("terminal manifest is unsafe")
    try:
        encoded = path.read_text(encoding="utf-8")
        raw = json.loads(encoded)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("terminal manifest is invalid") from error
    if canonical_json(raw) != encoded:
        raise ValueError("terminal manifest is not canonical")
    return validate_manifest(
        raw,
        expected_run_id=expected_run_id,
        expected_command=expected_command,
        expected_state=expected_state,
        expected_outputs=expected_outputs,
        expected_reason=expected_reason,
        expected_created_at=expected_created_at,
        expected_started_at=expected_started_at,
        expected_ended_at=expected_ended_at,
        expected_limits=expected_limits,
        expected_run_log=expected_run_log,
    )


def validate_manifest(
    value: object,
    *,
    expected_run_id: str,
    expected_command: str,
    expected_state: TerminalRunState,
    expected_outputs: Sequence[OutputInventory] = (),
    expected_reason: str | None = None,
    expected_created_at: float | None = None,
    expected_started_at: float | None = None,
    expected_ended_at: float | None = None,
    expected_limits: Mapping[str, int] | None = None,
    expected_run_log: RunLogInventory | None = None,
) -> dict[str, object]:
    """Validate exact manifest schema, evidence, limits, and public values."""
    if not isinstance(value, dict):
        raise ValueError("terminal manifest must be an object")
    manifest = cast(dict[str, object], value)
    keys = _MANIFEST_BASE_KEYS | ({"reason"} if expected_reason is not None else set())
    if set(manifest) != keys:
        raise ValueError("terminal manifest schema is invalid")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("terminal manifest schema is invalid")
    if manifest.get("run_id") != expected_run_id or manifest.get("command") != expected_command:
        raise ValueError("terminal manifest identity is invalid")
    if manifest.get("state") != expected_state:
        raise ValueError("terminal manifest state is invalid")
    created_at = _manifest_number(manifest.get("created_at"), "created_at")
    started_at = _manifest_number(manifest.get("started_at"), "started_at")
    ended_at = _manifest_number(manifest.get("ended_at"), "ended_at")
    if expected_created_at is not None and created_at != expected_created_at:
        raise ValueError("terminal manifest creation time is invalid")
    if expected_started_at is not None and started_at != expected_started_at:
        raise ValueError("terminal manifest start time is invalid")
    if expected_ended_at is not None and ended_at != expected_ended_at:
        raise ValueError("terminal manifest end time is invalid")
    if started_at < created_at or ended_at < started_at:
        raise ValueError("terminal manifest timestamps are invalid")
    raw_limits = manifest.get("limits")
    if not isinstance(raw_limits, dict) or set(raw_limits) != MANIFEST_LIMIT_KEYS:
        raise ValueError("terminal manifest limits are invalid")
    for key, limit in raw_limits.items():
        if not isinstance(key, str) or not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("terminal manifest limits are invalid")
        if limit < 1:
            raise ValueError("terminal manifest limits are invalid")
    if expected_limits is not None and dict(raw_limits) != dict(expected_limits):
        raise ValueError("terminal manifest limits do not match expected configuration")
    actual_outputs = _manifest_outputs(manifest.get("outputs"))
    expected_output_values = tuple(sorted(expected_outputs, key=lambda item: item.relative_path))
    if actual_outputs != expected_output_values:
        raise ValueError("terminal manifest outputs are invalid")
    actual_log = _manifest_log(manifest.get("run_log"), expected_run_id)
    if actual_log != expected_run_log:
        raise ValueError("terminal manifest log evidence is invalid")
    if expected_reason is None:
        if "reason" in manifest:
            raise ValueError("terminal manifest reason is invalid")
    elif manifest.get("reason") != expected_reason:
        raise ValueError("terminal manifest reason is invalid")
    return manifest


def _manifest_number(value: object, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"terminal manifest {field} is invalid")
    return float(value)


def _manifest_outputs(value: object) -> tuple[OutputInventory, ...]:
    if not isinstance(value, list):
        raise ValueError("terminal manifest outputs are invalid")
    outputs: list[OutputInventory] = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"byte_count", "path", "sha256"}:
            raise ValueError("terminal manifest outputs are invalid")
        path, digest, byte_count = entry.get("path"), entry.get("sha256"), entry.get("byte_count")
        if (
            not isinstance(path, str)
            or not isinstance(digest, str)
            or not isinstance(byte_count, int)
            or isinstance(byte_count, bool)
        ):
            raise ValueError("terminal manifest outputs are invalid")
        relative = Path(path)
        _validate_relative_path(relative)
        if (
            len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or byte_count < 0
        ):
            raise ValueError("terminal manifest outputs are invalid")
        outputs.append(OutputInventory(path, digest, byte_count))
    return tuple(sorted(outputs, key=lambda item: item.relative_path))


def _manifest_log(value: object, run_id: str) -> RunLogInventory | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"byte_count", "path", "sha256"}:
        raise ValueError("terminal manifest log evidence is invalid")
    path, digest, byte_count = value.get("path"), value.get("sha256"), value.get("byte_count")
    if (
        not isinstance(path, str)
        or path != f"runs/logs/{run_id}.ndjson"
        or not isinstance(digest, str)
        or not isinstance(byte_count, int)
        or isinstance(byte_count, bool)
        or byte_count < 0
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise ValueError("terminal manifest log evidence is invalid")
    return RunLogInventory(path, digest, byte_count)


def _ensure_directory(path: Path) -> None:
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise ValueError("workspace layout path is unsafe")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


def _write_new_file(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        os.link(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _append_durable(path: Path, content: bytes) -> None:
    with path.open("ab") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    """Persist a directory entry where the host supports directory fsync."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
