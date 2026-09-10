"""Supervised terminal rejection evidence for model workflow domain boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from intelligence.artifacts import canonical_json, write_manifest
from intelligence.artifacts_staging import publish_inventory, scan_staged_outputs
from intelligence.config import RuntimeConfig
from intelligence.datasets.artifacts import write_dataset
from intelligence.datasets.definition import DatasetComputation, compute
from intelligence.datasets.models import DatasetRow
from intelligence.model_workflows import commands
from intelligence.model_workflows.artifacts import descriptor_value
from intelligence.model_workflows.catalog import find as find_model
from intelligence.model_workflows.commands import ModelPin, evaluation_registry, train_registry
from intelligence.model_workflows.contracts import RECIPE, EvaluationRequest, TrainRequest, digest
from intelligence.model_workflows.dataset_admission import admit_dataset, admit_dataset_metadata
from intelligence.registry import SafeRejectionError
from intelligence.runtime import IntelligenceRuntime
from intelligence.state import State
from test_datasets_artifacts import _publish_dataset
from test_datasets_definition import _inputs


def _publish_eligible_dataset(
    workspace: Path, ineligible_entity: str | None = None
) -> tuple[str, str]:
    inputs = _inputs(True)
    prototype = compute(inputs).rows[0]
    rows: list[DatasetRow] = []
    for index in range(20):
        row = replace(
            prototype,
            activity_missingness_reason=None if index % 2 else "no_eligible_archive",
            category_id="category-a" if index % 2 else "category-b",
            feature_source_record_pk=f"feature-{index}",
            label=("won", "lost", "open")[index % 3],
            person_id=f"person-{index}",
            source_entity_id=f"deal-{index}",
            stage_id="stage-a",
            stage_semantic_id="semantic-a",
        )
        if row.source_entity_id == ineligible_entity:
            row = replace(
                row,
                disposition="ineligible",
                feature_source_record_pk=None,
                person_id="api_secret=never-persist-this",
            )
        rows.append(row)
    state = State(workspace)
    try:
        run = state.create_mutating_run("dataset_build")
        staging = state.layout.staging / run.run_id
        staging.mkdir(mode=0o700, parents=True)
        artifact = write_dataset(staging, inputs, DatasetComputation(tuple(rows), ()))
        inventory = scan_staged_outputs(workspace, run.run_id, 100_000_000)
        state.mark_execution_quiescent(run)
        state.begin_publishing(run, inventory)
        published = publish_inventory(workspace, run.run_id, inventory, 100_000_000)
        state.complete_publication(run, published, {"dataset_id": artifact.descriptor.dataset_id})
        write_manifest(
            workspace,
            run.run_id,
            "dataset_build",
            "completed",
            outputs=published,
            created_at=run.created_at,
            started_at=run.started_at,
            limits=dict(run.limits),
        )
        return run.run_id, artifact.descriptor.dataset_id
    finally:
        state.close()


def _publish_missingness_conflict(workspace: Path, source_run_id: str) -> None:
    state = State(workspace)
    try:
        run = state.create_mutating_run("train_run")
        staging = state.layout.staging / run.run_id
        source = workspace / "outputs" / source_run_id
        shutil.copytree(source, staging)
        model_id = next((staging / "models").iterdir()).name
        missingness_path = staging / "models" / model_id / "missingness.json"
        missingness = json.loads(missingness_path.read_text(encoding="utf-8"))
        count = missingness["summaries"]["held_out"]["features"]["category_id"]["missing"]
        assert isinstance(count, int)
        missingness["summaries"]["held_out"]["features"]["category_id"]["missing"] = (
            1 if count == 0 else 0
        )
        unsigned = dict(missingness)
        unsigned.pop("digest")
        missingness["digest"] = digest(unsigned)
        missingness_path.write_text(canonical_json(missingness), encoding="utf-8", newline="\n")
        model_descriptor_path = staging / "acceptance-descriptors" / "models" / f"{model_id}.json"
        model_descriptor = json.loads(model_descriptor_path.read_text(encoding="utf-8"))
        model_descriptor["missingness_digest"] = digest(missingness)
        model_descriptor["run_id"] = run.run_id
        model_descriptor["inventory"] = [
            {
                "byte_count": (staging / item["relative_path"]).stat().st_size,
                "relative_path": item["relative_path"],
                "sha256": hashlib.sha256(
                    (staging / item["relative_path"]).read_bytes()
                ).hexdigest(),
            }
            for item in model_descriptor["inventory"]
        ]
        fields = dict(model_descriptor)
        fields.pop("descriptor_digest")
        schema = fields.pop("schema_version")
        model_descriptor_path.write_text(
            canonical_json(descriptor_value(schema, fields)), encoding="utf-8", newline="\n"
        )
        evaluation_id = model_descriptor["evaluation_id"]
        evaluation_descriptor_path = (
            staging / "acceptance-descriptors" / "evaluations" / f"{evaluation_id}.json"
        )
        evaluation_descriptor = json.loads(evaluation_descriptor_path.read_text(encoding="utf-8"))
        evaluation_descriptor["model_run_id"] = run.run_id
        evaluation_descriptor["request_digest"] = digest(
            {
                "dataset": evaluation_descriptor["dataset"],
                "evaluation_kind": evaluation_descriptor["evaluation_kind"],
                "model_id": evaluation_descriptor["model_id"],
                "model_logical_digest": evaluation_descriptor["model_logical_digest"],
                "model_run_id": run.run_id,
            }
        )
        evaluation_descriptor["run_id"] = run.run_id
        fields = dict(evaluation_descriptor)
        fields.pop("descriptor_digest")
        schema = fields.pop("schema_version")
        evaluation_descriptor_path.write_text(
            canonical_json(descriptor_value(schema, fields)), encoding="utf-8", newline="\n"
        )
        inventory = scan_staged_outputs(workspace, run.run_id, 100_000_000)
        state.mark_execution_quiescent(run)
        state.begin_publishing(run, inventory)
        published = publish_inventory(workspace, run.run_id, inventory, 100_000_000)
        state.complete_publication(run, published, {"accepted": "model"})
        write_manifest(
            workspace,
            run.run_id,
            "train_run",
            "completed",
            outputs=published,
            created_at=run.created_at,
            started_at=run.started_at,
            limits=dict(run.limits),
        )
    finally:
        state.close()


def _terminal_reason(runtime: IntelligenceRuntime, command: str) -> tuple[str, str]:
    row = runtime.state.connection.execute(
        "SELECT id FROM runs WHERE command = ? ORDER BY created_at DESC", (command,)
    ).fetchone()
    assert row is not None
    run_id = str(row[0])
    manifest = (runtime.state.layout.manifests / f"{run_id}.json").read_text(encoding="utf-8")
    log = (runtime.state.layout.logs / f"{run_id}.ndjson").read_text(encoding="utf-8")
    return manifest, log


@pytest.mark.skipif(os.name == "nt", reason="model workflows require POSIX resource enforcement")
def test_r10_supervised_model_handlers_classify_actual_domain_failures(tmp_path: Path) -> None:
    dataset_run, dataset_id = _publish_eligible_dataset(tmp_path)
    request = TrainRequest(dataset_id, dataset_run, RECIPE, 7)
    dataset_pin = admit_dataset_metadata(tmp_path, request)
    runtime = IntelligenceRuntime(
        RuntimeConfig(tmp_path, mutations_enabled=True), train_registry(dataset_pin)
    )
    try:
        model_run_id = runtime.run("train_run")
    finally:
        runtime.close()
    model = find_model(
        tmp_path,
        next((tmp_path / "outputs" / model_run_id / "models").iterdir()).name,
        model_run_id,
    )
    admitted = admit_dataset(tmp_path, request)
    held = model.candidate["logical"]["population"]["held_out_members"]
    assert isinstance(held, list) and held
    held_entity = next(
        str(row["source_entity_id"])
        for row in admitted.rows
        if __import__(
            "intelligence.model_workflows.population", fromlist=["membership_hash"]
        ).membership_hash(row)
        == held[0]
    )
    incompatible_run, incompatible_id = _publish_eligible_dataset(tmp_path, held_entity)
    evaluation_request = EvaluationRequest(
        model.model_id, model_run_id, incompatible_id, incompatible_run
    )
    incompatible_pin = admit_dataset_metadata(
        tmp_path, TrainRequest(incompatible_id, incompatible_run, RECIPE, 7)
    )
    logical = model.candidate["logical"]
    assert isinstance(logical, dict) and isinstance(logical["seed"], int)
    model_pin = ModelPin(model.model_id, model_run_id, digest(logical), logical["seed"])
    runtime = IntelligenceRuntime(
        RuntimeConfig(tmp_path, mutations_enabled=True),
        evaluation_registry(evaluation_request, incompatible_pin, model_pin),
    )
    try:
        with pytest.raises(RuntimeError, match="process failed"):
            runtime.run("evaluate_run")
        manifest, log = _terminal_reason(runtime, "evaluate_run")
        assert '"reason":"incompatible_population"' in manifest
        assert '"reason":"incompatible_population"' in log
        assert "api_secret=never-persist-this" not in manifest
        assert "api_secret=never-persist-this" not in log
    finally:
        runtime.close()
    _publish_missingness_conflict(tmp_path, model_run_id)
    runtime = IntelligenceRuntime(
        RuntimeConfig(tmp_path, mutations_enabled=True), train_registry(dataset_pin)
    )
    try:
        with pytest.raises(RuntimeError, match="process failed"):
            runtime.run("train_run")
        manifest, log = _terminal_reason(runtime, "train_run")
        assert '"reason":"replay_conflict"' in manifest
        assert '"reason":"replay_conflict"' in log
    finally:
        runtime.close()


@pytest.mark.skipif(os.name == "nt", reason="model workflows require POSIX resource enforcement")
def test_r10_supervised_train_checksum_failure_is_malformed_without_raw_text(
    tmp_path: Path,
) -> None:
    dataset_run, descriptor = _publish_dataset(tmp_path)
    request = TrainRequest(descriptor.dataset_id, dataset_run, RECIPE, 7)
    dataset_pin = admit_dataset_metadata(tmp_path, request)
    rows = tmp_path / "outputs" / dataset_run / "datasets" / descriptor.dataset_id / "rows.ndjson"
    rows.write_text(
        rows.read_text(encoding="utf-8") + "api_secret=never-persist-this", encoding="utf-8"
    )
    runtime = IntelligenceRuntime(
        RuntimeConfig(tmp_path, mutations_enabled=True), train_registry(dataset_pin)
    )
    try:
        with pytest.raises(RuntimeError, match="process failed"):
            runtime.run("train_run")
        manifest, log = _terminal_reason(runtime, "train_run")
        assert '"reason":"malformed_artifact"' in manifest
        assert '"reason":"malformed_artifact"' in log
        assert "api_secret=never-persist-this" not in manifest
        assert "api_secret=never-persist-this" not in log
    finally:
        runtime.close()


def test_r10_compare_classifies_incompatible_target_and_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "intelligence.model_workflows.comparison.compare",
        lambda *_args: (_ for _ in ()).throw(
            ValueError("incompatible_evaluation_contract:dataset")
        ),
    )
    with pytest.raises(SafeRejectionError, match="incompatible_target"):
        commands._compare(
            {"comparison_id": "comparison-a", "logical": {}},
            "left",
            "right",
            tmp_path,
            lambda: False,
        )
    monkeypatch.setattr(
        "intelligence.model_workflows.comparison.compare",
        lambda *_args: {"comparison_id": "comparison-b", "logical": {}},
    )
    with pytest.raises(SafeRejectionError, match="comparison_drift"):
        commands._compare(
            {"comparison_id": "comparison-a", "logical": {}},
            "left",
            "right",
            tmp_path,
            lambda: False,
        )
