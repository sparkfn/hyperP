"""SQLite WAL state, fencing, publication recovery, and verified backup bundles."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from intelligence.artifacts import (
    canonical_json,
    run_log_inventory,
    sha256_file,
    workspace_layout,
    write_manifest,
)
from intelligence.models import (
    Health,
    OutputInventory,
    ReconciledPublication,
    Run,
    RunState,
    TerminalRunState,
)

SCHEMA_VERSION = 4
_TERMINAL: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled", "timed_out", "stale_recovered"}
)


class State:
    """Foundation-owned durable state. Callers must close the instance."""

    def __init__(self, workspace: Path) -> None:
        self.layout = workspace_layout(workspace)
        self.workspace = self.layout.root
        self._migrate_legacy_database()
        self.path = self.layout.state_database
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self._bootstrap()
        self._verify_connection(self.connection)

    def _migrate_legacy_database(self) -> None:
        """Safely copy the version-one root database before reopening it at the versioned path."""
        legacy = self.workspace / "state.sqlite3"
        if self.layout.state_database.exists():
            if not self.path_exists_safe(self.layout.state_database):
                raise ValueError("versioned Intelligence state is unsafe")
            return
        if not legacy.exists():
            return
        if legacy.is_symlink() or not legacy.is_file():
            raise ValueError("legacy Intelligence state is unsafe")
        temporary = self.layout.state_directory / f".state.sqlite3.migrate-{uuid.uuid4().hex}"
        source = sqlite3.connect(f"file:{legacy}?mode=ro", uri=True)
        target = sqlite3.connect(temporary)
        try:
            source.backup(target)
            self._verify_connection(target)
        finally:
            target.close()
            source.close()
        os.replace(temporary, self.layout.state_database)

    @staticmethod
    def path_exists_safe(path: Path) -> bool:
        return path.exists() and not path.is_symlink() and path.is_file()

    def _bootstrap(self) -> None:
        self.connection.executescript(
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
        self.connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            raise RuntimeError("Intelligence schema version is missing")
        version = int(row[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError("Intelligence state was created by a newer schema")
        if version < SCHEMA_VERSION:
            self._upgrade(version)
        self.connection.execute("INSERT OR IGNORE INTO mutation_lock(singleton) VALUES(1)")

    def _upgrade(self, version: int) -> None:
        if version < 1 or version > 3:
            raise RuntimeError("Intelligence state schema is unsupported")
        columns = {str(row[1]) for row in self.connection.execute("PRAGMA table_info(runs)")}
        for name in (
            "cancellation_requested",
            "publishing_inventory_json",
            "started_at",
            "ended_at",
        ):
            if name not in columns:
                default = (
                    " INTEGER NOT NULL DEFAULT 0" if name == "cancellation_requested" else " REAL"
                )
                if name == "publishing_inventory_json":
                    default = " TEXT"
                self.connection.execute(f"ALTER TABLE runs ADD COLUMN {name}{default}")
        self.connection.execute(
            "UPDATE metadata SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),)
        )

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self.connection.close()

    def create_mutating_run(self, command: str) -> Run:
        """Claim the exclusive mutation lock or reject concurrent work."""
        run_id, now = uuid.uuid4().hex, time.time()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            changed = self.connection.execute(
                "UPDATE mutation_lock SET fence = fence + 1, run_id = ?, heartbeat_at = ? "
                "WHERE singleton = 1 AND run_id IS NULL",
                (run_id, now),
            ).rowcount
            if changed != 1:
                raise RuntimeError("a mutating Intelligence run is already active")
            row = self.connection.execute(
                "SELECT fence FROM mutation_lock WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise RuntimeError("mutation lock is missing")
            fence = int(row[0])
            self.connection.execute(
                "INSERT INTO runs(id, command, state, fence, created_at, heartbeat_at, started_at) "
                "VALUES(?, ?, 'running', ?, ?, ?, ?)",
                (run_id, command, fence, now, now, now),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return Run(run_id, command, "running", fence, now, now, started_at=now)

    def verify_fence(self, run: Run) -> None:
        """Reject mutation or publication by an owner whose durable fence was lost."""
        row = self.connection.execute(
            "SELECT run_id FROM mutation_lock WHERE singleton = 1 AND fence = ?", (run.fence,)
        ).fetchone()
        if row is None or row["run_id"] != run.run_id:
            raise RuntimeError("Intelligence mutation fence was lost")

    def heartbeat(self, run: Run) -> None:
        """Refresh a lock only if this exact fence remains authoritative."""
        now = time.time()
        changed = self.connection.execute(
            "UPDATE mutation_lock SET heartbeat_at = ? "
            "WHERE singleton = 1 AND run_id = ? AND fence = ?",
            (now, run.run_id, run.fence),
        ).rowcount
        if changed != 1:
            raise RuntimeError("Intelligence mutation fence was lost")
        self.connection.execute("UPDATE runs SET heartbeat_at = ? WHERE id = ?", (now, run.run_id))

    def is_cancelled(self, run_id: str) -> bool:
        row = self.connection.execute(
            "SELECT cancellation_requested FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        return row is not None and bool(row[0])

    def cancel(self, run_id: str) -> None:
        """Durably request cancellation; the owner records the terminal outcome."""
        self.connection.execute(
            "UPDATE runs SET cancellation_requested = 1 WHERE id = ? "
            "AND state IN ('queued', 'running', 'publishing')",
            (run_id,),
        )

    def begin_publishing(self, run: Run, outputs: Sequence[OutputInventory]) -> None:
        """Persist an exact intended inventory before filesystem publication."""
        self.verify_fence(run)
        encoded = _encode_inventory(outputs)
        changed = self.connection.execute(
            "UPDATE runs SET state = 'publishing', publishing_inventory_json = ? "
            "WHERE id = ? AND fence = ? AND state = 'running' AND cancellation_requested = 0",
            (encoded, run.run_id, run.fence),
        ).rowcount
        if changed != 1:
            raise RuntimeError("run cannot enter publishing")

    def register_output(self, run: Run, path: str, sha256: str, byte_count: int) -> None:
        """Compatibility registration seam; full publication should use complete_publication."""
        self.verify_fence(run)
        _validate_output(OutputInventory(path, sha256, byte_count))
        self.connection.execute(
            "INSERT INTO accepted_outputs(relative_path, run_id, sha256, byte_count) "
            "VALUES(?, ?, ?, ?)",
            (path, run.run_id, sha256, byte_count),
        )

    def complete_publication(
        self, run: Run, outputs: Sequence[OutputInventory], manifest: dict[str, object]
    ) -> None:
        """Atomically register a verified full inventory, terminal evidence, and lock release."""
        expected = _decode_inventory_for_run(self.connection, run.run_id)
        actual = tuple(sorted(outputs, key=lambda item: item.relative_path))
        if expected != _staged_from_published(run.run_id, actual):
            raise RuntimeError("published inventory does not match durable publication intent")
        self._terminal_transaction(run, "completed", manifest, actual)

    def terminal(self, run: Run, state: TerminalRunState, manifest: dict[str, object]) -> None:
        """Persist idempotent immutable terminal evidence and release only the matching fence."""
        self._terminal_transaction(run, state, manifest, ())

    def _terminal_transaction(
        self,
        run: Run,
        state: TerminalRunState,
        manifest: dict[str, object],
        outputs: Sequence[OutputInventory],
    ) -> None:
        encoded = canonical_json(manifest)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self.connection.execute(
                "SELECT state, manifest_json FROM runs WHERE id = ?", (run.run_id,)
            ).fetchone()
            if current is None:
                raise RuntimeError("run is missing")
            if str(current["state"]) in _TERMINAL:
                if str(current["state"]) == state and current["manifest_json"] == encoded:
                    self.connection.execute("COMMIT")
                    return
                raise RuntimeError("terminal run already has different immutable evidence")
            self.verify_fence(run)
            if state == "completed":
                for item in outputs:
                    _validate_output(item)
                    self.connection.execute(
                        "INSERT INTO accepted_outputs(relative_path, run_id, sha256, byte_count) "
                        "VALUES(?, ?, ?, ?)",
                        (item.relative_path, run.run_id, item.sha256, item.byte_count),
                    )
            else:
                self.connection.execute(
                    "DELETE FROM accepted_outputs WHERE run_id = ?", (run.run_id,)
                )
            changed = self.connection.execute(
                "UPDATE runs SET state = ?, manifest_json = ?, ended_at = ? "
                "WHERE id = ? AND fence = ? AND state NOT IN "
                "('completed', 'failed', 'cancelled', 'timed_out', 'stale_recovered')",
                (state, encoded, time.time(), run.run_id, run.fence),
            ).rowcount
            if changed != 1:
                raise RuntimeError("run fence was lost before terminalization")
            self.connection.execute(
                "UPDATE mutation_lock SET run_id = NULL, heartbeat_at = NULL "
                "WHERE singleton = 1 AND run_id = ? AND fence = ?",
                (run.run_id, run.fence),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def inspect(self, run_id: str) -> Run | None:
        row = self.connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return None if row is None else _row_to_run(row)

    def active_run(self) -> Run | None:
        """Return the current durable lock owner only when its run record agrees."""
        row = self.connection.execute(
            "SELECT r.* FROM mutation_lock AS lock "
            "JOIN runs AS r ON r.id = lock.run_id AND r.fence = lock.fence "
            "WHERE lock.singleton = 1 AND lock.run_id IS NOT NULL"
        ).fetchone()
        if row is None:
            return None
        run = _row_to_run(row)
        return run if run.state in {"running", "publishing"} else None

    def accepted_outputs(self, run_id: str) -> tuple[OutputInventory, ...]:
        rows = self.connection.execute(
            "SELECT relative_path, sha256, byte_count FROM accepted_outputs "
            "WHERE run_id = ? ORDER BY relative_path",
            (run_id,),
        )
        return tuple(OutputInventory(str(row[0]), str(row[1]), int(row[2])) for row in rows)

    def health(self, stale_seconds: int) -> Health:
        row = self.connection.execute(
            "SELECT run_id, heartbeat_at FROM mutation_lock WHERE singleton = 1"
        ).fetchone()
        publishing = self.connection.execute(
            "SELECT id FROM runs WHERE state = 'publishing' ORDER BY id"
        ).fetchall()
        if row is None:
            return Health(False, "mutation lock is missing")
        active = self.active_run()
        if row["run_id"] is None:
            if publishing:
                return Health(False, "unresolved orphaned publication requires reconciliation")
            return Health(True, None)
        if active is None:
            return Health(False, "mutation lock owner is corrupt")
        if any(str(item["id"]) != active.run_id for item in publishing):
            return Health(False, "unresolved orphaned publication requires reconciliation")
        heartbeat = row["heartbeat_at"]
        if heartbeat is None or time.time() - float(heartbeat) > stale_seconds:
            return Health(False, "stale mutation lock requires exact-run recovery")
        return Health(True, None)

    def recover_stale(self, run_id: str, reason: str, stale_seconds: int) -> None:
        """Explicitly release only a named stale lock with immutable terminal evidence."""
        cleaned = reason.strip()
        if not cleaned or len(cleaned) > 500 or _contains_secret(cleaned):
            raise ValueError("recovery reason must be 1..500 secret-free characters")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT run_id, heartbeat_at FROM mutation_lock WHERE singleton = 1"
            ).fetchone()
            if row is None or row["run_id"] != run_id or row["heartbeat_at"] is None:
                raise RuntimeError("requested run does not own the mutation lock")
            if time.time() - float(row["heartbeat_at"]) <= stale_seconds:
                raise RuntimeError("requested mutation lock is not stale")
            run_row = self.connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run_row is None:
                raise RuntimeError("requested run is missing")
            run = _row_to_run(run_row)
            manifest = write_manifest(
                self.workspace,
                run.run_id,
                run.command,
                "stale_recovered",
                reason=cleaned,
                created_at=run.created_at,
                started_at=run.started_at,
                run_log=run_log_inventory(self.workspace, run.run_id),
            )
            self.connection.execute("DELETE FROM accepted_outputs WHERE run_id = ?", (run_id,))
            self.connection.execute(
                "UPDATE runs SET state = 'stale_recovered', recovery_reason = ?, "
                "manifest_json = ?, ended_at = ? "
                "WHERE id = ? AND state NOT IN "
                "('completed', 'failed', 'cancelled', 'timed_out', 'stale_recovered')",
                (cleaned, canonical_json(manifest), time.time(), run_id),
            )
            self.connection.execute(
                "UPDATE mutation_lock SET run_id = NULL, heartbeat_at = NULL WHERE singleton = 1"
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def reconcile_publications(self) -> tuple[ReconciledPublication, ...]:
        """Classify orphaned publications as complete only when every intended file verifies."""
        lock = self.connection.execute(
            "SELECT run_id FROM mutation_lock WHERE singleton = 1"
        ).fetchone()
        if lock is not None and lock["run_id"] is not None:
            return ()
        results: list[ReconciledPublication] = []
        for row in self.connection.execute(
            "SELECT * FROM runs WHERE state = 'publishing' ORDER BY created_at"
        ):
            run = _row_to_run(row)
            expected = _decode_inventory(row["publishing_inventory_json"])
            outputs = _published_inventory(self.layout.root, run.run_id, expected)
            if outputs is None:
                results.append(
                    ReconciledPublication(run, (), "failed", "publication_recovery_invalid")
                )
            else:
                results.append(ReconciledPublication(run, outputs, "completed", None))
        return tuple(results)

    def finalize_reconciled(
        self, run: Run, manifest: dict[str, object], outputs: Sequence[OutputInventory] = ()
    ) -> None:
        """Terminalize an orphaned publication after immutable evidence is durable."""
        state = cast(TerminalRunState, str(manifest.get("state", "failed")))
        if state not in _TERMINAL:
            raise ValueError("reconciled publication manifest state is invalid")
        encoded = canonical_json(manifest)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if state == "completed":
                for item in outputs:
                    _validate_output(item)
                    self.connection.execute(
                        "INSERT OR IGNORE INTO accepted_outputs(relative_path, run_id, sha256, "
                        "byte_count) VALUES(?, ?, ?, ?)",
                        (item.relative_path, run.run_id, item.sha256, item.byte_count),
                    )
            else:
                self.connection.execute(
                    "DELETE FROM accepted_outputs WHERE run_id = ?", (run.run_id,)
                )
            changed = self.connection.execute(
                "UPDATE runs SET state = ?, manifest_json = ?, ended_at = ? "
                "WHERE id = ? AND state = 'publishing'",
                (state, encoded, time.time(), run.run_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError("reconciled publication cannot be terminalized")
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def backup(self, destination: Path) -> None:
        """Create a no-replace atomic bundle with a SQLite snapshot and accepted evidence."""
        final = _safe_backup_destination(self.layout.backups, destination)
        temporary = self.layout.backups / f".{final.name}.tmp-{uuid.uuid4().hex}"
        temporary.mkdir(mode=0o700)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                snapshot = temporary / "state.sqlite3"
                self._backup_database(snapshot)
                evidence = temporary / "evidence"
                entries = _backup_evidence(self.layout.root, self.connection)
                if entries:
                    evidence.mkdir(mode=0o700)
                inventory: list[dict[str, object]] = []
                for source, relative in entries:
                    target = evidence / relative
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    inventory.append(_inventory_item(target, f"evidence/{relative.as_posix()}"))
                manifest = {
                    "schema_version": SCHEMA_VERSION,
                    "state_snapshot": _inventory_item(snapshot, "state.sqlite3"),
                    "evidence": inventory,
                }
                _write_backup_manifest(temporary / "manifest.json", manifest)
                self.connection.execute("COMMIT")
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
            if final.exists() or final.is_symlink():
                raise FileExistsError("backup bundle already exists")
            os.rename(temporary, final)
        except BaseException:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise

    def verify_backup(self, backup: Path) -> None:
        """Reject missing, partial, symlinked, overwritten, or corrupt backup evidence."""
        if backup.is_symlink() or not backup.is_dir():
            raise ValueError("backup bundle is missing or unsafe")
        _assert_tree_safe(backup)
        manifest_path = backup / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("backup bundle manifest is missing or unsafe")
        snapshot, evidence = _read_backup_manifest(manifest_path)
        _verify_inventory_item(backup, snapshot)
        self._verify_backup_database(backup / "state.sqlite3")
        for item in evidence:
            _verify_inventory_item(backup, item)
        _assert_bundle_contents(backup, evidence)
        _verify_bundle_evidence(backup, evidence)

    def _backup_database(self, destination: Path) -> None:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("backup snapshot already exists")
        source = self.connection
        reader: sqlite3.Connection | None = None
        if self.connection.in_transaction:
            reader = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            source = reader
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
            self._verify_connection(target)
        finally:
            target.close()
            if reader is not None:
                reader.close()

    def _verify_backup_database(self, backup: Path) -> None:
        if backup.is_symlink() or not backup.is_file():
            raise ValueError("backup must be a regular file")
        connection = sqlite3.connect(f"file:{backup}?mode=ro&immutable=1", uri=True)
        try:
            self._verify_connection(connection)
        finally:
            connection.close()

    @staticmethod
    def _verify_connection(connection: sqlite3.Connection) -> None:
        row = connection.execute("PRAGMA integrity_check").fetchone()
        if row is None or str(row[0]).lower() != "ok":
            raise RuntimeError("SQLite integrity check failed")


def _row_to_run(row: sqlite3.Row) -> Run:
    state = str(row["state"])
    if state not in {"queued", "running", "publishing", *_TERMINAL}:
        raise RuntimeError("run state is corrupt")
    return Run(
        str(row["id"]),
        str(row["command"]),
        cast(RunState, state),
        int(row["fence"]),
        float(row["created_at"]),
        None if row["heartbeat_at"] is None else float(row["heartbeat_at"]),
        bool(row["cancellation_requested"]),
        None if row["recovery_reason"] is None else str(row["recovery_reason"]),
        None if row["started_at"] is None else float(row["started_at"]),
        None if row["ended_at"] is None else float(row["ended_at"]),
    )


def _encode_inventory(outputs: Sequence[OutputInventory]) -> str:
    for item in outputs:
        _validate_output(item)
    return canonical_json(
        [
            {
                "byte_count": item.byte_count,
                "relative_path": item.relative_path,
                "sha256": item.sha256,
            }
            for item in sorted(outputs, key=lambda item: item.relative_path)
        ]
    )


def _decode_inventory_for_run(
    connection: sqlite3.Connection, run_id: str
) -> tuple[OutputInventory, ...]:
    row = connection.execute(
        "SELECT publishing_inventory_json FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    return _decode_inventory(None if row is None else row[0])


def _decode_inventory(encoded: object) -> tuple[OutputInventory, ...]:
    if not isinstance(encoded, str):
        raise RuntimeError("publishing inventory is missing")
    raw = json.loads(encoded)
    if not isinstance(raw, list):
        raise RuntimeError("publishing inventory is corrupt")
    results: list[OutputInventory] = []
    for item in raw:
        if not isinstance(item, dict):
            raise RuntimeError("publishing inventory is corrupt")
        path, digest, count = item.get("relative_path"), item.get("sha256"), item.get("byte_count")
        if not isinstance(path, str) or not isinstance(digest, str) or not isinstance(count, int):
            raise RuntimeError("publishing inventory is corrupt")
        output = OutputInventory(path, digest, count)
        _validate_output(output)
        results.append(output)
    return tuple(sorted(results, key=lambda item: item.relative_path))


def _staged_from_published(
    run_id: str, outputs: Sequence[OutputInventory]
) -> tuple[OutputInventory, ...]:
    prefix = f"outputs/{run_id}/"
    values: list[OutputInventory] = []
    for item in outputs:
        if not item.relative_path.startswith(prefix):
            raise RuntimeError("published output path is invalid")
        values.append(
            OutputInventory(item.relative_path.removeprefix(prefix), item.sha256, item.byte_count)
        )
    return tuple(sorted(values, key=lambda item: item.relative_path))


def _published_inventory(
    root: Path, run_id: str, expected: Sequence[OutputInventory]
) -> tuple[OutputInventory, ...] | None:
    directory = root / "outputs" / run_id
    try:
        _assert_directory_inventory(directory, {Path(item.relative_path) for item in expected})
    except ValueError:
        return None
    published: list[OutputInventory] = []
    for item in expected:
        path = directory / item.relative_path
        if path.is_symlink() or not path.is_file() or path.stat().st_size != item.byte_count:
            return None
        if sha256_file(path) != item.sha256:
            return None
        published.append(
            OutputInventory(f"outputs/{run_id}/{item.relative_path}", item.sha256, item.byte_count)
        )
    return tuple(published)


def _validate_output(item: OutputInventory) -> None:
    if (
        not item.relative_path
        or item.relative_path.startswith("/")
        or ".." in Path(item.relative_path).parts
    ):
        raise ValueError("output inventory path is invalid")
    if (
        item.byte_count < 0
        or len(item.sha256) != 64
        or any(char not in "0123456789abcdef" for char in item.sha256)
    ):
        raise ValueError("output inventory is invalid")


def _safe_backup_destination(backups: Path, destination: Path) -> Path:
    name = destination.name
    if (
        destination.parent not in {Path("."), backups}
        or name in {"", ".", ".."}
        or name.endswith(".sqlite3")
    ):
        raise ValueError("backup bundle name must be one safe path component")
    return backups / name


def _backup_evidence(root: Path, connection: sqlite3.Connection) -> tuple[tuple[Path, Path], ...]:
    invalid = connection.execute(
        "SELECT 1 FROM accepted_outputs AS output "
        "JOIN runs AS run ON run.id = output.run_id WHERE run.state != 'completed'"
    ).fetchone()
    if invalid is not None:
        raise RuntimeError("accepted output belongs to a non-completed run")
    rows = connection.execute(
        "SELECT r.id, r.command FROM runs AS r WHERE r.state = 'completed' ORDER BY r.id"
    )
    evidence: list[tuple[Path, Path]] = []
    for row in rows:
        run_id = str(row[0])
        manifest = root / "runs" / "manifests" / f"{run_id}.json"
        if manifest.is_symlink() or not manifest.is_file():
            raise RuntimeError("accepted run manifest is missing or unsafe")
        outputs = tuple(
            OutputInventory(str(output[0]), str(output[1]), int(output[2]))
            for output in connection.execute(
                "SELECT relative_path, sha256, byte_count FROM accepted_outputs "
                "WHERE run_id = ? ORDER BY relative_path",
                (run_id,),
            )
        )
        _verify_run_manifest(manifest, run_id, str(row[1]), outputs)
        evidence.append((manifest, Path("manifests") / manifest.name))
        output_root = root / "outputs" / run_id
        expected = {
            Path(item.relative_path).relative_to(Path("outputs") / run_id) for item in outputs
        }
        _assert_directory_inventory(output_root, expected)
        for output in outputs:
            _validate_output(output)
            path = root / output.relative_path
            if path.stat().st_size != output.byte_count or sha256_file(path) != output.sha256:
                raise RuntimeError("accepted output evidence checksum is invalid")
            evidence.append((path, Path(output.relative_path)))
    return tuple(evidence)


def _inventory_item(path: Path, relative_path: str) -> dict[str, object]:
    return {"path": relative_path, "sha256": sha256_file(path), "byte_count": path.stat().st_size}


def _write_backup_manifest(path: Path, manifest: dict[str, object]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(canonical_json(manifest))
        handle.flush()
        os.fsync(handle.fileno())


def _read_backup_manifest(
    manifest_path: Path,
) -> tuple[dict[object, object], tuple[dict[object, object], ...]]:
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("backup bundle manifest is invalid") from error
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "state_snapshot", "evidence"}:
        raise ValueError("backup bundle schema is invalid")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("backup bundle schema is invalid")
    snapshot, evidence = raw.get("state_snapshot"), raw.get("evidence")
    if not isinstance(snapshot, dict) or not isinstance(evidence, list):
        raise ValueError("backup bundle inventory is invalid")
    if snapshot.get("path") != "state.sqlite3":
        raise ValueError("backup state snapshot inventory is invalid")
    items: list[dict[object, object]] = []
    for item in evidence:
        if not isinstance(item, dict):
            raise ValueError("backup evidence inventory is invalid")
        path = item.get("path")
        if not isinstance(path, str) or not path.startswith("evidence/"):
            raise ValueError("backup evidence inventory path is invalid")
        items.append(item)
    return snapshot, tuple(items)


def _verify_inventory_item(root: Path, item: dict[object, object]) -> None:
    path, digest, count = item.get("path"), item.get("sha256"), item.get("byte_count")
    if not isinstance(path, str) or not isinstance(digest, str) or not isinstance(count, int):
        raise ValueError("backup inventory entry is invalid")
    relative = Path(path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("backup inventory path is invalid")
    target = root / path
    if (
        target.is_symlink()
        or not target.is_file()
        or target.stat().st_size != count
        or sha256_file(target) != digest
    ):
        raise ValueError("backup inventory checksum is invalid")


def _assert_bundle_contents(backup: Path, evidence: Sequence[dict[object, object]]) -> None:
    expected_files = {Path("manifest.json"), Path("state.sqlite3")}
    for item in evidence:
        path = item.get("path")
        if not isinstance(path, str):
            raise ValueError("backup evidence inventory is invalid")
        relative = Path(path)
        if relative in expected_files:
            raise ValueError("backup evidence inventory is duplicated")
        expected_files.add(relative)
    actual_files: set[Path] = set()
    actual_directories: set[Path] = {Path(".")}
    for candidate in backup.rglob("*"):
        relative = candidate.relative_to(backup)
        if candidate.is_dir():
            actual_directories.add(relative)
        elif candidate.is_file():
            actual_files.add(relative)
        else:
            raise ValueError("backup bundle contains unsafe filesystem evidence")
    expected_directories = {Path(".")}
    if evidence:
        expected_directories.add(Path("evidence"))
    for path in expected_files:
        parent = path.parent
        while parent != Path("."):
            expected_directories.add(parent)
            parent = parent.parent
    if actual_files != expected_files or actual_directories != expected_directories:
        raise ValueError("backup bundle contains missing or extra evidence")


def _verify_bundle_evidence(backup: Path, inventory: Sequence[dict[object, object]]) -> None:
    connection = sqlite3.connect(f"file:{backup / 'state.sqlite3'}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        invalid = connection.execute(
            "SELECT 1 FROM accepted_outputs AS output "
            "JOIN runs AS run ON run.id = output.run_id WHERE run.state != 'completed'"
        ).fetchone()
        if invalid is not None:
            raise ValueError("backup accepted output belongs to a non-completed run")
        expected_evidence: set[Path] = set()
        rows = connection.execute(
            "SELECT id, command FROM runs WHERE state = 'completed' ORDER BY id"
        )
        for row in rows:
            run_id = str(row["id"])
            outputs = tuple(
                OutputInventory(str(output[0]), str(output[1]), int(output[2]))
                for output in connection.execute(
                    "SELECT relative_path, sha256, byte_count FROM accepted_outputs "
                    "WHERE run_id = ? ORDER BY relative_path",
                    (run_id,),
                )
            )
            for item in outputs:
                _validate_output(item)
            manifest = backup / "evidence" / "manifests" / f"{run_id}.json"
            expected_evidence.add(Path("evidence") / "manifests" / f"{run_id}.json")
            _verify_run_manifest(manifest, run_id, str(row["command"]), outputs)
            if outputs:
                output_root = backup / "evidence" / "outputs" / run_id
                expected = {
                    Path(item.relative_path).relative_to(Path("outputs") / run_id)
                    for item in outputs
                }
                _assert_directory_inventory(output_root, expected)
            for item in outputs:
                evidence = backup / "evidence" / item.relative_path
                expected_evidence.add(Path("evidence") / item.relative_path)
                if (
                    evidence.stat().st_size != item.byte_count
                    or sha256_file(evidence) != item.sha256
                ):
                    raise ValueError("backup accepted output checksum is invalid")
        actual_evidence = {
            Path(path)
            for item in inventory
            for path in (item.get("path"),)
            if isinstance(path, str)
        }
        if actual_evidence != expected_evidence:
            raise ValueError("backup evidence inventory does not match durable state")
    except sqlite3.Error as error:
        raise ValueError("backup state evidence is invalid") from error
    finally:
        connection.close()


def _verify_run_manifest(
    path: Path, run_id: str, command: str, outputs: Sequence[OutputInventory]
) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("accepted run manifest is missing or unsafe")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("accepted run manifest is invalid") from error
    if not isinstance(raw, dict):
        raise ValueError("accepted run manifest is invalid")
    if (
        raw.get("run_id") != run_id
        or raw.get("command") != command
        or raw.get("state") != "completed"
    ):
        raise ValueError("accepted run manifest does not match durable state")
    entries = raw.get("outputs")
    if not isinstance(entries, list):
        raise ValueError("accepted run manifest output inventory is invalid")
    manifest_outputs: list[OutputInventory] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("accepted run manifest output inventory is invalid")
        relative, digest, count = entry.get("path"), entry.get("sha256"), entry.get("byte_count")
        if (
            not isinstance(relative, str)
            or not isinstance(digest, str)
            or not isinstance(count, int)
        ):
            raise ValueError("accepted run manifest output inventory is invalid")
        item = OutputInventory(relative, digest, count)
        _validate_output(item)
        manifest_outputs.append(item)
    if tuple(sorted(manifest_outputs, key=lambda item: item.relative_path)) != tuple(outputs):
        raise ValueError("accepted run manifest does not match accepted outputs")


def _assert_directory_inventory(directory: Path, expected: set[Path]) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("accepted output directory is missing or unsafe")
    actual: set[Path] = set()
    for candidate in directory.rglob("*"):
        relative = candidate.relative_to(directory)
        if candidate.is_symlink():
            raise ValueError("accepted output directory contains a symbolic link")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise ValueError("accepted output directory contains unsafe evidence")
        actual.add(relative)
    if actual != expected:
        raise ValueError("accepted output directory contains missing or extra evidence")


def _contains_secret(value: str) -> bool:
    return any(
        marker in value.lower()
        for marker in ("secret", "token", "password", "credential", "authorization")
    )


def _assert_tree_safe(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("backup bundle contains a symbolic link")
