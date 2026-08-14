"""Descriptor-relative filesystem primitives for restricted artifacts."""

from __future__ import annotations

import ctypes
import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from src.connectors.bitrix_stage_history.artifact_manifest import ArtifactFileDigest

READ_SIZE = 1024 * 1024
_RENAME_NOREPLACE = 1


class _RenameAt2(Protocol):
    argtypes: list[object]
    restype: object

    def __call__(
        self,
        source_fd: int,
        source_name: bytes,
        destination_fd: int,
        destination_name: bytes,
        flags: int,
    ) -> int: ...


def open_root(path: Path) -> tuple[Path, int]:
    """Open a private root owned by the trusted storage service identity."""
    if ".." in path.parts:
        raise ValueError("restricted artifact roots cannot contain parent traversal")
    absolute = path.absolute()
    descriptor = os.open(absolute.anchor, _directory_flags())
    try:
        for part in absolute.parts[1:]:
            try:
                os.mkdir(part, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
            except FileExistsError:
                pass
            details = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(details.st_mode):
                raise ValueError("restricted artifact roots cannot have symlinked ancestors")
            if not stat.S_ISDIR(details.st_mode):
                raise ValueError("restricted artifact root ancestry must contain directories")
            child = os.open(part, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        details = os.fstat(descriptor)
        if details.st_uid != os.geteuid():
            raise ValueError("restricted artifact root must be owned by the service identity")
        if stat.S_IMODE(details.st_mode) & 0o077:
            raise ValueError("restricted artifact root permissions are too broad")
        return absolute, descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_or_create_private_directory(parent_fd: int, name: str) -> int:
    validate_flat_name(name)
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileExistsError:
        pass
    descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    try:
        details = os.fstat(descriptor)
        if stat.S_IMODE(details.st_mode) & 0o077:
            raise ValueError("restricted artifact directory permissions are too broad")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def create_private_directory(parent_fd: int, name: str) -> int:
    validate_flat_name(name)
    os.mkdir(name, 0o700, dir_fd=parent_fd)
    os.fsync(parent_fd)
    try:
        return os.open(name, _directory_flags(), dir_fd=parent_fd)
    except BaseException:
        os.rmdir(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        raise


def open_private_directory(parent_fd: int, name: str) -> int:
    validate_flat_name(name)
    descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    try:
        details = os.fstat(descriptor)
        if stat.S_IMODE(details.st_mode) & 0o077:
            raise RuntimeError("artifact directory permissions are too broad")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_regular_file(parent_fd: int, name: str, *, require_single_link: bool = True) -> int:
    validate_flat_name(name)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or (require_single_link and details.st_nlink != 1):
            raise ValueError("restricted artifacts require single-link regular files")
        if stat.S_IMODE(details.st_mode) & 0o077:
            raise ValueError("restricted artifact file permissions are too broad")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_new_file(parent_fd: int, name: str, mode: int) -> int:
    validate_flat_name(name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(name, flags, mode, dir_fd=parent_fd)


def write_new_file(
    parent_fd: int,
    name: str,
    content: bytes,
    *,
    mode: int,
    guard: Callable[[], None] | None = None,
) -> None:
    _run_guard(guard)
    descriptor = open_new_file(parent_fd, name, mode)
    try:
        write_all(descriptor, content, guard=guard)
        _run_guard(guard)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _run_guard(guard)
    os.fsync(parent_fd)


def rename_entry_no_replace(
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
    *,
    guard: Callable[[], None] | None = None,
) -> None:
    """Atomically publish one flat entry without replacing an existing name."""
    validate_flat_name(source_name)
    validate_flat_name(destination_name)
    library = ctypes.CDLL(None, use_errno=True)
    raw_rename = getattr(library, "renameat2", None)
    if raw_rename is None:
        raise RuntimeError("atomic no-replace artifact publication is unavailable")
    renameat2 = cast(_RenameAt2, raw_rename)
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    source_encoded = os.fsencode(source_name)
    destination_encoded = os.fsencode(destination_name)
    _run_guard(guard)
    if (
        renameat2(
            source_fd,
            source_encoded,
            destination_fd,
            destination_encoded,
            _RENAME_NOREPLACE,
        )
        != 0
    ):
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination_name)
    _run_guard(guard)
    os.fsync(source_fd)
    _run_guard(guard)
    os.fsync(destination_fd)


def read_file(parent_fd: int, name: str, *, max_bytes: int, immutable: bool) -> bytes:
    descriptor = open_regular_file(parent_fd, name)
    try:
        details = os.fstat(descriptor)
        if immutable and stat.S_IMODE(details.st_mode) & 0o222:
            raise RuntimeError("sealed artifact evidence file is writable")
        if details.st_size > max_bytes:
            raise RuntimeError("sealed artifact evidence exceeds its byte limit")
        chunks: list[bytes] = []
        byte_count = 0
        while chunk := os.read(descriptor, min(READ_SIZE, max_bytes + 1 - byte_count)):
            chunks.append(chunk)
            byte_count += len(chunk)
            if byte_count > max_bytes:
                raise RuntimeError("sealed artifact evidence exceeds its byte limit")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def snapshot_file(
    source_fd: int,
    destination_fd: int,
    name: str,
    *,
    max_file_bytes: int,
    guard: Callable[[], None] | None = None,
) -> ArtifactFileDigest:
    _run_guard(guard)
    source = open_regular_file(source_fd, name)
    try:
        destination = open_new_file(destination_fd, name, 0o600)
        digest = hashlib.sha256()
        try:
            before = os.fstat(source)
            if before.st_size > max_file_bytes:
                raise RuntimeError("artifact source file exceeds its byte limit")
            byte_count = _copy_stream(source, destination, digest, max_file_bytes, guard=guard)
            _run_guard(guard)
            os.fsync(destination)
            after = os.fstat(source)
            if _mutation_identity(before) != _mutation_identity(after):
                raise RuntimeError("artifact source file changed during snapshot")
            return ArtifactFileDigest(name, digest.hexdigest(), byte_count)
        finally:
            os.close(destination)
    finally:
        os.close(source)


def copy_verified_file(
    source_fd: int,
    destination_fd: int,
    expected: ArtifactFileDigest,
    *,
    guard: Callable[[], None] | None = None,
) -> None:
    _run_guard(guard)
    source = open_regular_file(source_fd, expected.relative_path)
    try:
        target = open_new_file(destination_fd, expected.relative_path, 0o600)
        digest = hashlib.sha256()
        try:
            byte_count = _copy_stream(source, target, digest, expected.byte_count, guard=guard)
            _run_guard(guard)
            os.fsync(target)
        finally:
            os.close(target)
    finally:
        os.close(source)
    if digest.hexdigest() != expected.sha256 or byte_count != expected.byte_count:
        raise RuntimeError("artifact backup digest verification failed")


def verify_data_file(parent_fd: int, expected: ArtifactFileDigest) -> None:
    descriptor = open_regular_file(parent_fd, expected.relative_path)
    try:
        details = os.fstat(descriptor)
        if stat.S_IMODE(details.st_mode) & 0o222:
            raise RuntimeError("sealed artifact contains a writable file")
        digest = hashlib.sha256()
        byte_count = 0
        while chunk := os.read(descriptor, READ_SIZE):
            digest.update(chunk)
            byte_count += len(chunk)
            if byte_count > expected.byte_count:
                raise RuntimeError("sealed artifact digest verification failed")
        if digest.hexdigest() != expected.sha256 or byte_count != expected.byte_count:
            raise RuntimeError("sealed artifact digest verification failed")
    finally:
        os.close(descriptor)


def make_directory_immutable(
    directory_fd: int,
    *,
    guard: Callable[[], None] | None = None,
) -> None:
    for name in sorted(os.listdir(directory_fd)):
        _run_guard(guard)
        descriptor = open_regular_file(directory_fd, name)
        try:
            _run_guard(guard)
            os.fchmod(descriptor, 0o400)
            _run_guard(guard)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    _run_guard(guard)
    os.fchmod(directory_fd, 0o500)
    _run_guard(guard)
    os.fsync(directory_fd)


def validate_immutable_directory(
    directory_fd: int,
    expected: tuple[ArtifactFileDigest, ...],
    manifest_name: str,
) -> None:
    details = os.fstat(directory_fd)
    if stat.S_IMODE(details.st_mode) & 0o222:
        raise RuntimeError("sealed artifact directory is writable")
    expected_names = tuple(item.relative_path for item in expected) + (manifest_name,)
    if tuple(sorted(os.listdir(directory_fd))) != tuple(sorted(expected_names)):
        raise RuntimeError("sealed artifact file inventory does not match manifest")
    manifest = open_regular_file(directory_fd, manifest_name)
    try:
        if stat.S_IMODE(os.fstat(manifest).st_mode) & 0o222:
            raise RuntimeError("sealed artifact manifest is writable")
    finally:
        os.close(manifest)
    for item in expected:
        verify_data_file(directory_fd, item)


def remove_flat_directory(parent_fd: int, name: str) -> None:
    try:
        directory_fd = open_private_directory(parent_fd, name)
    except FileNotFoundError:
        return
    try:
        os.fchmod(directory_fd, 0o700)
        for entry_name in os.listdir(directory_fd):
            entry = os.stat(entry_name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(entry.st_mode) and not stat.S_ISLNK(entry.st_mode):
                raise RuntimeError("flat artifact storage contained an unexpected directory")
            os.unlink(entry_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        pinned = os.fstat(directory_fd)
        if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise RuntimeError("artifact cleanup directory identity changed")
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def remove_file(parent_fd: int, name: str) -> None:
    try:
        details = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(details.st_mode):
        raise RuntimeError("artifact cleanup target is not a regular file")
    os.unlink(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def remove_file_identity(parent_fd: int, name: str, device: int, inode: int) -> None:
    details = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(details.st_mode) or (details.st_dev, details.st_ino) != (
        device,
        inode,
    ):
        raise RuntimeError("artifact cleanup file identity changed")
    os.unlink(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def clear_flat_directory(parent_fd: int) -> None:
    for name in os.listdir(parent_fd):
        details = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISDIR(details.st_mode) and not stat.S_ISLNK(details.st_mode):
            remove_flat_directory(parent_fd, name)
        else:
            os.unlink(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def validate_flat_name(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("artifact file name must be a safe flat name")


def write_all(
    descriptor: int,
    content: bytes,
    *,
    guard: Callable[[], None] | None = None,
) -> None:
    view = memoryview(content)
    while view:
        _run_guard(guard)
        written = os.write(descriptor, view)
        if written < 1:
            raise OSError("artifact file write did not advance")
        view = view[written:]


def _copy_stream(
    source: int,
    destination: int,
    digest: hashlib._Hash,
    limit: int,
    *,
    guard: Callable[[], None] | None,
) -> int:
    byte_count = 0
    while chunk := os.read(source, min(READ_SIZE, limit + 1 - byte_count)):
        write_all(destination, chunk, guard=guard)
        digest.update(chunk)
        byte_count += len(chunk)
        if byte_count > limit:
            raise RuntimeError("artifact file exceeds its byte limit")
    return byte_count


def _run_guard(guard: Callable[[], None] | None) -> None:
    if guard is not None:
        guard()


def _mutation_identity(details: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _directory_flags() -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags
