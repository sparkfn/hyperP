"""SQLite WAL state facade with fenced lifecycle and delegated persistence helpers."""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from intelligence import state_queries
from intelligence.artifacts import (
    DEFAULT_MANIFEST_LIMITS,
    MANIFEST_LIMIT_KEYS,
    canonical_json,
    quarantine_manifest,
    read_manifest,
    run_log_inventory,
    workspace_layout,
    write_manifest,
)
from intelligence.models import (
    Health,
    OutputInventory,
    ReconciledPublication,
    Run,
    TerminalRunState,
)
from intelligence.state_backup import (
    _contains_secret,
    create_backup,
    verify_backup_bundle,
)
from intelligence.state_publication import (
    _decode_inventory,
    _decode_inventory_for_run,
    _encode_inventory,
    _published_inventory,
    _row_to_run,
    _staged_from_published,
    _validate_output,
)
from intelligence.state_schema import (
    bootstrap,
    migrate_legacy_database,
    path_exists_safe,
    verify_connection,
)

_TERMINAL: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled", "timed_out", "stale_recovered"}
)


class State:
    """Foundation-owned durable state. Callers must close the instance."""

    def __init__(self, workspace: Path) -> None:
        self.layout = workspace_layout(workspace)
        self.workspace = self.layout.root
        migrate_legacy_database(
            self.workspace,
            self.layout.state_directory,
            self.layout.state_database,
            verify_connection,
        )
        self.path = self.layout.state_database
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        bootstrap(self.connection, verify_connection)

    path_exists_safe = staticmethod(path_exists_safe)

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self.connection.close()

    def create_mutating_run(self, command: str, limits: dict[str, int] | None = None) -> Run:
        """Claim the exclusive mutation lock or reject concurrent work."""
        run_id, now = uuid.uuid4().hex, time.time()
        effective_limits = dict(limits or DEFAULT_MANIFEST_LIMITS)
        if set(effective_limits) != MANIFEST_LIMIT_KEYS or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in effective_limits.values()
        ):
            raise ValueError("effective runtime limits are invalid")
        limits_json = canonical_json(effective_limits)
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
                "INSERT INTO runs(id, command, state, fence, created_at, heartbeat_at, started_at, "
                "limits_json) VALUES(?, ?, 'running', ?, ?, ?, ?, ?)",
                (run_id, command, fence, now, now, now, limits_json),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return Run(
            run_id,
            command,
            "running",
            fence,
            now,
            now,
            started_at=now,
            limits=tuple(sorted(effective_limits.items())),
        )

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
        return state_queries.is_cancelled(self.connection, run_id)

    def cancel(self, run_id: str) -> None:
        """Durably request cancellation before publication's non-cancellable commit point."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT state FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is not None and str(row[0]) == "publishing":
                raise RuntimeError("publishing has passed the cancellation commit point")
            self.connection.execute(
                "UPDATE runs SET cancellation_requested = 1 WHERE id = ? "
                "AND state IN ('queued', 'running')",
                (run_id,),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

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
            changed = self.connection.execute(
                "UPDATE mutation_lock SET run_id = NULL, heartbeat_at = NULL "
                "WHERE singleton = 1 AND run_id = ? AND fence = ?",
                (run.run_id, run.fence),
            ).rowcount
            if changed != 1:
                raise RuntimeError("run fence was lost before lock release")
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def inspect(self, run_id: str) -> Run | None:
        return state_queries.inspect(self.connection, run_id)

    def active_run(self) -> Run | None:
        """Return the current durable lock owner only when its run record agrees."""
        return state_queries.active_run(self.connection)

    def accepted_outputs(self, run_id: str) -> tuple[OutputInventory, ...]:
        return state_queries.accepted_outputs(self.connection, run_id)

    def health(self, stale_seconds: int) -> Health:
        return state_queries.health(self.connection, stale_seconds, self.active_run)

    def recover_stale(self, run_id: str, reason: str, stale_seconds: int) -> None:
        """Explicitly release only a named stale lock with immutable terminal evidence."""
        cleaned = reason.strip()
        if not cleaned or len(cleaned) > 500 or _contains_secret(cleaned):
            raise ValueError("recovery reason must be 1..500 secret-free characters")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT run_id, heartbeat_at, fence FROM mutation_lock WHERE singleton = 1"
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
            if int(row["fence"]) != run.fence:
                raise RuntimeError("mutation lock fence does not match the run")
            if run.state not in {"queued", "running", "publishing"}:
                raise RuntimeError("requested run is not recoverable")
            recovery_state: TerminalRunState = "stale_recovered"
            recovery_outputs: tuple[OutputInventory, ...] = ()
            recovery_reason: str | None = cleaned
            if run.state == "publishing":
                try:
                    expected = _decode_inventory(run_row["publishing_inventory_json"])
                    recovered = _published_inventory(self.workspace, run_id, expected)
                except (OSError, RuntimeError, TypeError, ValueError):
                    recovered = None
                if recovered is None:
                    recovery_state = "failed"
                    recovery_reason = "publication_recovery_invalid"
                else:
                    recovery_state = "completed"
                    recovery_outputs = recovered
                    recovery_reason = None
            manifest_path = self.layout.manifests / f"{run_id}.json"
            existing: dict[str, object] | None = None
            if manifest_path.exists() or manifest_path.is_symlink():
                try:
                    existing = read_manifest(
                        manifest_path,
                        expected_run_id=run_id,
                        expected_command=run.command,
                        expected_state=recovery_state,
                        expected_outputs=recovery_outputs,
                        expected_reason=recovery_reason,
                        expected_created_at=run.created_at,
                        expected_started_at=run.started_at,
                        expected_limits=dict(run.limits) if run.limits else None,
                        expected_run_log=run_log_inventory(self.workspace, run.run_id),
                    )
                except (OSError, ValueError):
                    quarantine_manifest(self.workspace, run_id)
            manifest = existing or write_manifest(
                self.workspace,
                run.run_id,
                run.command,
                recovery_state,
                outputs=recovery_outputs,
                reason=recovery_reason,
                created_at=run.created_at,
                started_at=run.started_at,
                limits=dict(run.limits) or DEFAULT_MANIFEST_LIMITS,
                run_log=run_log_inventory(self.workspace, run.run_id),
            )
            if recovery_state == "completed":
                self.connection.execute("DELETE FROM accepted_outputs WHERE run_id = ?", (run_id,))
                for item in recovery_outputs:
                    self.connection.execute(
                        "INSERT OR IGNORE INTO accepted_outputs(relative_path, run_id, sha256, "
                        "byte_count) VALUES(?, ?, ?, ?)",
                        (item.relative_path, run_id, item.sha256, item.byte_count),
                    )
            else:
                self.connection.execute("DELETE FROM accepted_outputs WHERE run_id = ?", (run_id,))
            changed = self.connection.execute(
                "UPDATE runs SET state = ?, recovery_reason = ?, "
                "manifest_json = ?, ended_at = ? "
                "WHERE id = ? AND state NOT IN "
                "('completed', 'failed', 'cancelled', 'timed_out', 'stale_recovered')",
                (recovery_state, recovery_reason, canonical_json(manifest), time.time(), run_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError("stale run transition was fenced out")
            changed = self.connection.execute(
                "UPDATE mutation_lock SET run_id = NULL, heartbeat_at = NULL "
                "WHERE singleton = 1 AND run_id = ? AND fence = ?",
                (run_id, run.fence),
            ).rowcount
            if changed != 1:
                raise RuntimeError("stale lock release was fenced out")
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
            try:
                expected = _decode_inventory(row["publishing_inventory_json"])
                outputs = _published_inventory(self.layout.root, run.run_id, expected)
            except (OSError, RuntimeError, TypeError, ValueError):
                outputs = None
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
                self.connection.execute(
                    "DELETE FROM accepted_outputs WHERE run_id = ?", (run.run_id,)
                )
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
        create_backup(self.layout, self.path, self.connection, destination)

    def verify_backup(self, backup: Path) -> None:
        """Reject missing, partial, symlinked, overwritten, or corrupt backup evidence."""
        verify_backup_bundle(backup)
