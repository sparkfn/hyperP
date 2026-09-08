"""Atomic private file primitives for durable CRM activity checkpoints."""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path

from intelligence.crm.activities.path_safety import has_link_or_reparse


def read_bytes(path: Path) -> bytes:
    """Read one admitted regular checkpoint file."""
    metadata = path.lstat()
    if (
        has_link_or_reparse(metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ValueError("checkpoint evidence is unsafe")
    return path.read_bytes()


def publish_new(path: Path, payload: bytes) -> None:
    """Publish a new immutable file using private-write then hard-link commit."""
    temporary = temporary_path(path)
    published = False
    try:
        write_private(temporary, payload)
        os.link(temporary, path)
        published = True
        temporary.unlink()
        fsync(path.parent)
    except BaseException:
        if published:
            remove_published(path)
        remove_if_present(temporary)
        raise


def publish_replace(path: Path, payload: bytes) -> None:
    """Atomically replace mutable checkpoint state evidence."""
    temporary = temporary_path(path)
    try:
        write_private(temporary, payload)
        os.replace(temporary, path)
        fsync(path.parent)
    except BaseException:
        remove_if_present(temporary)
        raise


def exists(path: Path) -> bool:
    """Check a path without following its final component."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def fsync(path: Path) -> None:
    """Synchronize a directory where supported by the host filesystem."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def remove_published(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and not has_link_or_reparse(metadata)
    ):
        path.unlink()
        fsync(path.parent)


def remove_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def temporary_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
