"""Supervised dataset build/verify on canonical accepted dependency fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from intelligence.config import RuntimeConfig
from intelligence.datasets.admission import admit
from intelligence.datasets.bounds import ReadBudget
from intelligence.datasets.catalog import CatalogEntry, find_run
from intelligence.datasets.commands import _build_handler, build_registry, verify_registry
from intelligence.datasets.models import DatasetRequest
from intelligence.runtime import IntelligenceRuntime
from intelligence.state import State
from test_datasets_admission import _activity_run, _Config, _deal_run, _Runtime
from test_datasets_artifacts import _publish_dataset


def test_supervised_build_and_verify_publish_state_registered_evidence(tmp_path: Path) -> None:
    state = State(tmp_path)
    try:
        deal_run = _deal_run(state)
        activity_run, _snapshot = _activity_run(state)
        request = DatasetRequest(
            deal_run,
            "checkpoint-a",
            activity_run,
            "crm-deal-state-v1",
            "2026-01-01T12:00:00Z",
            "2026-01-02T12:00:00Z",
            7,
        )
        inputs = admit(_Runtime(_Config(tmp_path), state), request)
    finally:
        state.close()
    config = RuntimeConfig(tmp_path, mutations_enabled=True, max_runtime_seconds=20)
    runtime = IntelligenceRuntime(config, build_registry(inputs))
    try:
        build_run = runtime.run("dataset_build")
    finally:
        runtime.close()
    entry = find_run_entry(tmp_path, build_run)
    rows = (
        tmp_path / "outputs" / build_run / "datasets" / entry.descriptor.dataset_id / "rows.ndjson"
    )
    assert (
        json.loads(rows.read_text(encoding="utf-8").splitlines()[0])[
            "archived_activity_count_lower_bound"
        ]
        == 1
    )
    runtime = IntelligenceRuntime(config, verify_registry(inputs, entry.descriptor))
    try:
        verify_run = runtime.run("dataset_verify")
    finally:
        runtime.close()
    state = State(tmp_path)
    try:
        assert state.inspect(build_run).state == "completed"
        assert state.inspect(verify_run).state == "completed"
        assert state.accepted_outputs(verify_run)[0].relative_path.endswith(".json")
    finally:
        state.close()


def find_run_entry(workspace: Path, run_id: str) -> CatalogEntry:
    return find_run(workspace, run_id)


def test_catalog_uses_one_cumulative_budget_across_accepted_runs(tmp_path: Path) -> None:
    _publish_dataset(tmp_path)
    _publish_dataset(tmp_path)
    from intelligence.datasets.catalog import entries

    with __import__("pytest").raises(RuntimeError, match="entry ceiling"):
        entries(tmp_path, ReadBudget(10_000_000, 2, 10_000))


def test_revalidation_drift_and_cancellation_create_no_dataset_staging_output(
    tmp_path: Path,
) -> None:
    state = State(tmp_path)
    try:
        deal_run = _deal_run(state)
        activity_run, snapshot = _activity_run(state)
        request = DatasetRequest(
            deal_run,
            "checkpoint-a",
            activity_run,
            "crm-deal-state-v1",
            "2026-01-01T12:00:00Z",
            "2026-01-02T12:00:00Z",
            7,
        )
        inputs = admit(_Runtime(_Config(tmp_path), state), request)
    finally:
        state.close()
    cancelled = tmp_path / "staging" / "cancelled"
    cancelled.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="cancelled"):
        _build_handler(inputs, cancelled, lambda: True)
    assert not (cancelled / "datasets").exists()
    manifest = (
        tmp_path
        / "outputs"
        / activity_run
        / "snapshots"
        / "crm"
        / "activities"
        / snapshot
        / "manifest.json"
    )
    manifest.write_text(manifest.read_text(encoding="utf-8") + " ", encoding="utf-8")
    drifted = tmp_path / "staging" / "drifted"
    drifted.mkdir(parents=True)
    with pytest.raises((RuntimeError, ValueError)):
        _build_handler(inputs, drifted, lambda: False)
    assert not (drifted / "datasets").exists()


def test_supervised_failed_build_preserves_prior_accepted_dataset(tmp_path: Path) -> None:
    state = State(tmp_path)
    try:
        deal_run = _deal_run(state)
        activity_run, snapshot = _activity_run(state)
        request = DatasetRequest(
            deal_run,
            "checkpoint-a",
            activity_run,
            "crm-deal-state-v1",
            "2026-01-01T12:00:00Z",
            "2026-01-02T12:00:00Z",
            7,
        )
        inputs = admit(_Runtime(_Config(tmp_path), state), request)
    finally:
        state.close()
    config = RuntimeConfig(tmp_path, mutations_enabled=True, max_runtime_seconds=20)
    runtime = IntelligenceRuntime(config, build_registry(inputs))
    try:
        accepted_run = runtime.run("dataset_build")
    finally:
        runtime.close()
    prior = find_run_entry(tmp_path, accepted_run)
    rows = (
        tmp_path
        / "outputs"
        / accepted_run
        / "datasets"
        / prior.descriptor.dataset_id
        / "rows.ndjson"
    )
    before = rows.read_bytes()
    manifest = (
        tmp_path
        / "outputs"
        / activity_run
        / "snapshots"
        / "crm"
        / "activities"
        / snapshot
        / "manifest.json"
    )
    manifest.write_text(manifest.read_text(encoding="utf-8") + " ", encoding="utf-8")
    runtime = IntelligenceRuntime(config, build_registry(inputs))
    try:
        with pytest.raises(RuntimeError):
            runtime.run("dataset_build")
        failed = runtime.state.connection.execute(
            "SELECT id FROM runs WHERE command = 'dataset_build' AND state = 'failed' "
            "ORDER BY created_at DESC"
        ).fetchone()
        assert failed is not None
        assert runtime.state.accepted_outputs(str(failed[0])) == ()
    finally:
        runtime.close()
    assert find_run_entry(tmp_path, accepted_run).descriptor == prior.descriptor
    assert rows.read_bytes() == before
