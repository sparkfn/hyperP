"""Shared filesystem, path-safety, and durability primitives."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import stat
import uuid
from pathlib import Path

from intelligence.models import WorkspaceLayout

_SECRET_MARKERS: tuple[str, ...] = ("secret", "token", "password", "credential", "authorization")


def workspace_layout(workspace: Path) -> WorkspaceLayout:
    """Create and return the fixed private workspace layout without following child links."""
    root = workspace.resolve()
    _ensure_directory(root)
    state_directory = root / "state"
    staging = root / "staging"
    runs = root / "runs"
    manifests = runs / "manifests"
    rejected_manifests = runs / "rejected-manifests"
    logs = runs / "logs"
    outputs = root / "outputs"
    backups = root / "backups"
    for path in (
        state_directory,
        staging,
        runs,
        manifests,
        rejected_manifests,
        logs,
        outputs,
        backups,
    ):
        _ensure_directory(path)
    return WorkspaceLayout(
        root=root,
        state_directory=state_directory,
        state_database=state_directory / "state.sqlite3",
        staging=staging,
        runs=runs,
        manifests=manifests,
        rejected_manifests=rejected_manifests,
        logs=logs,
        outputs=outputs,
        backups=backups,
    )


def canonical_json(value: object) -> str:
    """Encode deterministic JSON used for immutable evidence."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


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


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically install a path only when its destination does not exist."""
    if os.name == "nt":
        os.rename(source, destination)
        return
    try:
        library = ctypes.CDLL(None, use_errno=True)
        renameat2 = library.renameat2
    except AttributeError as error:
        raise RuntimeError("atomic no-replace rename is unavailable") from error
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(destination),
        1,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise FileExistsError(destination)
        raise OSError(error_number, os.strerror(error_number), str(destination))


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
