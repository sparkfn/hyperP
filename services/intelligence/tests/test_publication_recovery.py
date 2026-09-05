"""Durable recovery outcomes for interrupted output publication."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from intelligence import state as state_module
from intelligence.artifacts import (
    canonical_json,
    publish_inventory,
    scan_staged_outputs,
    sha256_file,
    workspace_layout,
    write_manifest,
)
from intelligence.config import RuntimeConfig
from intelligence.models import OutputInventory
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
    state.mark_execution_quiescent(run)
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


@pytest.mark.parametrize("kind", ("corrupt", "partial", "directory", "symlink"))
def test_post_publication_invalid_manifest_is_quarantined(tmp_path: Path, kind: str) -> None:
    """Post-rename recovery uses the independent rejected-manifest area."""
    run_id, state = _orphan_publishing_run(tmp_path, publish=True)
    manifest_path = state.layout.manifests / f"{run_id}.json"
    if kind == "corrupt":
        manifest_path.write_text("not-json", encoding="utf-8")
    elif kind == "partial":
        manifest_path.write_text('{"schema_version":2,"run_id":"broken"}', encoding="utf-8")
    elif kind == "directory":
        manifest_path.mkdir()
    else:
        target = tmp_path / "manifest-target"
        target.write_text("attacker", encoding="utf-8")
        try:
            manifest_path.unlink()
            manifest_path.symlink_to(target)
        except OSError:
            state.close()
            pytest.skip("symbolic links are unavailable in this test environment")
    state.close()
    runtime = IntelligenceRuntime(RuntimeConfig(tmp_path), Registry())
    try:
        run = runtime.state.inspect(run_id)
        assert run is not None and run.state == "completed"
        assert len(runtime.state.accepted_outputs(run_id)) == 1
        quarantine = tuple(
            (runtime.state.layout.rejected_manifests / run_id).glob(".rejected-manifest-*.json")
        )
        assert len(quarantine) == 1
        assert (runtime.state.layout.manifests / f"{run_id}.json").is_file()
    finally:
        runtime.close()


def test_legacy_v1_manifest_allows_post_publication_stale_recovery(tmp_path: Path) -> None:
    """A prior schema-v1 manifest remains valid after publication and stale recovery."""
    run_id, state = _orphan_publishing_run(tmp_path, publish=True, release_lock=False)
    try:
        run = state.inspect(run_id)
        assert run is not None
        output_path = state.layout.outputs / run_id / "result.json"
        output = OutputInventory(
            f"outputs/{run_id}/result.json",
            sha256_file(output_path),
            output_path.stat().st_size,
        )
        legacy = {
            "schema_version": 1,
            "run_id": run_id,
            "command": "approved",
            "created_at": run.created_at,
            "started_at": run.started_at,
            "ended_at": run.created_at + 1,
            "state": "completed",
            "limits": {
                "max_log_bytes": 101,
                "max_output_bytes": 202,
                "max_runtime_seconds": 303,
            },
            "outputs": [
                {
                    "byte_count": output.byte_count,
                    "path": output.relative_path,
                    "sha256": output.sha256,
                }
            ],
            "run_log": None,
        }
        (state.layout.manifests / f"{run_id}.json").write_text(
            canonical_json(legacy), encoding="utf-8"
        )
        state.connection.execute("UPDATE runs SET limits_json = NULL WHERE id = ?", (run_id,))
        state.connection.execute("UPDATE mutation_lock SET heartbeat_at = 0 WHERE singleton = 1")
        state.recover_stale(run_id, "operator recovery", 1)
        recovered = state.inspect(run_id)
        assert recovered is not None and recovered.state == "completed"
        assert state.accepted_outputs(run_id) == (output,)
    finally:
        state.close()


def test_legacy_v1_custom_limits_reused_during_lock_free_startup(tmp_path: Path) -> None:
    """A migrated v4 row does not impose current runtime limits on old evidence."""
    run_id, state = _orphan_publishing_run(tmp_path, publish=True)
    run = state.inspect(run_id)
    assert run is not None
    output_path = state.layout.outputs / run_id / "result.json"
    output = OutputInventory(
        f"outputs/{run_id}/result.json",
        sha256_file(output_path),
        output_path.stat().st_size,
    )
    legacy = {
        "schema_version": 1,
        "run_id": run_id,
        "command": "approved",
        "created_at": run.created_at,
        "started_at": run.started_at,
        "ended_at": run.created_at + 1,
        "state": "completed",
        "limits": {
            "max_log_bytes": 111,
            "max_output_bytes": 222,
            "max_runtime_seconds": 333,
        },
        "outputs": [
            {
                "byte_count": output.byte_count,
                "path": output.relative_path,
                "sha256": output.sha256,
            }
        ],
        "run_log": None,
    }
    (state.layout.manifests / f"{run_id}.json").write_text(canonical_json(legacy), encoding="utf-8")
    state.connection.execute("UPDATE runs SET limits_json = NULL WHERE id = ?", (run_id,))
    state.close()
    runtime = IntelligenceRuntime(RuntimeConfig(tmp_path), Registry())
    try:
        recovered = runtime.state.inspect(run_id)
        assert recovered is not None and recovered.state == "completed"
        assert (
            json.loads(
                (runtime.state.layout.manifests / f"{run_id}.json").read_text(encoding="utf-8")
            )["limits"]
            == legacy["limits"]
        )
    finally:
        runtime.close()


def test_unknown_limits_recovery_evidence_does_not_claim_defaults(tmp_path: Path) -> None:
    """A migrated active row emits explicit schema-v1 unknown-limit evidence."""
    state = State(tmp_path)
    run = state.create_mutating_run("approved")
    state.connection.execute("UPDATE runs SET limits_json = NULL WHERE id = ?", (run.run_id,))
    state.mark_execution_quiescent(run)
    (state.layout.manifests / f"{run.run_id}.json").unlink(missing_ok=True)
    state.connection.execute("UPDATE mutation_lock SET heartbeat_at = 0 WHERE singleton = 1")
    state.close()
    recreated = State(tmp_path, runtime_epoch="recreated-container")
    try:
        recreated.recover_stale(run.run_id, "v4 recovery", 1)
        evidence = json.loads(
            (recreated.layout.manifests / f"{run.run_id}.json").read_text(encoding="utf-8")
        )
        assert evidence["schema_version"] == 1
        assert evidence["limits"] == {}
    finally:
        recreated.close()


@pytest.mark.parametrize("run_state", ("running", "publishing"))
def test_actual_v4_active_rows_emit_unknown_limit_evidence(tmp_path: Path, run_state: str) -> None:
    """Real schema-v4 active rows recover without fabricating effective limits."""
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
        """,
    )
    connection.execute(
        "INSERT INTO mutation_lock(singleton, run_id, fence, heartbeat_at) "
        "VALUES(1, 'legacy-active', 1, 0)"
    )
    connection.execute(
        "INSERT INTO runs(id, command, state, fence, created_at, heartbeat_at, started_at, "
        "publishing_inventory_json) VALUES(?, 'approved', ?, 1, 1, 0, 1, ?)",
        ("legacy-active", run_state, "[]" if run_state == "publishing" else None),
    )
    connection.commit()
    connection.close()
    first = State(tmp_path, runtime_epoch="legacy-container")
    try:
        run = first.inspect("legacy-active")
        assert run is not None and run.execution_may_be_alive
        with pytest.raises(RuntimeError, match="execution-domain"):
            first.recover_stale("legacy-active", "same epoch", 1)
    finally:
        first.close()

    recreated = State(tmp_path, runtime_epoch="recreated-container")
    try:
        recreated.recover_stale("legacy-active", "v4 recovery", 1)
        evidence = json.loads(
            (recreated.layout.manifests / "legacy-active.json").read_text(encoding="utf-8")
        )
        assert evidence["schema_version"] == 1
        assert evidence["limits"] == {}
    finally:
        recreated.close()


