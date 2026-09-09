"""Artifact-only admission, deterministic rules, and safe model bundle verification."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, cast

from intelligence.artifacts import canonical_json
from intelligence.model_workflows.artifacts import (
    EVALUATION_DESCRIPTOR_SCHEMA,
    MODEL_DESCRIPTOR_SCHEMA,
    POPULATION_SCHEMA,
    descriptor_value,
    verify_evaluation_bundle,
    verify_train_bundle,
)
from intelligence.model_workflows.codec import JsonValue
from intelligence.model_workflows.contracts import (
    EVALUATION_SCHEMA,
    FEATURES,
    MODEL_SCHEMA,
    RECIPE,
    RECIPE_VERSION,
    EvaluationRequest,
    TrainRequest,
    code_fingerprint,
    digest,
)
from intelligence.model_workflows.dataset_admission import AdmittedDataset, admit_dataset
from intelligence.model_workflows.population import membership_hash as _membership_hash
from intelligence.model_workflows.population import split as _split_population
from intelligence.model_workflows.publication_io import inventory as _inventory
from intelligence.model_workflows.publication_io import missingness as _missingness
from intelligence.model_workflows.publication_io import write_json as _write
from intelligence.model_workflows.publication_io import write_ndjson as _write_ndjson

Label = Literal["lost", "open", "won"]
_LABELS: tuple[Label, ...] = ("lost", "open", "won")


def population(
    dataset: AdmittedDataset,
) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...], dict[str, int]]:
    """Create a seeded group split; Persons stay transient and never become artifacts."""
    return _split_population(dataset.rows, dataset.request.seed)


def train_bundle(dataset: AdmittedDataset) -> tuple[dict[str, object], dict[str, object]]:
    """Build a dependency-free categorical frequency candidate and mandatory held-out evaluation."""
    training, held_out, exclusions = population(dataset)
    counts: dict[str, dict[Label, int]] = {}
    global_counts: dict[Label, int] = {label: 0 for label in _LABELS}
    for row in training:
        label = _label(row)
        global_counts[label] += 1
        key = _feature_key(row)
        bucket = counts.setdefault(key, {item: 0 for item in _LABELS})
        bucket[label] += 1
    fallback = _winner(global_counts)
    rules = [
        {
            "features": json.loads(key),
            "label_counts": counts[key],
            "prediction": _winner(counts[key]),
        }
        for key in sorted(counts)
    ]
    population_record = {
        "exclusions": exclusions,
        "held_out_members": [_membership_hash(row) for row in held_out],
        "held_out_membership_digest": digest([_membership_hash(row) for row in held_out]),
        "split_contract": "person-group-sha256-mod5-v1",
        "training_members": [_membership_hash(row) for row in training],
        "training_membership_digest": digest([_membership_hash(row) for row in training]),
    }
    logical = {
        "code_fingerprint": code_fingerprint(),
        "dataset": dataset.pin(),
        "fallback": fallback,
        "features": list(FEATURES),
        "global_label_counts": global_counts,
        "population": population_record,
        "recipe": RECIPE,
        "recipe_version": RECIPE_VERSION,
        "rules": rules,
        "seed": dataset.request.seed,
    }
    model_id = f"model-{digest(logical)[:24]}"
    model = {
        "active": False,
        "logical": logical,
        "model_id": model_id,
        "schema_version": MODEL_SCHEMA,
    }
    evaluation = evaluate_model(model, held_out, dataset.pin(), "held_out")
    return model, evaluation


def evaluate_model(
    model: Mapping[str, object],
    rows: tuple[dict[str, object], ...],
    dataset_pin: dict[str, object],
    kind: str,
) -> dict[str, object]:
    """Evaluate with fixed class ordering and explicit undefined metric denominators."""
    logical = _mapping(model.get("logical"), "model logical")
    predictions = {_feature_key(rule["features"]): rule["prediction"] for rule in _rules(logical)}
    fallback = _text(logical.get("fallback"), "fallback")
    confusion: dict[str, int] = {
        f"{actual}->{predicted}": 0 for actual in _LABELS for predicted in _LABELS
    }
    for row in rows:
        actual = _label(row)
        predicted = predictions.get(_feature_key(row), fallback)
        if predicted not in _LABELS:
            raise ValueError("model prediction is invalid")
        confusion[f"{actual}->{predicted}"] += 1
    total = len(rows)
    correct = sum(confusion[f"{label}->{label}"] for label in _LABELS)
    per_class: dict[str, dict[str, object]] = {}
    for label in _LABELS:
        true_positive = confusion[f"{label}->{label}"]
        predicted_total = sum(confusion[f"{actual}->{label}"] for actual in _LABELS)
        actual_total = sum(confusion[f"{label}->{predicted}"] for predicted in _LABELS)
        per_class[label] = {
            "precision": _ratio(true_positive, predicted_total),
            "precision_denominator": predicted_total,
            "recall": _ratio(true_positive, actual_total),
            "recall_denominator": actual_total,
        }
    logical_evaluation = {
        "dataset": dataset_pin,
        "evaluation_kind": kind,
        "labels": list(_LABELS),
        "metrics_contract": "confusion-accuracy-precision-recall-v1",
        "model_id": model.get("model_id"),
        "population_members": [_membership_hash(row) for row in rows],
        "population_digest": digest([_membership_hash(row) for row in rows]),
        "metrics": {
            "accuracy": _ratio(correct, total),
            "accuracy_denominator": total,
            "confusion": confusion,
            "per_class": per_class,
        },
    }
    evaluation_id = f"evaluation-{digest(logical_evaluation)[:24]}"
    return {
        "evaluation_id": evaluation_id,
        "logical": logical_evaluation,
        "schema_version": EVALUATION_SCHEMA,
    }


def write_train(staging: Path, dataset: AdmittedDataset) -> tuple[str, str]:
    """Write a complete canonical candidate/evaluation/descriptor bundle to staging only."""
    model, evaluation = train_bundle(dataset)
    model_id = _text(model.get("model_id"), "model id")
    evaluation_id = _text(evaluation.get("evaluation_id"), "evaluation id")
    model_path = staging / "models" / model_id / "candidate.json"
    evaluation_path = staging / "evaluations" / evaluation_id / "evaluation.json"
    _write(model_path, model)
    _write(evaluation_path, evaluation)
    population = _mapping(_mapping(model["logical"], "logical").get("population"), "population")
    member_values: list[dict[str, object]] = []
    for item in _strings(population.get("training_members")):
        member_values.append(
            {"membership_hash": item, "partition": "training", "schema_version": POPULATION_SCHEMA}
        )
    for item in _strings(population.get("held_out_members")):
        member_values.append(
            {"membership_hash": item, "partition": "held_out", "schema_version": POPULATION_SCHEMA}
        )
    members = tuple(member_values)
    _write_ndjson(
        staging / "models" / model_id / "population.ndjson",
        members,
    )
    _write(
        staging / "models" / model_id / "missingness.json",
        _missingness(),
    )
    evaluation_descriptor = descriptor_value(
        EVALUATION_DESCRIPTOR_SCHEMA,
        {
            "active": False,
            "command": "train_run",
            "dataset": cast(dict[str, JsonValue], dataset.pin()),
            "evaluation_id": evaluation_id,
            "evaluation_kind": "held_out",
            "evaluation_logical_digest": digest(evaluation["logical"]),
            "inventory": [
                _inventory(evaluation_path, f"evaluations/{evaluation_id}/evaluation.json")
            ],
            "model_id": model_id,
            "model_logical_digest": digest(model["logical"]),
            "model_run_id": staging.name,
            "request_digest": digest(
                {
                    "dataset": cast(dict[str, JsonValue], dataset.pin()),
                    "evaluation_kind": "held_out",
                    "model_id": model_id,
                    "model_logical_digest": digest(model["logical"]),
                    "model_run_id": staging.name,
                }
            ),
            "run_id": staging.name,
        },
    )
    _write(
        staging / "acceptance-descriptors" / "evaluations" / f"{evaluation_id}.json",
        evaluation_descriptor,
    )
    descriptor = descriptor_value(
        MODEL_DESCRIPTOR_SCHEMA,
        {
            "active": False,
            "code_fingerprint": code_fingerprint(),
            "command": "train_run",
            "dataset": cast(dict[str, JsonValue], dataset.pin()),
            "evaluation_id": evaluation_id,
            "evaluation_logical_digest": digest(evaluation["logical"]),
            "model_id": model_id,
            "model_logical_digest": digest(model["logical"]),
            "inventory": [
                _inventory(model_path, f"models/{model_id}/candidate.json"),
                _inventory(
                    staging / "models" / model_id / "population.ndjson",
                    f"models/{model_id}/population.ndjson",
                ),
                _inventory(
                    staging / "models" / model_id / "missingness.json",
                    f"models/{model_id}/missingness.json",
                ),
                _inventory(evaluation_path, f"evaluations/{evaluation_id}/evaluation.json"),
            ],
            "recipe": RECIPE,
            "recipe_version": RECIPE_VERSION,
            "request_digest": digest(
                {
                    "code_fingerprint": code_fingerprint(),
                    "dataset": cast(dict[str, JsonValue], dataset.pin()),
                    "recipe": RECIPE,
                    "recipe_version": RECIPE_VERSION,
                    "seed": dataset.request.seed,
                }
            ),
            "run_id": staging.name,
            "seed": dataset.request.seed,
        },
    )
    _write(staging / "acceptance-descriptors" / "models" / f"{model_id}.json", descriptor)
    verify_staged_train(staging, model_id, evaluation_id)
    return model_id, evaluation_id


def load_model(workspace: Path, model_id: str, model_run_id: str) -> dict[str, object]:
    """Load only a State-accepted inactive canonical candidate without trusting paths."""
    from intelligence.model_workflows.catalog import find as find_model

    return find_model(workspace, model_id, model_run_id).candidate


def write_evaluation(staging: Path, request: EvaluationRequest, model: Mapping[str, object]) -> str:
    """Write an independent repeat evaluation over the candidate's held-out population only."""
    dataset = admit_dataset(
        staging.parent.parent,
        TrainRequest(request.dataset_id, request.accepted_run_id, RECIPE, _seed(model)),
    )
    held = set(
        _strings(
            _mapping(_mapping(model["logical"], "logical")["population"], "population").get(
                "held_out_members"
            )
        )
    )
    training = set(
        _strings(
            _mapping(_mapping(model["logical"], "logical")["population"], "population").get(
                "training_members"
            )
        )
    )
    if held & training:
        raise ValueError("model training and held-out populations overlap")
    rows = tuple(row for row in dataset.rows if _membership_hash(row) in held)
    if not rows or any(_membership_hash(row) in training for row in rows):
        raise ValueError("evaluation population is incompatible")
    evaluation = evaluate_model(model, rows, dataset.pin(), "independent_held_out_replay")
    evaluation_id = _text(evaluation.get("evaluation_id"), "evaluation id")
    _write(staging / "evaluations" / evaluation_id / "evaluation.json", evaluation)
    path = staging / "evaluations" / evaluation_id / "evaluation.json"
    descriptor = descriptor_value(
        EVALUATION_DESCRIPTOR_SCHEMA,
        {
            "active": False,
            "command": "evaluate_run",
            "dataset": cast(dict[str, JsonValue], dataset.pin()),
            "evaluation_id": evaluation_id,
            "evaluation_kind": "independent_held_out_replay",
            "evaluation_logical_digest": digest(evaluation["logical"]),
            "inventory": [_inventory(path, f"evaluations/{evaluation_id}/evaluation.json")],
            "model_id": _text(model.get("model_id"), "model id"),
            "model_logical_digest": digest(_mapping(model.get("logical"), "logical")),
            "model_run_id": request.model_run_id,
            "request_digest": digest(
                {
                    "dataset": cast(dict[str, JsonValue], dataset.pin()),
                    "evaluation_kind": "independent_held_out_replay",
                    "model_id": _text(model.get("model_id"), "model id"),
                    "model_logical_digest": digest(_mapping(model.get("logical"), "logical")),
                    "model_run_id": request.model_run_id,
                }
            ),
            "run_id": staging.name,
        },
    )
    _write(staging / "acceptance-descriptors" / "evaluations" / f"{evaluation_id}.json", descriptor)
    verify_evaluation_bundle(staging, staging.name)
    return evaluation_id


