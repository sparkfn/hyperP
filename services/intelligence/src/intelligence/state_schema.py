"""SQLite schema creation, migration, and legacy-layout handling."""

from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path

SCHEMA_VERSION = 7
ConnectionVerifier = Callable[[sqlite3.Connection], None]


def current_runtime_epoch() -> str | None:
    """Return a trusted PID-namespace identity, or None when it cannot be proven."""
    try:
        raw = Path("/proc/1/stat").read_text(encoding="utf-8")
        fields = raw.rsplit(")", 1)[1].split()
        namespace = os.readlink("/proc/self/ns/pid")
        if not namespace or len(fields) <= 19:
            return None
        return f"{namespace}:pid1-start:{fields[19]}"
    except (OSError, IndexError, UnicodeDecodeError, ValueError):
        return None


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


def bootstrap(
    connection: sqlite3.Connection,
    verify_connection: ConnectionVerifier,
    runtime_epoch: str | None,
) -> None:
    """Create or migrate the durable schema and establish the singleton lock row."""
    objects = _user_schema_objects(connection)
    metadata_exists = "metadata" in objects
    if not metadata_exists:
        if objects:
            raise RuntimeError("Intelligence database has no schema metadata")
        _create_current_schema(connection)
        version = SCHEMA_VERSION
    else:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            raise RuntimeError("Intelligence schema version is missing")
        try:
            version = int(row[0])
        except (TypeError, ValueError) as error:
            raise RuntimeError("Intelligence schema version is invalid") from error
    if version > SCHEMA_VERSION:
        raise RuntimeError("Intelligence state was created by a newer schema")
    if version < SCHEMA_VERSION:
        upgrade(connection, version, runtime_epoch)
        _validate_current_schema(connection, require_constraints=False)
    else:
        migrated_legacy = connection.execute(
            "SELECT 1 FROM metadata WHERE key = 'legacy_schema_migration'"
        ).fetchone()
        _validate_current_schema(connection, require_constraints=migrated_legacy is None)
    connection.execute("INSERT OR IGNORE INTO mutation_lock(singleton) VALUES(1)")
    verify_connection(connection)


def _user_schema_objects(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
    return {str(row[0]) for row in rows}


def _create_current_schema(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)
        """,
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, command TEXT NOT NULL, state TEXT NOT NULL,
            fence INTEGER NOT NULL, created_at REAL NOT NULL, heartbeat_at REAL,
            cancellation_requested INTEGER NOT NULL DEFAULT 0, recovery_reason TEXT,
            manifest_json TEXT, publishing_inventory_json TEXT, started_at REAL, ended_at REAL,
            limits_json TEXT, runtime_epoch TEXT,
            cleanup_unresolved INTEGER NOT NULL DEFAULT 0,
            execution_may_be_alive INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE mutation_lock (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1), run_id TEXT,
            fence INTEGER NOT NULL DEFAULT 0, heartbeat_at REAL
        )
        """,
        """
        CREATE TABLE accepted_outputs (
            relative_path TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
            sha256 TEXT NOT NULL, byte_count INTEGER NOT NULL CHECK(byte_count >= 0)
        )
        """,
    )
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _validate_current_schema(connection: sqlite3.Connection, *, require_constraints: bool) -> None:
    required: dict[str, frozenset[str]] = {
        "metadata": frozenset({"key", "value"}),
        "runs": frozenset(
            {
                "id",
                "command",
                "state",
                "fence",
                "created_at",
                "heartbeat_at",
                "cancellation_requested",
                "recovery_reason",
                "manifest_json",
                "publishing_inventory_json",
                "started_at",
                "ended_at",
                "limits_json",
                "runtime_epoch",
                "cleanup_unresolved",
                "execution_may_be_alive",
            }
        ),
        "mutation_lock": frozenset({"singleton", "run_id", "fence", "heartbeat_at"}),
        "accepted_outputs": frozenset({"relative_path", "run_id", "sha256", "byte_count"}),
    }
    objects = _user_schema_objects(connection)
    if not set(required).issubset(objects):
        raise RuntimeError("Intelligence current schema is incomplete")
    for table, expected in required.items():
        actual = frozenset(str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})"))
        if not expected.issubset(actual):
            raise RuntimeError("Intelligence current schema is incomplete")
    if not require_constraints:
        return
    primary_keys = {
        "runs": "id",
        "mutation_lock": "singleton",
        "accepted_outputs": "relative_path",
    }
    for table, column in primary_keys.items():
        rows = tuple(connection.execute(f"PRAGMA table_info({table})"))
        primary = next((row for row in rows if str(row[1]) == column), None)
        if primary is None or int(primary[5]) != 1:
            raise RuntimeError("Intelligence current schema constraints are invalid")
    constraints = {
        "mutation_lock": ("CHECK(SINGLETON = 1)",),
        "accepted_outputs": ("REFERENCES RUNS", "CHECK(BYTE_COUNT >= 0)"),
    }
    for table, fragments in constraints.items():
        sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        sql = "" if sql_row is None or sql_row[0] is None else str(sql_row[0]).upper()
        if any(fragment.upper() not in sql for fragment in fragments):
            raise RuntimeError("Intelligence current schema constraints are invalid")


