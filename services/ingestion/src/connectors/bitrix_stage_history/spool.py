"""Restricted SQLite storage for stage-history capability evidence."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from pathlib import Path
from typing import Protocol


class _Digest(Protocol):
    def update(self, value: bytes) -> None: ...


class RestrictedSpool:
    """SQLite-only spool containing stable IDs and hashes, never source payloads."""

    def __init__(self, directory: Path, pass_number: int) -> None:
        if isinstance(pass_number, bool) or not isinstance(pass_number, int) or pass_number < 1:
            raise ValueError("pass_number must be positive")
        _prepare_restricted_directory(directory)
        self.path = directory / f"stage-history-pass-{pass_number}.sqlite3"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        os.close(fd)
        try:
            self._connection = sqlite3.connect(self.path)
            self._create_schema()
        except BaseException:
            connection = getattr(self, "_connection", None)
            if isinstance(connection, sqlite3.Connection):
                connection.close()
            for spool_path in spool_storage_paths(self.path):
                spool_path.unlink(missing_ok=True)
            raise

    def _create_schema(self) -> None:
        self._connection.execute(
            "CREATE TABLE events ("
            "stable_id TEXT NOT NULL, "
            "canonical_hash TEXT NOT NULL, "
            "occurrence_count INTEGER NOT NULL, "
            "PRIMARY KEY (stable_id, canonical_hash)"
            ")"
        )
        self._connection.commit()

    def add(self, stable_id: str, canonical_hash: str) -> str:
        """Store one identity/hash variant and classify the observation."""
        exact_row = self._connection.execute(
            "SELECT occurrence_count FROM events WHERE stable_id = ? AND canonical_hash = ?",
            (stable_id, canonical_hash),
        ).fetchone()
        if exact_row is not None:
            self._increment_occurrence(exact_row, stable_id, canonical_hash)
            return "same"
        identity_exists = self._identity_exists(stable_id)
        self._connection.execute(
            "INSERT INTO events(stable_id, canonical_hash, occurrence_count) VALUES (?, ?, 1)",
            (stable_id, canonical_hash),
        )
        return "conflict" if identity_exists else "unique"

    def _increment_occurrence(
        self,
        row: tuple[object, ...],
        stable_id: str,
        canonical_hash: str,
    ) -> None:
        occurrence_count = row[0]
        if not isinstance(occurrence_count, int):
            raise RuntimeError("Capability spool stored an invalid occurrence count")
        self._connection.execute(
            "UPDATE events SET occurrence_count = ? WHERE stable_id = ? AND canonical_hash = ?",
            (occurrence_count + 1, stable_id, canonical_hash),
        )

    def _identity_exists(self, stable_id: str) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM events WHERE stable_id = ? LIMIT 1", (stable_id,)
            ).fetchone()
            is not None
        )

    def manifest_digest(self) -> str:
        digest = hashlib.sha256()
        rows = self._connection.execute(
            "SELECT stable_id, canonical_hash, occurrence_count "
            "FROM events ORDER BY stable_id, canonical_hash"
        )
        for stable_id, canonical_hash, occurrence_count in rows:
            _update_digest(digest, stable_id, canonical_hash, occurrence_count)
        return "sha256:" + digest.hexdigest()

    def flush(self) -> None:
        """Durably flush the restricted evidence without closing its query handle."""
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def delete(self) -> None:
        try:
            self.close()
        finally:
            for spool_path in spool_storage_paths(self.path):
                spool_path.unlink(missing_ok=True)


def spool_storage_bytes(spool_path: Path) -> int:
    """Return SQLite database plus known journal/WAL sidecar bytes."""
    return sum(path.stat().st_size for path in spool_storage_paths(spool_path) if path.exists())


def spool_storage_paths(spool_path: Path) -> tuple[Path, ...]:
    return (
        spool_path,
        Path(f"{spool_path}-journal"),
        Path(f"{spool_path}-wal"),
        Path(f"{spool_path}-shm"),
    )


def _prepare_restricted_directory(directory: Path) -> None:
    try:
        path_stat = directory.lstat()
    except FileNotFoundError:
        try:
            directory.mkdir(parents=True, mode=0o700)
        except FileExistsError:
            path_stat = directory.lstat()
        else:
            path_stat = directory.lstat()
    if stat.S_ISLNK(path_stat.st_mode):
        raise ValueError("restricted spool directory cannot be a symlink")
    if not stat.S_ISDIR(path_stat.st_mode):
        raise ValueError("restricted spool directory must be a directory")
    if stat.S_IMODE(path_stat.st_mode) & 0o077:
        raise ValueError("existing restricted spool directory permissions are too broad")


def _update_digest(
    digest: _Digest,
    stable_id: object,
    canonical_hash: object,
    occurrence_count: object,
) -> None:
    if (
        not isinstance(stable_id, str)
        or not isinstance(canonical_hash, str)
        or not isinstance(occurrence_count, int)
    ):
        raise RuntimeError("Capability spool contained an invalid row")
    digest.update(stable_id.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(canonical_hash.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(str(occurrence_count).encode("ascii"))
    digest.update(b"\n")
