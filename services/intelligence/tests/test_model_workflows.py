"""Offline model workflow contracts: deterministic, partial-safe, and inactive."""

from __future__ import annotations

from pathlib import Path

import pytest
from intelligence.cli import build_parser
from intelligence.config import RuntimeConfig
from intelligence.model_workflows import cli as model_cli
from intelligence.model_workflows.commands import train_registry
from intelligence.model_workflows.comparison import compare
from intelligence.model_workflows.contracts import RECIPE, TrainRequest
from intelligence.model_workflows.core import (
    AdmittedDataset,
    train_bundle,
    verify_staged_train,
    write_evaluation,
    write_train,
)
from intelligence.model_workflows.dataset_admission import DatasetPin
from intelligence.models import Run
from intelligence.registry import Registry


def _dataset() -> AdmittedDataset:
    rows: list[dict[str, object]] = []
    for index in range(20):
        rows.append(
            {
                "activity_coverage": "legacy_partial_snapshot",
                "activity_missingness_reason": None if index % 2 else "no_eligible_archive",
                "archived_activity_count_lower_bound": None if index % 2 else 999999,
                "category_id": "cat-a" if index % 2 else "cat-b",
                "companion_call_count_lower_bound": None,
                "disposition": "labeled",
                "included_activity_join_corroboration": "stored_parent_only",
                "label": ("won", "lost", "open")[index % 3],
                "person_id": f"person-{index}",
                "source_entity_id": f"deal-{index}",
                "source_instance_id": "instance-a",
                "source_system": "bitrix_chat",
                "stage_id": "stage-a",
                "stage_semantic_id": "semantic-a",
            }
        )
    return AdmittedDataset(
        TrainRequest("dataset-a", "run-a", RECIPE, 7),
        {"code_version": "dataset", "seed": 1},
        "a" * 64,
        "b" * 64,
        tuple(rows),
    )


def test_categorical_candidate_is_deterministic_inactive_and_ignores_count_magnitudes() -> None:
    first_model, first_evaluation = train_bundle(_dataset())
    changed = _dataset()
    rows = list(changed.rows)
    rows[0] = {**rows[0], "archived_activity_count_lower_bound": 1}
    second_model, second_evaluation = train_bundle(
        AdmittedDataset(
            changed.request,
            changed.config,
            changed.manifest_digest,
            changed.content_digest,
            tuple(rows),
        )
    )
    assert first_model == second_model
    assert first_evaluation == second_evaluation
    assert first_model["active"] is False
    logical = first_model["logical"]
    assert isinstance(logical, dict)
    assert "archived_activity_count_lower_bound" not in logical["features"]
    assert (
        logical["population"]["training_membership_digest"]
        != logical["population"]["held_out_membership_digest"]
    )


def test_malformed_or_extra_staged_evidence_is_rejected(tmp_path: Path) -> None:
    model_id, evaluation_id = write_train(tmp_path, _dataset())
    verify_staged_train(tmp_path, model_id, evaluation_id)
    (tmp_path / "models" / model_id / "unsafe.pkl").write_bytes(b"unsafe")
    with pytest.raises(ValueError, match="unsafe"):
        verify_staged_train(tmp_path, model_id, evaluation_id)


def test_population_artifact_excludes_raw_person_and_source_identity(tmp_path: Path) -> None:
    model_id, evaluation_id = write_train(tmp_path, _dataset())
    population = (tmp_path / "models" / model_id / "population.ndjson").read_text(encoding="utf-8")
    candidate = (tmp_path / "models" / model_id / "candidate.json").read_text(encoding="utf-8")
    assert "person-" not in population
    assert "deal-" not in population
    assert "instance-a" not in population
    assert '"person_id"' not in candidate
    assert evaluation_id


def test_comparison_rejects_different_population_before_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del tmp_path
    first = {
        "evaluation_id": "left",
        "schema_version": "intelligence-model-evaluation-v1",
        "logical": {
            "population_digest": "a",
            "labels": ["lost"],
            "metrics_contract": "v",
            "metrics": {},
        },
    }
    second = {
        "evaluation_id": "right",
        "schema_version": "intelligence-model-evaluation-v1",
        "logical": {
            "population_digest": "b",
            "labels": ["lost"],
            "metrics_contract": "v",
            "metrics": {},
        },
    }
    values = iter((first, second))

    class Entry:
        def __init__(self, evaluation: dict[str, object], identifier: str) -> None:
            self.evaluation = evaluation
            self.evaluation_id = identifier

    monkeypatch.setattr(
        "intelligence.model_workflows.comparison.evaluation_for_run",
        lambda *_args: Entry(next(values), "entry"),
    )
    with pytest.raises(ValueError, match="incompatible"):
        compare(Path("workspace"), "left-run", "right-run")


def test_independent_evaluation_selects_held_out_membership_hashes(tmp_path: Path) -> None:
    model, _evaluation = train_bundle(_dataset())
    dataset = _dataset()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("intelligence.model_workflows.core.admit_dataset", lambda *_args: dataset)
    try:
        evaluation_id = write_evaluation(
            tmp_path,
            __import__(
                "intelligence.model_workflows.contracts", fromlist=["EvaluationRequest"]
            ).EvaluationRequest("model", "run", "dataset-a", "run-a"),
            model,
        )
    finally:
        monkeypatch.undo()
    assert evaluation_id.startswith("evaluation-")


def test_model_verify_requires_only_exact_model_identity() -> None:
    parsed = build_parser().parse_args(
        ["model", "verify", "--model-id", "model-a", "--model-run-id", "run-a"]
    )
    assert parsed.model_id == "model-a"
    assert not hasattr(parsed, "dataset_id")


def test_parent_train_admission_metadata_contains_exact_source_snapshot_pins() -> None:
    dataset = _dataset()
    config = {
        "inputs": {
            "deal_refs": {
                "run_id": "deal-run",
                "boundary_digest": "a" * 64,
                "inventory_digest": "b" * 64,
                "snapshot_manifest_digest": "c" * 64,
            },
            "activities": {
                "checkpoint_id": "checkpoint",
                "accepted_run_id": "activity-run",
                "logical_snapshot_id": "snapshot",
                "boundary_digest": "d" * 64,
                "descriptor_digest": "e" * 64,
                "inventory_digest": "f" * 64,
                "manifest_digest": "0" * 64,
            },
        }
    }
    admitted = AdmittedDataset(
        dataset.request, config, dataset.manifest_digest, dataset.content_digest, dataset.rows
    )
    command = train_registry(admitted).get("train_run")
    assert command.runtime_limits is not None
    assert command.public_metadata["deal_run_id"] == "deal-run"
    assert command.public_metadata["activity_logical_snapshot_id"] == "snapshot"


def test_train_registry_does_not_capture_materialized_dataset_rows() -> None:
    dataset = _dataset()
    pin = DatasetPin(
        dataset.request,
        {
            "inputs": {
                "deal_refs": {
                    "run_id": "deal",
                    "boundary_digest": "a" * 64,
                    "inventory_digest": "b" * 64,
                    "snapshot_manifest_digest": "c" * 64,
                },
                "activities": {
                    "checkpoint_id": "check",
                    "accepted_run_id": "activity",
                    "logical_snapshot_id": "snap",
                    "boundary_digest": "d" * 64,
                    "descriptor_digest": "e" * 64,
                    "inventory_digest": "f" * 64,
                    "manifest_digest": "0" * 64,
                },
            }
        },
        dataset.manifest_digest,
        dataset.content_digest,
    )
    handler = train_registry(pin).get("train_run").execute
    assert hasattr(handler, "args")
    assert all(not isinstance(value, tuple) or value != dataset.rows for value in handler.args)


def test_cli_run_returns_nonzero_for_noncompleted_terminal_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeState:
        def inspect(self, _run_id: str) -> Run:
            return Run("run", "model_verify", "cancelled", 1, 1.0, 1.0)

    class FakeRuntime:
        def __init__(self, _config: RuntimeConfig, _registry: Registry) -> None:
            self.state = FakeState()

        def run(self, _name: str) -> str:
            return "run"

        def close(self) -> None:
            return None

    monkeypatch.setattr(model_cli, "IntelligenceRuntime", FakeRuntime)
    assert (
        model_cli._run(RuntimeConfig(tmp_path, mutations_enabled=True), Registry(), "model_verify")
        == 1
    )
    assert '"state": "cancelled"' in capsys.readouterr().out


def test_f1_merged_producer_fingerprint_remains_provenance() -> None:
    merged = "304ff6b45e8c209eb006841016aa3393f7ac4b73f0721ef96a2b97483746a671"
    dataset = _dataset()
    config = {
        "code_contract_fingerprint": merged,
        "inputs": {
            "deal_refs": {
                "run_id": "deal",
                "boundary_digest": "a" * 64,
                "inventory_digest": "b" * 64,
                "snapshot_manifest_digest": "c" * 64,
            },
            "activities": {
                "checkpoint_id": "check",
                "accepted_run_id": "activity",
                "logical_snapshot_id": "snap",
                "boundary_digest": "d" * 64,
                "descriptor_digest": "e" * 64,
                "inventory_digest": "f" * 64,
                "manifest_digest": "0" * 64,
            },
        },
    }
    pin = DatasetPin(dataset.request, config, dataset.manifest_digest, dataset.content_digest)
    assert pin.config["code_contract_fingerprint"] == merged


def test_f8_comparison_child_detects_recomputed_input_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from intelligence.model_workflows import commands

    comparison = {"comparison_id": "comparison-x", "logical": {}}
    monkeypatch.setattr(
        commands,
        "write_comparison",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not write")),
    )
    monkeypatch.setattr(
        "intelligence.model_workflows.comparison.compare",
        lambda *_args: {"comparison_id": "comparison-y", "logical": {}},
    )
    with pytest.raises(RuntimeError, match="comparison_input_drift"):
        commands._compare(comparison, "left", "right", tmp_path, lambda: False)
