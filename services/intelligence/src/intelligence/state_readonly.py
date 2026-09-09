"""Genuinely read-only SQLite State access for diagnostic command paths."""

from __future__ import annotations

import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path

from intelligence import state_queries
from intelligence.models import OutputInventory, Run


@dataclass(frozen=True)
class _FileGuard:
    device: int
    inode: int
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class _ImmutableGuard:
    database: Path
    state_directory: Path
    database_guard: _FileGuard
    directory_guard: _FileGuard
    sidecars: tuple[Path, Path]


class ReadOnlyState:
    """A State query subset which never bootstraps, migrates, or creates paths."""

    def __init__(self, connection: sqlite3.Connection, guard: _ImmutableGuard | None) -> None:
        self._connection = connection
        self._guard = guard

    @classmethod
    def open(cls, workspace: Path) -> ReadOnlyState:
        database = _database_path(workspace)
        uri, guard = _readonly_plan(database)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(uri, uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            _validate_schema(connection)
        except BaseException:
            if connection is not None:
                connection.close()
            raise
        if connection is None:
            raise AssertionError("readonly State connection was not opened")
        return cls(connection, guard)

    def close(self) -> None:
        changed = self._guard is not None and not _guard_is_current(self._guard)
        self._connection.close()
        if changed:
            raise RuntimeError("immutable Intelligence State changed during status read")

    def inspect(self, run_id: str) -> Run | None:
        return state_queries.inspect(self._connection, run_id)

    def accepted_outputs(self, run_id: str) -> tuple[OutputInventory, ...]:
        return state_queries.accepted_outputs(self._connection, run_id)

    def completed_run_ids(self, command: str, limit: int) -> tuple[str, ...]:
        """Read bounded completed run identities without creating State paths or rows."""
        return state_queries.completed_run_ids(self._connection, command, limit)


def _database_path(workspace: Path) -> Path:
    _directory(workspace, "Intelligence workspace")
    state = workspace / "state"
    try:
        _directory(state, "Intelligence State directory")
    except FileNotFoundError:
        raise FileNotFoundError("Intelligence State is absent") from None
    database = state / "state.sqlite3"
    try:
        metadata = database.lstat()
    except FileNotFoundError:
        raise FileNotFoundError("Intelligence State is absent") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Intelligence State database is unsafe")
    return database


def _readonly_plan(database: Path) -> tuple[str, _ImmutableGuard | None]:
    wal = database.with_name(f"{database.name}-wal")
    shared_memory = database.with_name(f"{database.name}-shm")
    sidecars = (wal, shared_memory)
    exists = tuple(_sidecar_exists(path) for path in sidecars)
    if exists == (False, False):
        guard = _capture_immutable_guard(database, sidecars)
        return f"{database.absolute().as_uri()}?mode=ro&immutable=1", guard
    if exists != (True, True):
        raise ValueError("Intelligence State WAL sidecars are incomplete")
    for path in sidecars:
        _safe_sidecar(path)
    # Existing safe WAL/SHM sidecars preserve visibility of an active State;
    # mode=ro does not create new auxiliary paths.
    return f"{database.absolute().as_uri()}?mode=ro", None


def _capture_immutable_guard(database: Path, sidecars: tuple[Path, Path]) -> _ImmutableGuard:
    if any(_sidecar_exists(path) for path in sidecars):
        raise RuntimeError("Intelligence State WAL sidecars changed during immutable admission")
    return _ImmutableGuard(
        database,
        database.parent,
        _file_guard(database, "Intelligence State database"),
        _file_guard(database.parent, "Intelligence State directory"),
        sidecars,
    )


def _guard_is_current(guard: _ImmutableGuard) -> bool:
    try:
        return (
            _file_guard(guard.database, "Intelligence State database") == guard.database_guard
            and _file_guard(guard.state_directory, "Intelligence State directory")
            == guard.directory_guard
            and not any(_sidecar_exists(path) for path in guard.sidecars)
        )
    except (FileNotFoundError, ValueError, OSError):
        return False


def _file_guard(path: Path, label: str) -> _FileGuard:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"{label} is unsafe")
    return _FileGuard(metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)


def _sidecar_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _safe_sidecar(path: Path) -> None:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ValueError("Intelligence State WAL sidecar is unsafe")


def _directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise FileNotFoundError(f"{label} is absent") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} is unsafe")


def _validate_schema(connection: sqlite3.Connection) -> None:
    required = {
        "runs": {
            "id",
            "command",
            "state",
            "fence",
            "created_at",
            "heartbeat_at",
            "cancellation_requested",
            "recovery_reason",
            "started_at",
            "ended_at",
            "limits_json",
            "runtime_epoch",
            "cleanup_unresolved",
            "execution_may_be_alive",
        },
        "accepted_outputs": {"relative_path", "run_id", "sha256", "byte_count"},
    }
    for table, columns in required.items():
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        present = {str(row[1]) for row in rows}
        if not columns.issubset(present):
            raise ValueError("Intelligence State schema is incompatible")
