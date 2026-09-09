"""Adversarial contracts for strict immutable model bundle evidence."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from intelligence.artifacts import canonical_json
from intelligence.model_workflows.artifacts import (
    EVALUATION_DESCRIPTOR_SCHEMA,
    MISSINGNESS_SCHEMA,
    MODEL_DESCRIPTOR_SCHEMA,
    POPULATION_SCHEMA,
    descriptor_value,
    verify_train_bundle,
)
from intelligence.model_workflows.contracts import ACTIVITY_PROVENANCE, FEATURES, digest


def _write(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(canonical_json(value), encoding="utf-8", newline="\n")


def _inventory(root: Path, paths: tuple[str, ...]) -> list[dict[str, object]]:
    return [
        {
            "byte_count": (root / path).stat().st_size,
            "relative_path": path,
            "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest(),
        }
        for path in sorted(paths)
    ]


def _bundle(
    root: Path,
    run_id: str = "train-run",
    *,
    rule_prediction: str = "lost",
    evaluation_labels: list[str] | None = None,
) -> tuple[str, str]:
    members = ("a" * 64, "b" * 64, "c" * 64)
    dataset = {
        "accepted_run_id": "dataset-run",
        "config_digest": "d" * 64,
        "content_digest": "e" * 64,
        "dataset_id": "dataset-id",
        "manifest_digest": "f" * 64,
        "provenance": ACTIVITY_PROVENANCE,
    }
    population = {
        "exclusions": {"not_eligible_labeled_person": 1},
        "held_out_members": [members[2]],
        "held_out_membership_digest": digest([members[2]]),
        "split_contract": "person-group-sha256-mod5-v1",
        "training_members": [members[0], members[1]],
        "training_membership_digest": digest([members[0], members[1]]),
    }
    logical = {
        "code_fingerprint": "1" * 64,
        "dataset": dataset,
        "fallback": "lost",
        "features": list(FEATURES),
        "global_label_counts": {"lost": 1, "open": 1, "won": 0},
        "population": population,
        "recipe": "categorical_frequency_v1",
        "recipe_version": "1",
        "rules": [
            {
                "features": {key: None for key in FEATURES},
                "label_counts": {"lost": 1, "open": 1, "won": 0},
                "prediction": rule_prediction,
            }
        ],
        "seed": 7,
    }
    model_digest = digest(logical)
    model_id = f"model-{model_digest[:24]}"
    model = {
        "active": False,
        "logical": logical,
        "model_id": model_id,
        "schema_version": "intelligence-model-candidate-v1",
    }
    evaluation_logical = {
        "dataset": dataset,
        "evaluation_kind": "held_out",
        "labels": ["lost", "open", "won"] if evaluation_labels is None else evaluation_labels,
        "metrics": {
            "accuracy": {"reason": None, "value": 1.0},
            "accuracy_denominator": 1,
            "confusion": {
                f"{actual}->{predicted}": 1 if actual == predicted == "lost" else 0
                for actual in ("lost", "open", "won")
                for predicted in ("lost", "open", "won")
            },
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
        "population_digest": digest([members[2]]),
        "population_members": [members[2]],
    }
    evaluation_digest = digest(evaluation_logical)
    evaluation_id = f"evaluation-{evaluation_digest[:24]}"
    evaluation = {
        "evaluation_id": evaluation_id,
        "logical": evaluation_logical,
        "schema_version": "intelligence-model-evaluation-v1",
    }
    _write(root / "models" / model_id / "candidate.json", model)
    population_rows = (
        {
            "membership_hash": members[0],
            "partition": "training",
            "schema_version": POPULATION_SCHEMA,
        },
        {
            "membership_hash": members[1],
            "partition": "training",
            "schema_version": POPULATION_SCHEMA,
        },
        {
            "membership_hash": members[2],
            "partition": "held_out",
            "schema_version": POPULATION_SCHEMA,
        },
    )
    population_path = root / "models" / model_id / "population.ndjson"
    population_path.write_bytes(
        b"".join(canonical_json(row).encode("utf-8") + b"\n" for row in population_rows)
    )
    missingness_unsigned = {
        "activity_coverage": "legacy_partial_snapshot",
        "features": list(FEATURES),
        "provenance": ACTIVITY_PROVENANCE,
        "schema_version": MISSINGNESS_SCHEMA,
    }
    _write(
        root / "models" / model_id / "missingness.json",
        {**missingness_unsigned, "digest": digest(missingness_unsigned)},
    )
    _write(root / "evaluations" / evaluation_id / "evaluation.json", evaluation)
    model_paths = (
        f"models/{model_id}/candidate.json",
        f"models/{model_id}/population.ndjson",
        f"models/{model_id}/missingness.json",
        f"evaluations/{evaluation_id}/evaluation.json",
    )
    model_descriptor = descriptor_value(
        MODEL_DESCRIPTOR_SCHEMA,
        {
            "active": False,
            "code_fingerprint": "1" * 64,
            "command": "train_run",
            "dataset": dataset,
            "evaluation_id": evaluation_id,
            "evaluation_logical_digest": evaluation_digest,
            "inventory": _inventory(root, model_paths),
            "model_id": model_id,
            "model_logical_digest": model_digest,
            "recipe": "categorical_frequency_v1",
            "recipe_version": "1",
            "request_digest": digest(
                {
                    "code_fingerprint": "1" * 64,
                    "dataset": dataset,
                    "recipe": "categorical_frequency_v1",
                    "recipe_version": "1",
                    "seed": 7,
                }
            ),
            "run_id": run_id,
            "seed": 7,
        },
    )
    _write(root / "acceptance-descriptors" / "models" / f"{model_id}.json", model_descriptor)
    evaluation_descriptor = descriptor_value(
        EVALUATION_DESCRIPTOR_SCHEMA,
        {
            "active": False,
            "command": "train_run",
            "dataset": dataset,
            "evaluation_id": evaluation_id,
            "evaluation_kind": "held_out",
            "evaluation_logical_digest": evaluation_digest,
            "inventory": _inventory(root, (f"evaluations/{evaluation_id}/evaluation.json",)),
            "model_id": model_id,
            "model_logical_digest": model_digest,
            "model_run_id": run_id,
            "request_digest": digest(
                {
                    "dataset": dataset,
                    "evaluation_kind": "held_out",
                    "model_id": model_id,
                    "model_logical_digest": model_digest,
                    "model_run_id": run_id,
                }
            ),
            "run_id": run_id,
        },
    )
    _write(
        root / "acceptance-descriptors" / "evaluations" / f"{evaluation_id}.json",
        evaluation_descriptor,
    )
    return model_id, evaluation_id


def test_exact_canonical_train_bundle_is_accepted(tmp_path: Path) -> None:
    model_id, evaluation_id = _bundle(tmp_path)
    bundle = verify_train_bundle(tmp_path, "train-run")
    assert bundle.model_descriptor.model_id == model_id
    assert bundle.evaluation_descriptor.evaluation_id == evaluation_id


def test_train_bundle_rejects_evaluation_descriptor_model_run_mismatch(tmp_path: Path) -> None:
    model_id, evaluation_id = _bundle(tmp_path)
    descriptor_path = tmp_path / "acceptance-descriptors" / "evaluations" / f"{evaluation_id}.json"
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    descriptor["model_run_id"] = "different-model-run"
    descriptor["request_digest"] = digest(
        {
            "dataset": descriptor["dataset"],
            "evaluation_kind": descriptor["evaluation_kind"],
            "model_id": model_id,
            "model_logical_digest": descriptor["model_logical_digest"],
            "model_run_id": descriptor["model_run_id"],
        }
    )
    fields = dict(descriptor)
    fields.pop("descriptor_digest")
    schema_version = fields.pop("schema_version")
    _write(descriptor_path, descriptor_value(schema_version, fields))

    with pytest.raises(ValueError, match="evaluation provenance linkage is invalid"):
        verify_train_bundle(tmp_path, "train-run")


def test_train_bundle_rejects_self_consistent_invalid_model_rule_semantics(tmp_path: Path) -> None:
    _bundle(tmp_path, rule_prediction="won")
    with pytest.raises(ValueError, match="model rule prediction is invalid"):
        verify_train_bundle(tmp_path, "train-run")


def test_train_bundle_rejects_self_consistent_invalid_evaluation_metrics(tmp_path: Path) -> None:
    _bundle(tmp_path, evaluation_labels=["won", "open", "lost"])
    with pytest.raises(ValueError, match="evaluation semantics are invalid"):
        verify_train_bundle(tmp_path, "train-run")


@pytest.mark.parametrize(
    ("relative", "replacement"),
    [
        ("models/{model}/candidate.json", '{"active":false,"logical":NaN}'),
        ("models/{model}/candidate.json", '{"active":false,"logical":' + "[" * 40 + "]" * 40 + "}"),
        ("models/{model}/candidate.json", '{"active":false,"logical":{}} '),
        ("models/{model}/population.ndjson", '{"membership_hash":"a","partition":"training"}\n'),
    ],
)
def test_nonfinite_deep_noncanonical_and_bad_population_are_rejected(
    tmp_path: Path, relative: str, replacement: str
) -> None:
    model_id, _ = _bundle(tmp_path)
    target = tmp_path / relative.format(model=model_id)
    target.write_text(replacement, encoding="utf-8", newline="\n")
    with pytest.raises(ValueError):
        verify_train_bundle(tmp_path, "train-run")


def test_bundle_rejects_extra_symlink_hardlink_and_oversize(tmp_path: Path) -> None:
    model_id, _ = _bundle(tmp_path)
    extra = tmp_path / "models" / model_id / "extra.json"
    extra.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="missing or unexpected"):
        verify_train_bundle(tmp_path, "train-run")
    extra.unlink()
    source = tmp_path / "models" / model_id / "candidate.json"
    try:
        os.link(source, extra)
    except OSError:
        pytest.skip("hard links are unavailable on this filesystem")
    with pytest.raises(ValueError, match="unsafe"):
        verify_train_bundle(tmp_path, "train-run")


def test_bundle_rejects_descriptor_candidate_population_and_missingness_tamper(
    tmp_path: Path,
) -> None:
    model_id, evaluation_id = _bundle(tmp_path)
    descriptor = tmp_path / "acceptance-descriptors" / "models" / f"{model_id}.json"
    descriptor.write_text(
        descriptor.read_text(encoding="utf-8").replace('"active":false', '"active":true'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        verify_train_bundle(tmp_path, "train-run")
    _bundle(tmp_path / "fresh")
    root = tmp_path / "fresh"
    candidate = root / "models" / model_id / "candidate.json"
    candidate.write_text(
        canonical_json(
            {
                "active": False,
                "logical": {},
                "model_id": model_id,
                "schema_version": "intelligence-model-candidate-v1",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        verify_train_bundle(root, "train-run")
    root = tmp_path / "fresh2"
    model_id, evaluation_id = _bundle(root)
    missingness = root / "models" / model_id / "missingness.json"
    missingness.write_text(canonical_json({"schema_version": MISSINGNESS_SCHEMA}), encoding="utf-8")
    with pytest.raises(ValueError):
        verify_train_bundle(root, "train-run")
    evaluation = root / "evaluations" / evaluation_id / "evaluation.json"
    evaluation.write_text(canonical_json({"evaluation_id": evaluation_id}), encoding="utf-8")
    with pytest.raises(ValueError):
        verify_train_bundle(root, "train-run")


def test_comparison_descriptor_binds_request_and_exact_inventory(tmp_path: Path) -> None:
    from intelligence.model_workflows.artifacts import (
        COMPARISON_DESCRIPTOR_SCHEMA,
        verify_comparison_bundle,
    )

    logical = {
        "left_evaluation_id": "evaluation-left",
        "left_metrics": {"accuracy": 1},
        "metrics_contract": "confusion-accuracy-precision-recall-v1",
        "population_digest": "4" * 64,
        "right_evaluation_id": "evaluation-right",
        "right_metrics": {"accuracy": 0},
    }
    comparison_id = f"comparison-{digest(logical)[:24]}"
    comparison = {
        "comparison_id": comparison_id,
        "logical": logical,
        "schema_version": "intelligence-model-comparison-v1",
    }
    _write(tmp_path / "comparisons" / comparison_id / "comparison.json", comparison)
    request = {
        "left_evaluation_id": "evaluation-left",
        "left_run_id": "left-run",
        "right_evaluation_id": "evaluation-right",
        "right_run_id": "right-run",
    }
    descriptor = descriptor_value(
        COMPARISON_DESCRIPTOR_SCHEMA,
        {
            "command": "evaluate_compare",
            "comparison_id": comparison_id,
            "comparison_logical_digest": digest(logical),
            "inventory": _inventory(tmp_path, (f"comparisons/{comparison_id}/comparison.json",)),
            "left_evaluation_id": "evaluation-left",
            "left_run_id": "left-run",
            "request_digest": digest(request),
            "right_evaluation_id": "evaluation-right",
            "right_run_id": "right-run",
            "run_id": "comparison-run",
        },
    )
    _write(
        tmp_path / "acceptance-descriptors" / "comparisons" / f"{comparison_id}.json",
        descriptor,
    )
    assert verify_comparison_bundle(tmp_path, "comparison-run")[1].comparison_id == comparison_id
    descriptor["right_run_id"] = "tampered"
    _write(
        tmp_path / "other.json",
        descriptor,
    )
    with pytest.raises(ValueError):
        from intelligence.model_workflows.artifacts import parse_comparison_descriptor

        parse_comparison_descriptor(descriptor, "comparison-run")