def test_recovery_log_cap_uses_persisted_admission_limit(tmp_path: Path) -> None:
    """Startup reconciliation does not replace a small admitted log cap with config."""
    state = State(tmp_path)
    run = state.create_mutating_run(
        "approved",
        {
            "max_log_bytes": 350,
            "max_output_bytes": 1000,
            "max_output_entries": 10,
            "max_runtime_seconds": 30,
        },
    )
    (state.layout.staging / run.run_id).mkdir()
    (state.layout.staging / run.run_id / "result.json").write_text("{}", encoding="utf-8")
    inventory = scan_staged_outputs(tmp_path, run.run_id, 1000)
    state.begin_publishing(run, inventory)
    state.connection.execute("UPDATE mutation_lock SET run_id = NULL, heartbeat_at = NULL")
    from intelligence.artifacts import append_run_log

    append_run_log(
        tmp_path,
        run.run_id,
        "padding",
        {"value": "x" * 260},
        350,
        command=run.command,
    )
    state.close()
    runtime = IntelligenceRuntime(RuntimeConfig(tmp_path, max_log_bytes=10_000), Registry())
    try:
        log_path = runtime.state.layout.logs / f"{run.run_id}.ndjson"
        assert log_path.stat().st_size <= 350
        assert runtime.state.inspect(run.run_id) is not None
    finally:
        runtime.close()


def test_post_publication_quarantine_failure_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted quarantine preserves the stale lock until a later retry succeeds."""
    run_id, state = _orphan_publishing_run(tmp_path, publish=True, release_lock=False)
    try:
        (state.layout.manifests / f"{run_id}.json").write_text("not-json", encoding="utf-8")
        state.connection.execute("UPDATE mutation_lock SET heartbeat_at = 0 WHERE singleton = 1")

        def fail_quarantine(_workspace: Path, _run_id: str) -> Path | None:
            raise OSError("injected quarantine interruption")

        original = state_module.quarantine_manifest
        monkeypatch.setattr(state_module, "quarantine_manifest", fail_quarantine)
        with pytest.raises(OSError, match="interruption"):
            state.recover_stale(run_id, "operator recovery", 1)
        lock = state.connection.execute(
            "SELECT run_id FROM mutation_lock WHERE singleton = 1"
        ).fetchone()
        assert lock is not None and lock[0] == run_id
        monkeypatch.setattr(state_module, "quarantine_manifest", original)
        state.recover_stale(run_id, "operator retry", 1)
        recovered = state.inspect(run_id)
        assert recovered is not None and recovered.state == "completed"
        assert state.active_run() is None
    finally:
        state.close()


def test_startup_reuses_valid_preexisting_parent_manifest(tmp_path: Path) -> None:
    """A valid parent manifest survives the publication-to-DB crash window unchanged."""
    run_id, state = _orphan_publishing_run(tmp_path, publish=True)
    run = state.inspect(run_id)
    assert run is not None
    output_path = state.layout.outputs / run_id / "result.json"
    output = OutputInventory(
        f"outputs/{run_id}/result.json", sha256_file(output_path), output_path.stat().st_size
    )
    write_manifest(
        tmp_path,
        run_id,
        run.command,
        "completed",
        outputs=(output,),
        created_at=run.created_at,
        started_at=run.started_at,
        limits=dict(run.limits),
    )
    original = (state.layout.manifests / f"{run_id}.json").read_bytes()
    state.close()
    runtime = IntelligenceRuntime(RuntimeConfig(tmp_path), Registry())
    try:
        assert (runtime.state.layout.manifests / f"{run_id}.json").read_bytes() == original
        assert tuple((runtime.state.layout.rejected_manifests / run_id).glob("*")) == ()
        recovered = runtime.state.inspect(run_id)
        assert recovered is not None and recovered.state == "completed"
    finally:
        runtime.close()
