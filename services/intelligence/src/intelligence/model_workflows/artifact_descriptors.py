"""Strict parsing primitives for model workflow acceptance descriptors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from intelligence.model_workflows.codec import JsonValue
from intelligence.model_workflows.contracts import (
    ACTIVITY_PROVENANCE,
    RECIPE,
    RECIPE_VERSION,
    digest,
    safe_id,
)
from intelligence.models import OutputInventory

MODEL_DESCRIPTOR_SCHEMA = "intelligence-model-acceptance-v2"
EVALUATION_DESCRIPTOR_SCHEMA = "intelligence-evaluation-acceptance-v1"
COMPARISON_DESCRIPTOR_SCHEMA = "intelligence-comparison-acceptance-v1"
MISSINGNESS_SCHEMA = "intelligence-model-missingness-v1"
POPULATION_SCHEMA = "intelligence-model-population-v1"
MAX_MODEL_ROWS = 20_000

ArtifactKind = Literal["model", "evaluation", "comparison"]


@dataclass(frozen=True)
class ModelAcceptanceDescriptor:
    """The exact State-registerable descriptor for a candidate and held-out evaluation."""

    run_id: str
    model_id: str
    evaluation_id: str
    request_digest: str
    model_logical_digest: str
    evaluation_logical_digest: str
    inventory: tuple[OutputInventory, ...]
    raw: dict[str, JsonValue]


@dataclass(frozen=True)
class EvaluationAcceptanceDescriptor:
    """The exact State-registerable descriptor for one independent evaluation."""

    run_id: str
    evaluation_id: str
    model_id: str
    model_run_id: str
    request_digest: str
    evaluation_logical_digest: str
    inventory: tuple[OutputInventory, ...]
    raw: dict[str, JsonValue]


@dataclass(frozen=True)
class ComparisonAcceptanceDescriptor:
    """The exact State-registerable descriptor for one evaluation comparison."""

    run_id: str
    comparison_id: str
    request_digest: str
    comparison_logical_digest: str
    inventory: tuple[OutputInventory, ...]
    raw: dict[str, JsonValue]


@dataclass(frozen=True)
class TrainBundle:
    """A complete inactive training publication after staged or State verification."""

    model: dict[str, JsonValue]
    evaluation: dict[str, JsonValue]
    population: tuple[dict[str, JsonValue], ...]
    missingness: dict[str, JsonValue]
    model_descriptor: ModelAcceptanceDescriptor
    evaluation_descriptor: EvaluationAcceptanceDescriptor


def parse_model_descriptor(
    value: dict[str, JsonValue], expected_run_id: str
) -> ModelAcceptanceDescriptor:
    """Parse a strict self-digesting train acceptance descriptor."""
    expected = {
        "active",
        "code_fingerprint",
        "command",
        "dataset",
        "descriptor_digest",
        "evaluation_id",
        "evaluation_logical_digest",
        "inventory",
        "model_id",
        "model_logical_digest",
        "recipe",
        "recipe_version",
        "request_digest",
        "run_id",
        "schema_version",
        "seed",
    }
    _descriptor_shape(value, expected, MODEL_DESCRIPTOR_SCHEMA, "train_run", expected_run_id)
    if (
        value["active"] is not False
        or value["recipe"] != RECIPE
        or value["recipe_version"] != RECIPE_VERSION
    ):
        raise ValueError("model descriptor policy is invalid")
    model_id = _identifier(value["model_id"], "model id")
    evaluation_id = _identifier(value["evaluation_id"], "evaluation id")
    _dataset_pin(value["dataset"])
    code_fingerprint = _digest(value["code_fingerprint"], "model code fingerprint")
    seed = value["seed"]
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError("model descriptor seed is invalid")
    request = {
        "code_fingerprint": code_fingerprint,
        "dataset": value["dataset"],
        "recipe": value["recipe"],
        "recipe_version": value["recipe_version"],
        "seed": seed,
    }
    if value["request_digest"] != digest(request):
        raise ValueError("model descriptor request linkage is invalid")
    return ModelAcceptanceDescriptor(
        expected_run_id,
        model_id,
        evaluation_id,
        _digest(value["request_digest"], "model request digest"),
        _digest(value["model_logical_digest"], "model logical digest"),
        _digest(value["evaluation_logical_digest"], "evaluation logical digest"),
        _inventory(value["inventory"], "model descriptor inventory"),
        value,
    )


def parse_evaluation_descriptor(
    value: dict[str, JsonValue], expected_run_id: str
) -> EvaluationAcceptanceDescriptor:
    """Parse a strict self-digesting evaluation acceptance descriptor."""
    expected = {
        "active",
        "command",
        "dataset",
        "descriptor_digest",
        "evaluation_id",
        "evaluation_kind",
        "evaluation_logical_digest",
        "inventory",
        "model_id",
        "model_logical_digest",
        "model_run_id",
        "request_digest",
        "run_id",
        "schema_version",
    }
    _descriptor_shape(value, expected, EVALUATION_DESCRIPTOR_SCHEMA, None, expected_run_id)
    command = value["command"]
    if command not in {"train_run", "evaluate_run"} or value["active"] is not False:
        raise ValueError("evaluation descriptor policy is invalid")
    _dataset_pin(value["dataset"])
    kind = value["evaluation_kind"]
    if kind not in {"held_out", "independent_held_out_replay"}:
        raise ValueError("evaluation descriptor kind is invalid")
    model_id = _identifier(value["model_id"], "model id")
    model_run_id = _identifier(value["model_run_id"], "model run")
    model_logical_digest = _digest(value["model_logical_digest"], "model logical digest")
    request = {
        "dataset": value["dataset"],
        "evaluation_kind": kind,
        "model_id": model_id,
        "model_logical_digest": model_logical_digest,
        "model_run_id": model_run_id,
    }
    if value["request_digest"] != digest(request):
        raise ValueError("evaluation descriptor request linkage is invalid")
    return EvaluationAcceptanceDescriptor(
        expected_run_id,
        _identifier(value["evaluation_id"], "evaluation id"),
        model_id,
        model_run_id,
        _digest(value["request_digest"], "evaluation request digest"),
        _digest(value["evaluation_logical_digest"], "evaluation logical digest"),
        _inventory(value["inventory"], "evaluation descriptor inventory"),
        value,
    )


def parse_comparison_descriptor(
    value: dict[str, JsonValue], expected_run_id: str
) -> ComparisonAcceptanceDescriptor:
    """Parse a strict self-digesting comparison acceptance descriptor."""
    expected = {
        "command",
        "comparison_id",
        "comparison_logical_digest",
        "descriptor_digest",
        "inventory",
        "left_evaluation_id",
        "left_run_id",
        "request_digest",
        "right_evaluation_id",
        "right_run_id",
        "run_id",
        "schema_version",
    }
    _descriptor_shape(
        value, expected, COMPARISON_DESCRIPTOR_SCHEMA, "evaluate_compare", expected_run_id
    )
    left_run_id = _identifier(value["left_run_id"], "left run")
    right_run_id = _identifier(value["right_run_id"], "right run")
    left_evaluation_id = _identifier(value["left_evaluation_id"], "left evaluation")
    right_evaluation_id = _identifier(value["right_evaluation_id"], "right evaluation")
    request = {
        "left_evaluation_id": left_evaluation_id,
        "left_run_id": left_run_id,
        "right_evaluation_id": right_evaluation_id,
        "right_run_id": right_run_id,
    }
    if value["request_digest"] != digest(request):
        raise ValueError("comparison descriptor request linkage is invalid")
    return ComparisonAcceptanceDescriptor(
        expected_run_id,
        _identifier(value["comparison_id"], "comparison id"),
        _digest(value["request_digest"], "comparison request digest"),
        _digest(value["comparison_logical_digest"], "comparison logical digest"),
        _inventory(value["inventory"], "comparison descriptor inventory"),
        value,
    )


def _descriptor_shape(
    value: dict[str, JsonValue],
    expected: set[str],
    schema: str,
    command: str | None,
    expected_run_id: str,
) -> None:
    if set(value) != expected or value.get("schema_version") != schema:
        raise ValueError("acceptance descriptor schema is invalid")
    if value.get("run_id") != expected_run_id or (
        command is not None and value.get("command") != command
    ):
        raise ValueError("acceptance descriptor run linkage is invalid")
    unsigned = dict(value)
    descriptor_digest = unsigned.pop("descriptor_digest")
    if _digest(descriptor_digest, "descriptor digest") != digest(unsigned):
        raise ValueError("acceptance descriptor digest is invalid")


def _inventory(value: JsonValue, label: str) -> tuple[OutputInventory, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} is invalid")
    parsed: list[OutputInventory] = []
    for item in value:
        mapping = _mapping(item, label)
        if set(mapping) != {"byte_count", "relative_path", "sha256"}:
            raise ValueError(f"{label} is invalid")
        path = mapping["relative_path"]
        count = mapping["byte_count"]
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
        ):
            raise ValueError(f"{label} is invalid")
        parsed.append(OutputInventory(path, _digest(mapping["sha256"], label), count))
    result = tuple(parsed)
    if len({item.relative_path for item in result}) != len(result):
        raise ValueError(f"{label} has duplicate entries")
    return result


def _population_logical(value: dict[str, JsonValue]) -> tuple[tuple[str, str], ...]:
    expected = {
        "exclusions",
        "held_out_members",
        "held_out_membership_digest",
        "split_contract",
        "training_members",
        "training_membership_digest",
    }
    if set(value) != expected or value["split_contract"] != "person-group-sha256-mod5-v1":
        raise ValueError("model population logical content is invalid")
    training = _digest_list(value["training_members"], "training membership")
    held_out = _digest_list(value["held_out_members"], "held-out membership")
    if (
        set(training) & set(held_out)
        or digest(training) != value["training_membership_digest"]
        or digest(held_out) != value["held_out_membership_digest"]
        or not training
        or not held_out
    ):
        raise ValueError("model population membership is invalid")
    if not isinstance(value["exclusions"], dict) or any(
        not isinstance(count, int) or isinstance(count, bool) or count < 0
        for count in value["exclusions"].values()
    ):
        raise ValueError("model population exclusions are invalid")
    return tuple(("training", item) for item in training) + tuple(
        ("held_out", item) for item in held_out
    )


def _dataset_pin(value: JsonValue) -> None:
    mapping = _mapping(value, "dataset pin")
    expected = {
        "accepted_run_id",
        "config_digest",
        "content_digest",
        "dataset_id",
        "manifest_digest",
        "provenance",
    }
    if set(mapping) != expected or mapping["provenance"] != ACTIVITY_PROVENANCE:
        raise ValueError("dataset pin is invalid")
    _identifier(mapping["accepted_run_id"], "accepted dataset run")
    _identifier(mapping["dataset_id"], "dataset id")
    for key in ("config_digest", "content_digest", "manifest_digest"):
        _digest(mapping[key], key)


def _identifier(value: JsonValue, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} is invalid")
    return safe_id(value, field)


def _digest(value: JsonValue, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{field} is invalid")
    return value


def _digest_list(value: JsonValue, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field} is invalid")
    result = tuple(_digest(item, field) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{field} has duplicate members")
    return result


def _mapping(value: JsonValue, field: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} is invalid")
    return value
