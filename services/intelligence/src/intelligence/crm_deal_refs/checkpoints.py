"""No-replace page evidence and safe canonical JSON helpers for private staging."""

from __future__ import annotations

import json
import os
import stat
import uuid
from collections.abc import Sequence
from pathlib import Path

from intelligence.artifacts import canonical_json, sha256_file


def write_new_json(path: Path, value: object) -> None:
    """Write canonical JSON once; an existing path is a conflict, never overwrite permission."""
    _ensure_parent(path)
    _write_new_bytes(path, canonical_json(value).encode("utf-8"))


def replace_json(path: Path, value: object) -> None:
    """Atomically advance the single mutable checkpoint only after page evidence is durable."""
    _ensure_parent(path)
    payload = canonical_json(value).encode("utf-8")
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    _write_new_bytes(temporary, payload)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def write_new_page(path: Path, rows: Sequence[object]) -> tuple[str, int]:
    """Write a canonical regular NDJSON page without replacing a committed page."""
    _ensure_parent(path)
    payload = "".join(canonical_json(row) + "\n" for row in rows).encode("utf-8")
    _write_new_bytes(path, payload)
    return sha256_file(path), len(payload)


def read_json(path: Path) -> object:
    """Read canonical regular JSON only; symlinks and non-regular entries fail closed."""
    _regular(path)
    encoded = path.read_text(encoding="utf-8")
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError as error:
        raise ValueError("snapshot JSON is invalid") from error
    if canonical_json(value) != encoded:
        raise ValueError("snapshot JSON is not canonical")
    return value


def regular_inventory(root: Path) -> tuple[str, ...]:
    """Return the complete regular-file inventory and reject links, devices, and hard links."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("snapshot root is unsafe")
    paths: list[str] = []
    for candidate in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("snapshot inventory contains unsafe evidence")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("snapshot inventory contains unsafe evidence")
        if metadata.st_nlink != 1:
            raise ValueError("snapshot inventory contains hard-linked evidence")
        paths.append(candidate.relative_to(root).as_posix())
    return tuple(paths)


def copy_regular_file(
    source: Path, target: Path, expected_sha256: str, expected_bytes: int
) -> None:
    """Copy one verified regular file once, rejecting byte conflicts and unsafe parent paths."""
    _regular(source)
    if source.stat().st_size != expected_bytes or sha256_file(source) != expected_sha256:
        raise ValueError("prior snapshot file checksum is invalid")
    _ensure_parent(target)
    if target.exists() or target.is_symlink():
        raise FileExistsError("resume target evidence already exists")
    _write_new_bytes(target, source.read_bytes())
    if target.stat().st_size != expected_bytes or sha256_file(target) != expected_sha256:
        raise RuntimeError("copied snapshot evidence checksum is invalid")


def _write_new_bytes(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise ValueError("snapshot path is unsafe")


def _regular(path: Path) -> None:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ValueError("snapshot evidence is unsafe")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