def upgrade(connection: sqlite3.Connection, version: int, runtime_epoch: str | None) -> None:
    """Apply the bounded in-place schema upgrade path transactionally."""
    if version < 1 or version > 6:
        raise RuntimeError("Intelligence state schema is unsupported")
    connection.execute("BEGIN IMMEDIATE")
    try:
        if version < 6 and runtime_epoch is None:
            active = connection.execute(
                "SELECT 1 FROM runs WHERE state IN ('queued', 'running', 'publishing') LIMIT 1"
            ).fetchone()
            if active is not None:
                raise RuntimeError("active legacy execution requires a trusted runtime epoch")
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(runs)")}
        added_runtime_epoch = "runtime_epoch" not in columns
        added_execution_fence = "execution_may_be_alive" not in columns
        for name in (
            "cancellation_requested",
            "publishing_inventory_json",
            "started_at",
            "ended_at",
            "limits_json",
            "runtime_epoch",
            "cleanup_unresolved",
            "execution_may_be_alive",
        ):
            if name not in columns:
                default = (
                    " INTEGER NOT NULL DEFAULT 0" if name == "cancellation_requested" else " REAL"
                )
                if name == "publishing_inventory_json":
                    default = " TEXT"
                if name == "limits_json":
                    default = " TEXT"
                if name == "runtime_epoch":
                    default = " TEXT"
                if name == "cleanup_unresolved":
                    default = " INTEGER NOT NULL DEFAULT 0"
                if name == "execution_may_be_alive":
                    default = " INTEGER NOT NULL DEFAULT 0"
                connection.execute(f"ALTER TABLE runs ADD COLUMN {name}{default}")
        if added_runtime_epoch and runtime_epoch is not None:
            # Pre-v6 rows had no epoch. This is only a baseline, not a quiescence
            # proof; a later different trusted epoch is still required for recovery.
            connection.execute(
                "UPDATE runs SET runtime_epoch = ? "
                "WHERE runtime_epoch IS NULL AND state IN ('queued', 'running', 'publishing')",
                (runtime_epoch,),
            )
        if added_execution_fence:
            # Historical supervisors had no durable quiescence proof. Active rows
            # remain fenced until a trusted epoch change is proven.
            connection.execute(
                "UPDATE runs SET execution_may_be_alive = 1 "
                "WHERE state IN ('queued', 'running', 'publishing')"
            )
        connection.execute(
            "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION),),
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('legacy_schema_migration', '1')"
        )
        _validate_current_schema(connection, require_constraints=False)
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
