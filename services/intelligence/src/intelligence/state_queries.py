"""Typed read-side and health queries for the State facade."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable

from intelligence.models import Health, OutputInventory, Run
from intelligence.state_publication import _row_to_run


def inspect(connection: sqlite3.Connection, run_id: str) -> Run | None:
    row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return None if row is None else _row_to_run(row)


def active_run(connection: sqlite3.Connection) -> Run | None:
    row = connection.execute(
        "SELECT r.* FROM mutation_lock AS lock "
        "JOIN runs AS r ON r.id = lock.run_id AND r.fence = lock.fence "
        "WHERE lock.singleton = 1 AND lock.run_id IS NOT NULL"
    ).fetchone()
    if row is None:
        return None
    run = _row_to_run(row)
    return run if run.state in {"running", "publishing"} else None


def accepted_outputs(connection: sqlite3.Connection, run_id: str) -> tuple[OutputInventory, ...]:
    rows = connection.execute(
        "SELECT relative_path, sha256, byte_count FROM accepted_outputs "
        "WHERE run_id = ? ORDER BY relative_path",
        (run_id,),
    )
    return tuple(OutputInventory(str(row[0]), str(row[1]), int(row[2])) for row in rows)


def is_cancelled(connection: sqlite3.Connection, run_id: str) -> bool:
    row = connection.execute(
        "SELECT cancellation_requested FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    return row is not None and bool(row[0])


def health(
    connection: sqlite3.Connection, stale_seconds: int, active: Callable[[], Run | None]
) -> Health:
    row = connection.execute(
        "SELECT run_id, heartbeat_at FROM mutation_lock WHERE singleton = 1"
    ).fetchone()
    publishing = connection.execute(
        "SELECT id FROM runs WHERE state = 'publishing' ORDER BY id"
    ).fetchall()
    if row is None:
        return Health(False, "mutation lock is missing")
    current = active()
    if row["run_id"] is None:
        if publishing:
            return Health(False, "unresolved orphaned publication requires reconciliation")
        return Health(True, None)
    if current is None:
        return Health(False, "mutation lock owner is corrupt")
    if current.cleanup_unresolved:
        return Health(False, "cleanup-unresolved execution requires container recreation")
    if any(str(item["id"]) != current.run_id for item in publishing):
        return Health(False, "unresolved orphaned publication requires reconciliation")
    heartbeat = row["heartbeat_at"]
    if heartbeat is None or time.time() - float(heartbeat) > stale_seconds:
        return Health(False, "stale mutation lock requires exact-run recovery")
    return Health(True, None)
