"""Pinned root lifecycle and locking for restricted artifact storage."""

from __future__ import annotations

import fcntl
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from src.connectors.bitrix_stage_history import artifact_fs_primitives as fs

SESSIONS_DIRECTORY = ".sessions"
PREPARING_DIRECTORY = ".preparing"
OBJECTS_DIRECTORY = ".objects"
SEALED_DIRECTORY = "sealed"
_LOCK_NAME = ".artifact-store.lock"


@dataclass
class RootState:
    path: Path
    root_fd: int
    sessions_fd: int
    preparing_fd: int
    objects_fd: int
    sealed_fd: int
    lock_fd: int = -1


def open_root_state(path: Path) -> RootState:
    root_path, root_fd = fs.open_root(path)
    descriptors: list[int] = []
    try:
        for name in (
            SESSIONS_DIRECTORY,
            PREPARING_DIRECTORY,
            OBJECTS_DIRECTORY,
            SEALED_DIRECTORY,
        ):
            descriptors.append(fs.open_or_create_private_directory(root_fd, name))
        return RootState(root_path, root_fd, *descriptors)
    except BaseException:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.close(root_fd)
        except OSError:
            pass
        raise


def validate_distinct_states(primary: RootState, backup: RootState) -> None:
    paths_overlap = (
        primary.path == backup.path
        or primary.path.is_relative_to(backup.path)
        or backup.path.is_relative_to(primary.path)
    )
    if paths_overlap:
        raise ValueError("artifact primary and backup roots must not overlap")
    primary_details = os.fstat(primary.root_fd)
    backup_details = os.fstat(backup.root_fd)
    if (primary_details.st_dev, primary_details.st_ino) == (
        backup_details.st_dev,
        backup_details.st_ino,
    ):
        raise ValueError("artifact primary and backup roots must not alias")


def acquire_root_locks(primary: RootState, backup: RootState) -> None:
    for state in sorted((primary, backup), key=lambda item: str(item.path)):
        state.lock_fd = _acquire_lock(state.root_fd)


def close_root_state(state: RootState) -> None:
    lock_fd = state.lock_fd
    state.lock_fd = -1
    descriptors: list[int] = []
    for attribute in (
        "sessions_fd",
        "preparing_fd",
        "objects_fd",
        "sealed_fd",
        "root_fd",
    ):
        descriptors.append(getattr(state, attribute))
        setattr(state, attribute, -1)
    errors: list[OSError] = []
    if lock_fd >= 0:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError as exc:
            errors.append(exc)
        try:
            os.close(lock_fd)
        except OSError as exc:
            errors.append(exc)
    for descriptor in descriptors:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as exc:
                errors.append(exc)
    if errors:
        raise errors[0]


def _acquire_lock(root_fd: int) -> int:
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(_LOCK_NAME, flags, 0o600, dir_fd=root_fd)
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or stat.S_IMODE(details.st_mode) & 0o077
        ):
            raise ValueError("artifact store lock must be a private single-link regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise RuntimeError("artifact store root is already active") from exc
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor
