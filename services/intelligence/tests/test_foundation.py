from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from intelligence.artifacts import canonical_json, publish_file, workspace_layout, write_manifest
from intelligence.config import RuntimeConfig
from intelligence.registry import PRODUCTION_REGISTRY, RegisteredCommand, Registry
from intelligence.runtime import IntelligenceRuntime
from intelligence.state import State
from intelligence.state_schema import bootstrap, upgrade, verify_connection


def test_wal_empty_registry_and_default_off(tmp_path: Path) -> None:
    state = State(tmp_path)
    assert state.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert PRODUCTION_REGISTRY.names() == ()
    runtime = IntelligenceRuntime(RuntimeConfig(tmp_path), Registry())
    with pytest.raises(ValueError, match="unknown"):
        runtime.run("unknown")
    runtime.close()


def test_lock_fence_cancel_recovery_and_reopen(tmp_path: Path) -> None:
    first = State(tmp_path)
    run = first.create_mutating_run("test")
    second = State(tmp_path)
    with pytest.raises(RuntimeError, match="already active"):
        second.create_mutating_run("other")
    first.cancel(run.run_id)
    assert first.is_cancelled(run.run_id)
    first.mark_execution_quiescent(run)
    first.connection.execute("UPDATE mutation_lock SET heartbeat_at = 0")
    assert not second.health(1).healthy
    second.recover_stale(run.run_id, "operator confirmed crash", 1)
    assert second.health(1).healthy
    recovered = second.inspect(run.run_id)
    assert recovered is not None and recovered.state == "stale_recovered"
    evidence = second.layout.manifests / f"{run.run_id}.json"
    assert evidence.is_file()
    assert '"reason":"operator confirmed crash"' in evidence.read_text(encoding="utf-8")
    second.close()
    first.close()
    reopened = State(tmp_path)
    assert reopened.inspect(run.run_id) is not None
    reopened.close()


def test_stale_recovery_fence_mismatch_rolls_back_run_and_lock(tmp_path: Path) -> None:
    """A corrupt owner fence cannot partially transition the run or release the lock."""
    state = State(tmp_path)
    run = state.create_mutating_run("test")
    state.connection.execute(
        "UPDATE mutation_lock SET fence = fence + 1, heartbeat_at = 0 WHERE singleton = 1"
    )
    before_run = state.connection.execute(
        "SELECT state, manifest_json, fence FROM runs WHERE id = ?", (run.run_id,)
    ).fetchone()
    before_lock = state.connection.execute(
        "SELECT run_id, heartbeat_at, fence FROM mutation_lock WHERE singleton = 1"
    ).fetchone()
    try:
        with pytest.raises(RuntimeError, match="fence"):
            state.recover_stale(run.run_id, "operator recovery", 1)
        after_run = state.connection.execute(
            "SELECT state, manifest_json, fence FROM runs WHERE id = ?", (run.run_id,)
        ).fetchone()
        after_lock = state.connection.execute(
            "SELECT run_id, heartbeat_at, fence FROM mutation_lock WHERE singleton = 1"
        ).fetchone()
        assert tuple(after_run) == tuple(before_run)
        assert tuple(after_lock) == tuple(before_lock)
    finally:
        state.close()


def test_atomic_no_replace_manifest_and_backup(tmp_path: Path) -> None:
    staging = tmp_path / "staging" / "run"
    staging.mkdir(parents=True)
    source = staging / "artifact.json"
    source.write_text("{}", encoding="utf-8")
    output = publish_file(tmp_path, "run", source, 100)
    assert output.sha256
    write_manifest(tmp_path, "run", "test", "completed", output)
    with pytest.raises(RuntimeError):
        write_manifest(tmp_path, "run", "test", "completed", output)
    manifest = write_manifest(tmp_path, "other", "test", "failed", None)
    assert "secret" not in str(manifest).lower()
    state = State(tmp_path)
    backup = state.layout.backups / "foundation-bundle"
    state.backup(backup)
    state.verify_backup(backup)
    assert (backup / "state.sqlite3").is_file()
    state.close()


def test_runtime_default_off(tmp_path: Path) -> None:
    def handler(directory: Path, cancelled: object) -> None:
        (directory / "ok").write_text("ok", encoding="utf-8")

    registry = Registry((RegisteredCommand("test", True, handler, {}),))
    runtime = IntelligenceRuntime(RuntimeConfig(tmp_path), registry)
    with pytest.raises(RuntimeError, match="disabled"):
        runtime.run("test")
    runtime.close()


def test_newer_schema_is_rejected_on_reopen(tmp_path: Path) -> None:
    """A runtime must not write state created by a schema it does not understand."""
    state = State(tmp_path)
    state.connection.execute("UPDATE metadata SET value = '999' WHERE key = 'schema_version'")
    state.close()
    with pytest.raises(RuntimeError, match="newer schema"):
        State(tmp_path)


def test_nonempty_metadata_less_database_is_rejected_unchanged(tmp_path: Path) -> None:
    """A database without admission metadata is not silently adopted or repaired."""
    layout = workspace_layout(tmp_path)
    connection = sqlite3.connect(layout.state_database)
    connection.execute("CREATE TABLE unrelated (value TEXT NOT NULL)")
    connection.execute("INSERT INTO unrelated(value) VALUES('unchanged')")
    connection.commit()
    before = layout.state_database.read_bytes()
    connection.close()
    with pytest.raises(RuntimeError, match="metadata"):
        State(tmp_path)
    assert layout.state_database.read_bytes() == before


def test_malformed_current_schema_is_rejected_before_repair(tmp_path: Path) -> None:
    """A v7 database missing required runtime tables is rejected unchanged."""
    layout = workspace_layout(tmp_path)
    connection = sqlite3.connect(layout.state_database)
    connection.executescript(
        """
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO metadata(key, value) VALUES('schema_version', '7');
        CREATE TABLE runs (id TEXT PRIMARY KEY, command TEXT NOT NULL);
        """
    )
    connection.commit()
    before = layout.state_database.read_bytes()
    connection.close()
    with pytest.raises(RuntimeError, match="incomplete"):
        State(tmp_path)
    assert layout.state_database.read_bytes() == before


def test_fresh_schema_creation_rolls_back_mid_create_failure(tmp_path: Path) -> None:
    """A failed fresh DDL sequence leaves no partial metadata-less schema behind."""
    layout = workspace_layout(tmp_path)
    connection = sqlite3.connect(layout.state_database, isolation_level=None)

    def deny_final_table(
        action: int, first: str | None, second: str | None, database: str | None, source: str | None
    ) -> int:
        del second, database, source
        if action == sqlite3.SQLITE_CREATE_TABLE and first == "accepted_outputs":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(deny_final_table)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            bootstrap(connection, verify_connection, None)
    finally:
        connection.set_authorizer(None)
    assert (
        connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        == []
    )
    connection.close()


def test_legacy_v1_manifest_reopens_and_verifies_in_backup(tmp_path: Path) -> None:
    """Prior schema-v1 evidence with empty limits remains readable and exportable."""
    state = State(tmp_path)
    run = state.create_mutating_run("legacy")
    legacy = {
        "schema_version": 1,
        "run_id": run.run_id,
        "command": "legacy",
        "created_at": run.created_at,
        "started_at": run.started_at,
        "ended_at": run.created_at + 1,
        "state": "completed",
        "limits": {},
        "outputs": [],
        "run_log": None,
    }
    (state.layout.outputs / run.run_id).mkdir(parents=True)
    state.mark_execution_quiescent(run)
    state.terminal(run, "completed", legacy)
    manifest_path = state.layout.manifests / f"{run.run_id}.json"
    manifest_path.write_text(canonical_json(legacy), encoding="utf-8")
    state.close()
    reopened = State(tmp_path)
    try:
        backup = reopened.layout.backups / "legacy-v1.bundle"
        reopened.backup(backup)
        reopened.verify_backup(backup)
        assert json.loads(manifest_path.read_text(encoding="utf-8"))["schema_version"] == 1
    finally:
        reopened.close()


def test_schema_v4_state_migrates_admission_limits_column(tmp_path: Path) -> None:
    """The limits metadata migration is safe for an existing v4 workspace."""
    layout = workspace_layout(tmp_path)
    connection = sqlite3.connect(layout.state_database)
    connection.executescript(
        """
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO metadata(key, value) VALUES('schema_version', '4');
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, command TEXT NOT NULL, state TEXT NOT NULL,
            fence INTEGER NOT NULL, created_at REAL NOT NULL, heartbeat_at REAL,
            cancellation_requested INTEGER NOT NULL DEFAULT 0, recovery_reason TEXT,
            manifest_json TEXT, publishing_inventory_json TEXT, started_at REAL, ended_at REAL
        );
        CREATE TABLE mutation_lock (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1), run_id TEXT,
            fence INTEGER NOT NULL DEFAULT 0, heartbeat_at REAL
        );
        CREATE TABLE accepted_outputs (
            relative_path TEXT PRIMARY KEY, run_id TEXT NOT NULL,
            sha256 TEXT NOT NULL, byte_count INTEGER NOT NULL
        );
        INSERT INTO mutation_lock(singleton) VALUES(1);
        """
    )
    connection.close()
    reopened = State(tmp_path)
    try:
        version = reopened.connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()[0]
        columns = {str(row[1]) for row in reopened.connection.execute("PRAGMA table_info(runs)")}
        assert version == "7"
        assert "limits_json" in columns
    finally:
        reopened.close()


def test_pre_v6_active_migration_waits_for_trusted_epoch(tmp_path: Path) -> None:
    """Legacy active rows remain unchanged when no trusted epoch is available."""
    layout = workspace_layout(tmp_path)
    connection = sqlite3.connect(layout.state_database, isolation_level=None)
    connection.executescript(
        """
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO metadata(key, value) VALUES('schema_version', '4');
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, command TEXT NOT NULL, state TEXT NOT NULL,
            fence INTEGER NOT NULL, created_at REAL NOT NULL, heartbeat_at REAL,
            cancellation_requested INTEGER NOT NULL DEFAULT 0, recovery_reason TEXT,
            manifest_json TEXT, publishing_inventory_json TEXT, started_at REAL,
            ended_at REAL
        );
        INSERT INTO runs(id, command, state, fence, created_at, heartbeat_at)
        VALUES('legacy-active', 'reviewed', 'running', 1, 1, 1);
        """
    )
    try:
        with pytest.raises(RuntimeError, match="trusted runtime epoch"):
            upgrade(connection, 4, None)
        assert (
            connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()[0]
            == "4"
        )
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(runs)")}
        assert "runtime_epoch" not in columns
        assert "execution_may_be_alive" not in columns
    finally:
        connection.close()


def test_schema_v6_active_run_migration_keeps_execution_fence(tmp_path: Path) -> None:
    """A pre-v7 active run remains unsafe until a trusted epoch changes."""
    layout = workspace_layout(tmp_path)
    epoch = "legacy-container"
    connection = sqlite3.connect(layout.state_database)
    connection.executescript(
        """
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO metadata(key, value) VALUES('schema_version', '6');
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, command TEXT NOT NULL, state TEXT NOT NULL,
            fence INTEGER NOT NULL, created_at REAL NOT NULL, heartbeat_at REAL,
            cancellation_requested INTEGER NOT NULL DEFAULT 0, recovery_reason TEXT,
            manifest_json TEXT, publishing_inventory_json TEXT, started_at REAL,
            ended_at REAL, limits_json TEXT, runtime_epoch TEXT,
            cleanup_unresolved INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE mutation_lock (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1), run_id TEXT,
            fence INTEGER NOT NULL DEFAULT 0, heartbeat_at REAL
        );
        CREATE TABLE accepted_outputs (
            relative_path TEXT PRIMARY KEY, run_id TEXT NOT NULL,
            sha256 TEXT NOT NULL, byte_count INTEGER NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO mutation_lock(singleton, run_id, fence, heartbeat_at) "
        "VALUES(1, 'legacy-active', 1, 0)"
    )
    connection.execute(
        "INSERT INTO runs(id, command, state, fence, created_at, heartbeat_at, "
        "cancellation_requested, limits_json, runtime_epoch, cleanup_unresolved) "
        "VALUES('legacy-active', 'reviewed', 'running', 1, 1, 0, 0, ?, ?, 0)",
        (
            json.dumps(
                {
                    "max_log_bytes": 1000,
                    "max_output_bytes": 1000,
                    "max_output_entries": 10,
                    "max_runtime_seconds": 10,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            epoch,
        ),
    )
    connection.commit()
    connection.close()

    migrated = State(tmp_path, runtime_epoch=epoch)
    try:
        run = migrated.inspect("legacy-active")
        assert run is not None and run.execution_may_be_alive
        with pytest.raises(RuntimeError, match="execution-domain"):
            migrated.recover_stale("legacy-active", "same epoch", 1)
    finally:
        migrated.close()

    recreated = State(tmp_path, runtime_epoch="recreated-container")
    try:
        recreated.recover_stale("legacy-active", "container recreated", 1)
        recovered = recreated.inspect("legacy-active")
        assert recovered is not None and recovered.state == "stale_recovered"
    finally:
        recreated.close()


def test_terminalization_requires_durable_quiescence_proof(tmp_path: Path) -> None:
    """State refuses both ordinary terminal paths while execution may be alive."""
    state = State(tmp_path)
    run = state.create_mutating_run("test")
    try:
        with pytest.raises(RuntimeError, match="quiescence"):
            state.terminal(run, "failed", {})
        state.begin_publishing(run, ())
        with pytest.raises(RuntimeError, match="quiescence"):
            state.complete_publication(run, (), {})
        current = state.inspect(run.run_id)
        assert current is not None and current.state == "publishing"
        assert state.active_run() is not None
    finally:
        state.close()


def test_schema_upgrade_rolls_back_partial_safety_backfill(tmp_path: Path) -> None:
    """A failed migration cannot leave new columns without their safety backfills."""
    layout = workspace_layout(tmp_path)
    connection = sqlite3.connect(layout.state_database, isolation_level=None)
    connection.executescript(
        """
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO metadata(key, value) VALUES('schema_version', '6');
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, command TEXT NOT NULL, state TEXT NOT NULL,
            fence INTEGER NOT NULL, created_at REAL NOT NULL, heartbeat_at REAL,
            cancellation_requested INTEGER NOT NULL DEFAULT 0, recovery_reason TEXT,
            manifest_json TEXT, publishing_inventory_json TEXT, started_at REAL,
            ended_at REAL, limits_json TEXT, runtime_epoch TEXT,
            cleanup_unresolved INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO runs(id, command, state, fence, created_at, heartbeat_at)
        VALUES('legacy-active', 'reviewed', 'running', 1, 1, 0);
        CREATE TRIGGER reject_migration_backfill BEFORE UPDATE ON runs
        BEGIN SELECT RAISE(ABORT, 'injected migration failure'); END;
        """
    )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="injected migration failure"):
            upgrade(connection, 6, "legacy-container")
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(runs)")}
        assert "execution_may_be_alive" not in columns
        assert (
            connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()[0]
            == "6"
        )
    finally:
        connection.close()
