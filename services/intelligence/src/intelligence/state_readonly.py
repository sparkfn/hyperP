"""Genuinely read-only SQLite State access for diagnostic command paths."""

from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

from intelligence import state_queries
from intelligence.models import OutputInventory, Run


class ReadOnlyState:
    """A State query subset which never bootstraps, migrates, or creates paths."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @classmethod
    def open(cls, workspace: Path) -> ReadOnlyState:
        database = _database_path(workspace)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(_readonly_uri(database), uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            _validate_schema(connection)
        except BaseException:
            if connection is not None:
                connection.close()
            raise
        if connection is None:
            raise AssertionError("readonly State connection was not opened")
        return cls(connection)

    def close(self) -> None:
        self._connection.close()

    def inspect(self, run_id: str) -> Run | None:
        return state_queries.inspect(self._connection, run_id)

    def accepted_outputs(self, run_id: str) -> tuple[OutputInventory, ...]:
        return state_queries.accepted_outputs(self._connection, run_id)


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


def _readonly_uri(database: Path) -> str:
    wal = database.with_name(f"{database.name}-wal")
    shared_memory = database.with_name(f"{database.name}-shm")
    sidecars = (wal, shared_memory)
    exists = tuple(_sidecar_exists(path) for path in sidecars)
    if exists == (False, False):
        # A closed database has no WAL state to replay. Immutable mode avoids
        # SQLite creating WAL/SHM sidecars for a diagnostic read.
        return f"{database.absolute().as_uri()}?mode=ro&immutable=1"
    if exists != (True, True):
        raise ValueError("Intelligence State WAL sidecars are incomplete")
    for path in sidecars:
        _safe_sidecar(path)
    # Existing safe WAL/SHM sidecars preserve visibility of an active State;
    # mode=ro does not create new auxiliary paths.
    return f"{database.absolute().as_uri()}?mode=ro"


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