def verify_staged_train(staging: Path, model_id: str, evaluation_id: str) -> None:
    """Reject extra, malformed, noncanonical, or active staged model evidence."""
    del model_id, evaluation_id
    verify_train_bundle(staging, staging.name)


def _feature_key(value: object) -> str:
    if isinstance(value, dict) and set(value) == set(FEATURES):
        return canonical_json({feature: value[feature] for feature in FEATURES})
    if not isinstance(value, dict):
        raise ValueError("dataset row is invalid")
    return canonical_json({feature: value.get(feature) for feature in FEATURES})


def _winner(counts: dict[Label, int]) -> Label:
    return max(_LABELS, key=lambda label: (counts[label], -_LABELS.index(label)))


def _ratio(numerator: int, denominator: int) -> dict[str, object]:
    return (
        {"reason": None, "value": numerator / denominator if denominator else None}
        if denominator
        else {"reason": "zero_denominator", "value": None}
    )


def _row_id(row: dict[str, object]) -> str:
    value = row.get("source_entity_id")
    return _text(value, "source entity")


def _label(row: dict[str, object]) -> Label:
    value = row.get("label")
    if value not in _LABELS:
        raise ValueError("dataset label is invalid")
    return value


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} is invalid")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is invalid")
    return value


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("membership is invalid")
    return tuple(value)


def _rules(logical: dict[str, object]) -> tuple[dict[str, object], ...]:
    value = logical.get("rules")
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("model rules are invalid")
    return tuple(value)


def _seed(model: Mapping[str, object]) -> int:
    value = _mapping(model.get("logical"), "logical").get("seed")
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("model seed is invalid")
    return value
