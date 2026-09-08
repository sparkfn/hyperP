"""No-replace page evidence and safe canonical JSON helpers for private staging."""

from __future__ import annotations

import json
import os
import stat
import uuid
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path

from intelligence.artifacts import canonical_json, sha256_file
from intelligence.crm_deal_refs.models import MAX_METADATA_BYTES, MAX_PAGE_BYTES, MAX_RECORD_BYTES


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
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    digest = sha256()
    size = 0
    try:
        with os.fdopen(descriptor, "wb") as handle:
            for row in rows:
                encoded = (canonical_json(row) + "\n").encode("utf-8")
                if len(encoded) > MAX_RECORD_BYTES or size + len(encoded) > MAX_PAGE_BYTES:
                    raise ValueError("snapshot page exceeds bounded record or page size")
                handle.write(encoded)
                digest.update(encoded)
                size += len(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    _fsync_directory(path.parent)
    return digest.hexdigest(), size


def read_json(path: Path, maximum_bytes: int = MAX_METADATA_BYTES) -> object:
    """Read canonical regular JSON only; symlinks and non-regular entries fail closed."""
    _regular(path)
    encoded = _read_bounded_text(path, maximum_bytes, "snapshot JSON")
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


def require_confined_directory(
    workspace: Path, target: Path, *, allow_missing: bool = False
) -> Path:
    """Reject links in every existing component from a trusted workspace to a domain directory."""
    trusted = workspace.absolute()
    candidate = target.absolute()
    try:
        relative = candidate.relative_to(trusted)
    except ValueError as error:
        raise ValueError("snapshot path escapes the trusted workspace") from error
    _directory(trusted, "trusted workspace")
    current = trusted
    for part in relative.parts:
        current = current / part
        try:
            _directory(current, "snapshot path")
        except FileNotFoundError:
            if allow_missing:
                return candidate
            raise ValueError("snapshot path is unavailable") from None
    return candidate


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
    _copy_bounded_bytes(source, target, expected_bytes)
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
    _directory(path.parent, "snapshot path")


def _regular(path: Path) -> None:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ValueError("snapshot evidence is unsafe")


def _directory(path: Path, field: str) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{field} is unsafe")


def _read_bounded_text(path: Path, maximum_bytes: int, field: str) -> str:
    if maximum_bytes < 1:
        raise ValueError("snapshot read limit is invalid")
    if path.stat().st_size > maximum_bytes:
        raise ValueError(f"{field} exceeds size limit")
    with path.open("rb") as handle:
        encoded = handle.read(maximum_bytes + 1)
    if len(encoded) > maximum_bytes:
        raise ValueError(f"{field} exceeds size limit")
    try:
        return encoded.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{field} is invalid") from error


def _copy_bounded_bytes(source: Path, target: Path, expected_bytes: int) -> None:
    copied = 0
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
            while chunk := reader.read(1024 * 1024):
                copied += len(chunk)
                if copied > expected_bytes:
                    raise ValueError("prior snapshot file exceeds expected size")
                writer.write(chunk)
            if copied != expected_bytes:
                raise ValueError("prior snapshot file size changed during copy")
            writer.flush()
            os.fsync(writer.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    _fsync_directory(target.parent)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
