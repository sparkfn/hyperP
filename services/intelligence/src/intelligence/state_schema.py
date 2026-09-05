"""SQLite schema creation, migration, and legacy-layout handling."""

from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path

SCHEMA_VERSION = 4
ConnectionVerifier = Callable[[sqlite3.Connection], None]


def verify_connection(connection: sqlite3.Connection) -> None:
    """Reject a corrupt SQLite database before it becomes active state."""
    row = connection.execute("PRAGMA integrity_check").fetchone()
    if row is None or str(row[0]).lower() != "ok":
        raise RuntimeError("SQLite integrity check failed")


def migrate_legacy_database(
    workspace: Path,
    state_directory: Path,
    state_database: Path,
    verify_connection: ConnectionVerifier,
) -> None:
    """Copy the legacy root database into the versioned state directory once."""
    legacy = workspace / "state.sqlite3"
    if state_database.exists():
        if not path_exists_safe(state_database):
            raise ValueError("versioned Intelligence state is unsafe")
        return
    if not legacy.exists():
        return
    if legacy.is_symlink() or not legacy.is_file():
        raise ValueError("legacy Intelligence state is unsafe")
    temporary = state_directory / f".state.sqlite3.migrate-{uuid.uuid4().hex}"
    source = sqlite3.connect(f"file:{legacy}?mode=ro", uri=True)
    target = sqlite3.connect(temporary)
    try:
        source.backup(target)
        verify_connection(target)
    finally:
        target.close()
        source.close()
    os.replace(temporary, state_database)


def path_exists_safe(path: Path) -> bool:
    """Return true only for a regular non-symlink file."""
    return path.exists() and not path.is_symlink() and path.is_file()


def bootstrap(connection: sqlite3.Connection, verify_connection: ConnectionVerifier) -> None:
    """Create or migrate the durable schema and establish the singleton lock row."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, command TEXT NOT NULL, state TEXT NOT NULL,
            fence INTEGER NOT NULL, created_at REAL NOT NULL, heartbeat_at REAL,
            cancellation_requested INTEGER NOT NULL DEFAULT 0, recovery_reason TEXT,
            manifest_json TEXT, publishing_inventory_json TEXT, started_at REAL, ended_at REAL
        );
        CREATE TABLE IF NOT EXISTS mutation_lock (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1), run_id TEXT,
            fence INTEGER NOT NULL DEFAULT 0, heartbeat_at REAL
        );
        CREATE TABLE IF NOT EXISTS accepted_outputs (
            relative_path TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
            sha256 TEXT NOT NULL, byte_count INTEGER NOT NULL CHECK(byte_count >= 0)
        );
        """
    )
    connection.execute(
        "INSERT OR IGNORE INTO metadata(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    row = connection.execute("SELECT value FROM metadata WHERE key = 'schema_version'").fetchone()
    if row is None:
        raise RuntimeError("Intelligence schema version is missing")
    version = int(row[0])
    if version > SCHEMA_VERSION:
        raise RuntimeError("Intelligence state was created by a newer schema")
    if version < SCHEMA_VERSION:
        upgrade(connection, version)
    connection.execute("INSERT OR IGNORE INTO mutation_lock(singleton) VALUES(1)")
    verify_connection(connection)


def upgrade(connection: sqlite3.Connection, version: int) -> None:
    """Apply the bounded in-place schema upgrade path."""
    if version < 1 or version > 3:
        raise RuntimeError("Intelligence state schema is unsupported")
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(runs)")}
    for name in ("cancellation_requested", "publishing_inventory_json", "started_at", "ended_at"):
        if name not in columns:
            default = " INTEGER NOT NULL DEFAULT 0" if name == "cancellation_requested" else " REAL"
            if name == "publishing_inventory_json":
                default = " TEXT"
            connection.execute(f"ALTER TABLE runs ADD COLUMN {name}{default}")
    connection.execute(
        "UPDATE metadata SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),)
    )
