"""Durable recovery outcomes for interrupted output publication."""

from __future__ import annotations

from pathlib import Path

import pytest
from intelligence import state as state_module
from intelligence.artifacts import publish_inventory, scan_staged_outputs
from intelligence.config import RuntimeConfig
from intelligence.registry import Registry
from intelligence.runtime import IntelligenceRuntime
from intelligence.state import State


def _orphan_publishing_run(
    tmp_path: Path, *, publish: bool, release_lock: bool = True
) -> tuple[str, State]:
    state = State(tmp_path)
    run = state.create_mutating_run("approved")
    staging = state.layout.staging / run.run_id
    staging.mkdir()
    (staging / "result.json").write_text("{}", encoding="utf-8")
    inventory = scan_staged_outputs(tmp_path, run.run_id, 100)
    state.begin_publishing(run, inventory)
    if publish:
        publish_inventory(tmp_path, run.run_id, inventory, 100)
    if release_lock:
        state.connection.execute(
            "UPDATE mutation_lock SET run_id = NULL, heartbeat_at = NULL WHERE singleton = 1"
        )
    return run.run_id, state


def test_verified_orphan_publication_completes_on_startup(tmp_path: Path) -> None:
    """Startup registers every verified published file and writes completed evidence."""
    run_id, state = _orphan_publishing_run(tmp_path, publish=True)
    state.close()
    runtime = IntelligenceRuntime(RuntimeConfig(tmp_path), Registry())
    try:
        run = runtime.state.inspect(run_id)
        assert run is not None and run.state == "completed"
        assert len(runtime.state.accepted_outputs(run_id)) == 1
        assert '"state":"completed"' in (
            runtime.state.layout.manifests / f"{run_id}.json"
        ).read_text(encoding="utf-8")
        assert runtime.health().healthy
    finally:
        runtime.close()


def test_invalid_orphan_publication_fails_without_accepted_outputs(tmp_path: Path) -> None:
    """Partial publication becomes explicit failure rather than silent healthy loss."""
    run_id, state = _orphan_publishing_run(tmp_path, publish=False)
    state.close()
    runtime = IntelligenceRuntime(RuntimeConfig(tmp_path), Registry())
    try:
        run = runtime.state.inspect(run_id)
        assert run is not None and run.state == "failed"
        assert runtime.state.accepted_outputs(run_id) == ()
        manifest = (runtime.state.layout.manifests / f"{run_id}.json").read_text(encoding="utf-8")
        assert '"reason":"publication_recovery_invalid"' in manifest
        assert runtime.health().healthy
    finally:
        runtime.close()


def test_corrupt_publishing_inventory_is_terminalized_and_releases_fence(tmp_path: Path) -> None:
    """Malformed durable publication intent must not strand stale recovery."""
    run_id, state = _orphan_publishing_run(tmp_path, publish=False, release_lock=False)
    try:
        state.connection.execute(
            "UPDATE runs SET publishing_inventory_json = ? WHERE id = ?",
            ("not-json", run_id),
        )
        state.connection.execute("UPDATE mutation_lock SET heartbeat_at = 0 WHERE singleton = 1")
        state.recover_stale(run_id, "operator recovery", 1)
        run = state.inspect(run_id)
        assert run is not None and run.state == "failed"
        assert run.recovery_reason == "publication_recovery_invalid"
        assert state.accepted_outputs(run_id) == ()
        lock = state.connection.execute(
            "SELECT run_id FROM mutation_lock WHERE singleton = 1"
        ).fetchone()
        assert lock is not None and lock[0] is None
        manifest = (state.layout.manifests / f"{run_id}.json").read_text(encoding="utf-8")
        assert '"reason":"publication_recovery_invalid"' in manifest
    finally:
        state.close()


def test_unreadable_published_evidence_is_classified_as_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filesystem errors while hashing evidence must become a failed recovery outcome."""
    run_id, state = _orphan_publishing_run(tmp_path, publish=True, release_lock=False)

    def raise_filesystem_error(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("simulated evidence race")

    monkeypatch.setattr(state_module, "_published_inventory", raise_filesystem_error)
    try:
        state.connection.execute("UPDATE mutation_lock SET heartbeat_at = 0 WHERE singleton = 1")
        state.recover_stale(run_id, "operator recovery", 1)
        run = state.inspect(run_id)
        assert run is not None and run.state == "failed"
        assert run.recovery_reason == "publication_recovery_invalid"
        assert state.accepted_outputs(run_id) == ()
    finally:
        state.close()
