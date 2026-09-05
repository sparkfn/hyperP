from __future__ import annotations

from pathlib import Path

import pytest
from intelligence.artifacts import publish_file, write_manifest
from intelligence.config import RuntimeConfig
from intelligence.registry import PRODUCTION_REGISTRY, RegisteredCommand, Registry
from intelligence.runtime import IntelligenceRuntime
from intelligence.state import State


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
