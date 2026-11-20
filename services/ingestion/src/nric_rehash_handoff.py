"""Secure host-local JSONL handoff files for unresolved NRIC anchors."""

from __future__ import annotations

import json
import os
import secrets
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from src.nric_rehash_plan import NricRehashError


@dataclass(frozen=True)
class HandoffEntry:
    person_id: str
    reason: str
    identifier_ids: tuple[str, ...]
    source_record_pks: tuple[str, ...]


class HandoffSummary(TypedDict):
    total: int
    by_reason: dict[str, int]
    file: str | None


def validate_handoff_dir(path: Path) -> None:
    """Require an existing owner-only directory without following symlinks."""
    directory_fd = _open_handoff_dir(path)
    os.close(directory_fd)


def write_handoff_file(path: Path, entries: Sequence[HandoffEntry]) -> str | None:
    """Write sorted, minimal handoff records and return only the created basename."""
    if not entries:
        return None
    directory_fd = _open_handoff_dir(path)
    file_fd: int | None = None
    created = False
    basename = _handoff_basename()
    try:
        file_fd = os.open(
            basename,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory_fd,
        )
        created = True
        os.fchmod(file_fd, 0o600)
        payload = _encode_entries(entries)
        _write_all(file_fd, payload)
        os.fsync(file_fd)
        return basename
    except (OSError, TypeError, UnicodeError, ValueError):
        if created:
            try:
                os.unlink(basename, dir_fd=directory_fd)
            except OSError:
                pass
        raise NricRehashError("handoff_write_failed") from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)


def handoff_summary(entries: Sequence[HandoffEntry], basename: str | None) -> HandoffSummary:
    by_reason: dict[str, int] = {}
    for entry in entries:
        by_reason[entry.reason] = by_reason.get(entry.reason, 0) + 1
    return {"total": len(entries), "by_reason": dict(sorted(by_reason.items())), "file": basename}


def _open_handoff_dir(path: Path) -> int:
    directory_fd: int | None = None
    try:
        if ".." in path.parts:
            raise NricRehashError("handoff_dir_unsafe")
        parts = path.parts[1:] if path.is_absolute() else path.parts
        current = "/" if path.is_absolute() else "."
        directory_fd = os.open(
            current,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        for part in parts:
            if part in ("", ".", ".."):
                raise NricRehashError("handoff_dir_unsafe")
            child_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = child_fd
        metadata = os.fstat(directory_fd)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise NricRehashError("handoff_dir_unsafe")
        return directory_fd
    except NricRehashError:
        if directory_fd is not None:
            os.close(directory_fd)
        raise
    except OSError:
        if directory_fd is not None:
            os.close(directory_fd)
        raise NricRehashError("handoff_dir_unsafe") from None


def _handoff_basename() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"nric-rehash-handoff-{timestamp}-{secrets.token_hex(4)}.jsonl"


def _encode_entries(entries: Sequence[HandoffEntry]) -> bytes:
    lines = [
        json.dumps(
            {
                "person_id": entry.person_id,
                "reason": entry.reason,
                "identifier_ids": sorted(entry.identifier_ids),
                "source_record_pks": sorted(entry.source_record_pks)[:50],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        for entry in sorted(entries, key=lambda item: item.person_id)
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _write_all(file_fd: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(file_fd, remaining)
        if written <= 0:
            raise OSError("handoff write made no progress")
        remaining = remaining[written:]
