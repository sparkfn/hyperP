"""Durable recovery outcomes for interrupted output publication."""

from __future__ import annotations

from pathlib import Path

from intelligence.artifacts import publish_inventory, scan_staged_outputs
from intelligence.config import RuntimeConfig
from intelligence.registry import Registry
from intelligence.runtime import IntelligenceRuntime
from intelligence.state import State


def _orphan_publishing_run(tmp_path: Path, *, publish: bool) -> tuple[str, State]:
    state = State(tmp_path)
    run = state.create_mutating_run("approved")
    staging = state.layout.staging / run.run_id
    staging.mkdir()
    (staging / "result.json").write_text("{}", encoding="utf-8")
    inventory = scan_staged_outputs(tmp_path, run.run_id, 100)
    state.begin_publishing(run, inventory)
    if publish:
        publish_inventory(tmp_path, run.run_id, inventory, 100)
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
