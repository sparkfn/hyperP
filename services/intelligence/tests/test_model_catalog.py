"""State/terminal-manifest and replay-conflict contracts for model artifact catalogs."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from intelligence.artifacts import canonical_json, write_manifest
from intelligence.artifacts_staging import publish_inventory, scan_staged_outputs
from intelligence.datasets.bounds import ReadBudget
from intelligence.model_workflows import catalog
from intelligence.model_workflows.artifacts import (
    EVALUATION_DESCRIPTOR_SCHEMA,
    descriptor_value,
    verify_train_bundle,
)
from intelligence.model_workflows.contracts import digest
from intelligence.state import State
from test_model_artifacts import _bundle


def _publish(workspace: Path, mutate: Callable[[Path, str], None] | None = None) -> tuple[str, str]:
    state = State(workspace)
    try:
        run = state.create_mutating_run("train_run")
        staging = state.layout.staging / run.run_id
        staging.mkdir(mode=0o700, parents=True)
        model_id, _evaluation_id = _bundle(staging, run.run_id)
        if mutate is not None:
            mutate(staging, model_id)
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
        return run.run_id, model_id
    finally:
        state.close()


def test_catalog_requires_state_registered_terminal_complete_bundle(tmp_path: Path) -> None:
    run_id, model_id = _publish(tmp_path)
    entry = catalog.find(tmp_path, model_id, run_id)
    assert entry.model_id == model_id
    evaluation = catalog.find_evaluation(
        tmp_path, entry.evaluation_descriptor.evaluation_id, run_id
    )
    assert evaluation.evaluation_id == entry.evaluation_descriptor.evaluation_id
    assert catalog.list_entries(tmp_path, 1) == (entry,)
    (tmp_path / "runs" / "manifests" / f"{run_id}.json").unlink()
    with pytest.raises(ValueError):
        catalog.find(tmp_path, model_id, run_id)


def test_catalog_rejects_extra_and_state_checksum_drift(tmp_path: Path) -> None:
    run_id, model_id = _publish(tmp_path)
    root = tmp_path / "outputs" / run_id
    (root / "unexpected.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="State-registered|extra"):
        catalog.find(tmp_path, model_id, run_id)
    (root / "unexpected.json").unlink()
    candidate = next((root / "models").iterdir()) / "candidate.json"
    candidate.write_text(candidate.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="State-registered|checksum|noncanonical"):
        catalog.find(tmp_path, model_id, run_id)


def test_catalog_overflow_and_conflicting_replay_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_id, model_id = _publish(tmp_path)
    accepted = catalog.find(tmp_path, model_id, run_id)

    class OverflowState:
        def completed_run_ids(self, _command: str, _limit: int) -> tuple[str, ...]:
            raise RuntimeError("completed run query exceeds catalog bound")

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        catalog.ReadOnlyState, "open", classmethod(lambda _cls, _workspace: OverflowState())
    )
    with pytest.raises(RuntimeError, match="bound"):
        catalog.entries(tmp_path)
    monkeypatch.undo()
    bundle = verify_train_bundle(tmp_path / "outputs" / run_id, run_id)
    changed_candidate = {**accepted.candidate, "model_id": "model-conflicting"}
    prior = catalog.ModelEntry(
        accepted.model_id,
        accepted.run_id,
        changed_candidate,
        accepted.evaluation,
        accepted.descriptor,
        accepted.evaluation_descriptor,
    )
    monkeypatch.setattr(catalog, "entries", lambda _workspace: (prior,))
    with pytest.raises(RuntimeError, match="replay conflicts"):
        catalog.check_model_replay_conflict(tmp_path, bundle)


def test_evaluation_catalog_rejects_self_consistent_nonheldout_population(tmp_path: Path) -> None:
    model_run_id, model_id = _publish(tmp_path)
    model = catalog.find(tmp_path, model_id, model_run_id)
    candidate_logical = model.candidate["logical"]
    assert isinstance(candidate_logical, dict)
    dataset = candidate_logical["dataset"]
    assert isinstance(dataset, dict)
    state = State(tmp_path)
    try:
        run = state.create_mutating_run("evaluate_run")
        staging = state.layout.staging / run.run_id
        staging.mkdir(mode=0o700, parents=True)
        member = "d" * 64
        confusion = {
            f"{actual}->{predicted}": 1 if actual == predicted == "lost" else 0
            for actual in ("lost", "open", "won")
            for predicted in ("lost", "open", "won")
        }
        logical = {
            "dataset": dataset,
            "evaluation_kind": "independent_held_out_replay",
            "labels": ["lost", "open", "won"],
            "metrics": {
                "accuracy": {"reason": None, "value": 1.0},
                "accuracy_denominator": 1,
                "confusion": confusion,
                "per_class": {
                    "lost": {
                        "precision": {"reason": None, "value": 1.0},
                        "precision_denominator": 1,
                        "recall": {"reason": None, "value": 1.0},
                        "recall_denominator": 1,
                    },
                    "open": {
                        "precision": {"reason": "zero_denominator", "value": None},
                        "precision_denominator": 0,
                        "recall": {"reason": "zero_denominator", "value": None},
                        "recall_denominator": 0,
                    },
                    "won": {
                        "precision": {"reason": "zero_denominator", "value": None},
                        "precision_denominator": 0,
                        "recall": {"reason": "zero_denominator", "value": None},
                        "recall_denominator": 0,
                    },
                },
            },
            "metrics_contract": "confusion-accuracy-precision-recall-v1",
            "model_id": model_id,
            "population_digest": digest([member]),
            "population_members": [member],
        }
        evaluation_id = f"evaluation-{digest(logical)[:24]}"
        evaluation = {
            "evaluation_id": evaluation_id,
            "logical": logical,
            "schema_version": "intelligence-model-evaluation-v1",
        }
        path = staging / "evaluations" / evaluation_id / "evaluation.json"
        path.parent.mkdir(mode=0o700, parents=True)
        path.write_text(
            __import__("intelligence.artifacts", fromlist=["canonical_json"]).canonical_json(
                evaluation
            ),
            encoding="utf-8",
        )
        inventory = [
            {
                "byte_count": path.stat().st_size,
                "relative_path": f"evaluations/{evaluation_id}/evaluation.json",
                "sha256": __import__(
                    "intelligence.artifacts", fromlist=["sha256_file"]
                ).sha256_file(path),
            }
        ]
        descriptor = descriptor_value(
            EVALUATION_DESCRIPTOR_SCHEMA,
            {
                "active": False,
                "command": "evaluate_run",
                "dataset": dataset,
                "evaluation_id": evaluation_id,
                "evaluation_kind": "independent_held_out_replay",
                "evaluation_logical_digest": digest(logical),
                "inventory": inventory,
                "model_id": model_id,
                "model_logical_digest": model.descriptor.model_logical_digest,
                "model_run_id": model_run_id,
                "request_digest": digest(
                    {
                        "dataset": dataset,
                        "evaluation_kind": "independent_held_out_replay",
                        "model_id": model_id,
                        "model_logical_digest": model.descriptor.model_logical_digest,
                        "model_run_id": model_run_id,
                    }
                ),
                "run_id": run.run_id,
            },
        )
        descriptor_path = (
            staging / "acceptance-descriptors" / "evaluations" / f"{evaluation_id}.json"
        )
        descriptor_path.parent.mkdir(mode=0o700, parents=True)
        descriptor_path.write_text(
            __import__("intelligence.artifacts", fromlist=["canonical_json"]).canonical_json(
                descriptor
            ),
            encoding="utf-8",
        )
        outputs = scan_staged_outputs(tmp_path, run.run_id, 100_000_000)
        state.mark_execution_quiescent(run)
        state.begin_publishing(run, outputs)
        published = publish_inventory(tmp_path, run.run_id, outputs, 100_000_000)
        state.complete_publication(run, published, {"accepted": "evaluation"})
        write_manifest(
            tmp_path,
            run.run_id,
            "evaluate_run",
            "completed",
            outputs=published,
            created_at=run.created_at,
            started_at=run.started_at,
            limits=dict(run.limits),
        )
    finally:
        state.close()
    with pytest.raises(ValueError, match="independent evaluation population is incompatible"):
        catalog.evaluation_for_run(tmp_path, run.run_id)


def test_r3_catalog_budget_supports_several_publications_and_fails_closed_at_boundary(
    tmp_path: Path,
) -> None:
    published = tuple(_publish(tmp_path) for _ in range(6))
    listed = catalog.list_entries(tmp_path, 10)
    assert {(entry.run_id, entry.model_id) for entry in listed} == set(published)
    bundle = verify_train_bundle(tmp_path / "outputs" / published[0][0], published[0][0])
    catalog.check_model_replay_conflict(tmp_path, bundle)
    with pytest.raises(RuntimeError, match="entry ceiling"):
        catalog.entries(tmp_path, ReadBudget(100_000_000, 1, 120_000))


def test_f4_catalog_accepts_exact_heldout_population_from_train_run(tmp_path: Path) -> None:
    run_id, model_id = _publish(tmp_path)
    model = catalog.find(tmp_path, model_id, run_id)
    evaluation = catalog.evaluation_for_run(tmp_path, run_id)
    population = model.candidate["logical"]["population"]
    assert evaluation.evaluation["logical"]["population_members"] == population["held_out_members"]
    assert (
        evaluation.evaluation["logical"]["population_digest"]
        == population["held_out_membership_digest"]
    )


def test_n3_valid_changed_missingness_conflicts_with_same_training_request(tmp_path: Path) -> None:
    _publish(tmp_path)

    def change_held_out_summary(staging: Path, model_id: str) -> None:
        missingness_path = staging / "models" / model_id / "missingness.json"
        missingness = json.loads(missingness_path.read_text(encoding="utf-8"))
        missingness["summaries"]["held_out"]["features"]["category_id"]["missing"] = 0
        unsigned = dict(missingness)
        unsigned.pop("digest")
        missingness["digest"] = digest(unsigned)
        missingness_path.write_text(canonical_json(missingness), encoding="utf-8", newline="\n")
        descriptor_path = staging / "acceptance-descriptors" / "models" / f"{model_id}.json"
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        descriptor["missingness_digest"] = digest(missingness)
        descriptor["inventory"] = [
            {
                "byte_count": (staging / item["relative_path"]).stat().st_size,
                "relative_path": item["relative_path"],
                "sha256": __import__("hashlib")
                .sha256((staging / item["relative_path"]).read_bytes())
                .hexdigest(),
            }
            for item in descriptor["inventory"]
        ]
        fields = dict(descriptor)
        fields.pop("descriptor_digest")
        schema = fields.pop("schema_version")
        descriptor_path.write_text(
            canonical_json(descriptor_value(schema, fields)), encoding="utf-8", newline="\n"
        )

    changed_run, _changed_model = _publish(tmp_path, change_held_out_summary)
    bundle = verify_train_bundle(tmp_path / "outputs" / changed_run, changed_run)
    with pytest.raises(RuntimeError, match="model replay conflicts"):
        catalog.check_model_replay_conflict(tmp_path, bundle)


def test_n2_catalog_rejects_external_symlink_before_opening_outside_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_id, model_id = _publish(tmp_path)
    root = tmp_path / "outputs" / run_id
    outside = tmp_path / "outside-models"
    models = root / "models"
    models.rename(outside)
    try:
        os.symlink(outside, models, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this filesystem")
    opened: list[Path] = []
    original_open = Path.open

    def track_open(path: Path, *args: object, **kwargs: object) -> object:
        if path.resolve().is_relative_to(outside):
            opened.append(path.resolve())
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", track_open)
    with pytest.raises(ValueError, match="unsafe"):
        catalog.find(tmp_path, model_id, run_id)
    assert opened == []
