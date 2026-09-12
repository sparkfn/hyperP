"""Private bounded scratch primitives for read-only repair status snapshots."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path


class CanonicalByteSorter:
    """Sort canonical byte rows on private disk without retaining their payloads."""

    def __init__(self, *, unique_keys: bool = False, cache_kib: int = 8 * 1024) -> None:
        if type(cache_kib) is not int or cache_kib < 1:
            raise ValueError("canonical sorter cache must be a positive integer")
        self._unique_keys = unique_keys
        self._cache_kib = cache_kib
        self._directory: tempfile.TemporaryDirectory[str] | None = None
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> CanonicalByteSorter:
        directory = tempfile.TemporaryDirectory(prefix="hyperp-crm-repair-status-")
        self._directory = directory
        try:
            _make_private(Path(directory.name))
            database = Path(directory.name) / "canonical-sort.sqlite3"
            connection = sqlite3.connect(database)
            self._connection = connection
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA temp_store=FILE")
            connection.execute("PRAGMA mmap_size=0")
            connection.execute(f"PRAGMA cache_size=-{self._cache_kib}")
            unique = ", UNIQUE(sort_key)" if self._unique_keys else ""
            connection.execute(
                "CREATE TABLE rows ("
                "sequence INTEGER PRIMARY KEY, sort_key BLOB NOT NULL, payload BLOB NOT NULL"
                f"{unique})"
            )
            connection.execute("CREATE INDEX rows_sort_order ON rows(sort_key, sequence)")
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def add(self, sort_key: bytes, payload: bytes) -> None:
        """Add one already-canonical newline-terminated payload."""
        if not sort_key:
            raise ValueError("canonical sorter key must be non-empty")
        if not payload or not payload.endswith(b"\n"):
            raise ValueError("canonical sorter payload must be newline-terminated")
        connection = self._require_connection()
        try:
            connection.execute(
                "INSERT INTO rows (sort_key, payload) VALUES (?, ?)",
                (sort_key, payload),
            )
        except sqlite3.IntegrityError as exc:
            if self._unique_keys:
                raise ValueError(
                    "repair inventory rows must have unique source-version identities"
                ) from exc
            raise RuntimeError("canonical sorter failed to retain duplicate evidence") from exc

    def values(self) -> Iterator[bytes]:
        """Yield payloads in bytewise canonical order, preserving duplicates."""
        connection = self._require_connection()
        cursor = connection.execute("SELECT payload FROM rows ORDER BY sort_key, sequence")
        for row in cursor:
            payload = row[0]
            if not isinstance(payload, bytes):
                raise RuntimeError("canonical sorter returned a non-byte payload")
            yield payload

    def close(self) -> None:
        """Close SQLite before removing the private scratch directory."""
        connection = self._connection
        self._connection = None
        directory = self._directory
        self._directory = None
        try:
            if connection is not None:
                connection.close()
        finally:
            if directory is not None:
                directory.cleanup()

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("canonical sorter is not open")
        return self._connection


def _make_private(path: Path) -> None:
    """Tighten POSIX permissions when available; Windows temp ACLs remain owner-scoped."""
    try:
        os.chmod(path, 0o700)
    except OSError:
        # TemporaryDirectory is already user-private on supported platforms. Do
        # not turn an advisory permission difference into retained sensitive data.
        pass
