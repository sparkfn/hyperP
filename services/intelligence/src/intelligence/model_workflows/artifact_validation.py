"""Content and inventory verification for immutable model workflow artifacts."""

from __future__ import annotations

import math
from pathlib import Path

from intelligence.datasets.bounds import MAX_ARTIFACT_FILE_BYTES, ReadBudget, digest_file
from intelligence.model_workflows.artifact_descriptors import (
    MISSINGNESS_SCHEMA,
    POPULATION_SCHEMA,
    ComparisonAcceptanceDescriptor,
    EvaluationAcceptanceDescriptor,
    ModelAcceptanceDescriptor,
    _digest,
    _digest_list,
    _mapping,
)
from intelligence.model_workflows.codec import JsonValue
from intelligence.model_workflows.contracts import (
    ACTIVITY_PROVENANCE,
    COMPARISON_SCHEMA,
    EVALUATION_SCHEMA,
    FEATURES,
    MODEL_SCHEMA,
    RECIPE,
    RECIPE_VERSION,
    digest,
)
from intelligence.model_workflows.path_safety import regular_file
from intelligence.models import OutputInventory


def _verify_model(descriptor: ModelAcceptanceDescriptor, candidate: dict[str, JsonValue]) -> None:
    if set(candidate) != {"active", "logical", "model_id", "schema_version"}:
        raise ValueError("model candidate schema is invalid")
    if candidate["schema_version"] != MODEL_SCHEMA or candidate["active"] is not False:
        raise ValueError("model candidate policy is invalid")
    if candidate["model_id"] != descriptor.model_id:
        raise ValueError("model candidate identity is invalid")
    logical = _mapping(candidate["logical"], "model logical")
    expected = {
        "code_fingerprint",
        "dataset",
        "fallback",
        "features",
        "global_label_counts",
        "population",
        "recipe",
        "recipe_version",
        "rules",
        "seed",
    }
    if set(logical) != expected or digest(logical) != descriptor.model_logical_digest:
        raise ValueError("model candidate logical content is invalid")
    if descriptor.model_id != f"model-{descriptor.model_logical_digest[:24]}":
        raise ValueError("model candidate identifier is noncanonical")
    if (
        logical["recipe"] != RECIPE
        or logical["recipe_version"] != RECIPE_VERSION
        or logical["features"] != list(FEATURES)
        or logical["dataset"] != descriptor.raw["dataset"]
        or logical["seed"] != descriptor.raw["seed"]
        or logical["code_fingerprint"] != descriptor.raw["code_fingerprint"]
    ):
        raise ValueError("model candidate provenance linkage is invalid")
    _digest(logical["code_fingerprint"], "model code fingerprint")
    population = _population_logical(_mapping(logical["population"], "model population"))
    _model_semantics(logical, population)


def _verify_evaluation(
    descriptor: EvaluationAcceptanceDescriptor,
    evaluation: dict[str, JsonValue],
    model_id: str,
    model_run_id: str,
) -> None:
    if set(evaluation) != {"evaluation_id", "logical", "schema_version"}:
        raise ValueError("evaluation schema is invalid")
    if (
        evaluation["schema_version"] != EVALUATION_SCHEMA
        or evaluation["evaluation_id"] != descriptor.evaluation_id
    ):
        raise ValueError("evaluation identity is invalid")
    logical = _mapping(evaluation["logical"], "evaluation logical")
    expected = {
        "dataset",
        "evaluation_kind",
        "labels",
        "metrics",
        "metrics_contract",
        "model_id",
        "population_digest",
        "population_members",
    }
    if set(logical) != expected or digest(logical) != descriptor.evaluation_logical_digest:
        raise ValueError("evaluation logical content is invalid")
    if (
        logical["dataset"] != descriptor.raw["dataset"]
        or logical["evaluation_kind"] != descriptor.raw["evaluation_kind"]
        or logical["model_id"] != model_id
        or descriptor.model_id != model_id
        or not isinstance(model_run_id, str)
        or descriptor.model_run_id != model_run_id
    ):
        raise ValueError("evaluation provenance linkage is invalid")
    if descriptor.evaluation_id != f"evaluation-{descriptor.evaluation_logical_digest[:24]}":
        raise ValueError("evaluation identifier is noncanonical")
    members = _digest_list(logical["population_members"], "evaluation population")
    if digest(members) != logical["population_digest"]:
        raise ValueError("evaluation population digest is invalid")
    _evaluation_semantics(logical, members)


_LABELS = ("lost", "open", "won")


def _model_semantics(
    logical: dict[str, JsonValue], population: tuple[tuple[str, str], ...]
) -> None:
    seed = logical["seed"]
    fallback = logical["fallback"]
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0 or fallback not in _LABELS:
        raise ValueError("model recipe semantics are invalid")
    counts = _label_counts(logical["global_label_counts"], "global label counts")
    training_count = sum(1 for partition, _member in population if partition == "training")
    if sum(counts.values()) != training_count:
        raise ValueError("model global counts do not match training population")
    rules = logical["rules"]
    if not isinstance(rules, list):
        raise ValueError("model rules are invalid")
    aggregate = {label: 0 for label in _LABELS}
    seen: set[tuple[str | None, ...]] = set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"features", "label_counts", "prediction"}:
            raise ValueError("model rule schema is invalid")
        features = rule["features"]
        if not isinstance(features, dict) or set(features) != set(FEATURES):
            raise ValueError("model rule features are invalid")
        feature_key: list[str | None] = []
        for name in FEATURES:
            value = features[name]
            if value is not None and (not isinstance(value, str) or not value or len(value) > 1024):
                raise ValueError("model rule features are invalid")
            feature_key.append(value)
        key = tuple(feature_key)
        if key in seen:
            raise ValueError("model rules duplicate feature combinations")
        seen.add(key)
        rule_counts = _label_counts(rule["label_counts"], "model rule counts")
        if sum(rule_counts.values()) < 1 or rule["prediction"] != _winner(rule_counts):
            raise ValueError("model rule prediction is invalid")
        for label in _LABELS:
            aggregate[label] += rule_counts[label]
    if aggregate != counts:
        raise ValueError("model rule counts do not match global counts")


def _evaluation_semantics(logical: dict[str, JsonValue], members: tuple[str, ...]) -> None:
    if (
        logical["labels"] != list(_LABELS)
        or logical["metrics_contract"] != "confusion-accuracy-precision-recall-v1"
    ):
        raise ValueError("evaluation semantics are invalid")
    if not members or len(members) != len(set(members)):
        raise ValueError("evaluation population is invalid")
    metrics = logical["metrics"]
    if not isinstance(metrics, dict) or set(metrics) != {
        "accuracy",
        "accuracy_denominator",
        "confusion",
        "per_class",
    }:
        raise ValueError("evaluation metrics schema is invalid")
    confusion = metrics["confusion"]
    expected = {f"{actual}->{predicted}" for actual in _LABELS for predicted in _LABELS}
    if not isinstance(confusion, dict) or set(confusion) != expected:
        raise ValueError("evaluation confusion is invalid")
    confusion_counts = {key: _count(confusion[key], "evaluation confusion") for key in expected}
    total = sum(confusion_counts.values())
    if metrics["accuracy_denominator"] != total or total != len(members):
        raise ValueError("evaluation accuracy denominator is invalid")
    correct = sum(confusion_counts[f"{label}->{label}"] for label in _LABELS)
    _ratio_value(metrics["accuracy"], correct, total)
    per_class = metrics["per_class"]
    if not isinstance(per_class, dict) or set(per_class) != set(_LABELS):
        raise ValueError("evaluation per-class metrics are invalid")
    for label in _LABELS:
        value = per_class[label]
        if not isinstance(value, dict) or set(value) != {
            "precision",
            "precision_denominator",
            "recall",
            "recall_denominator",
        }:
            raise ValueError("evaluation per-class metrics are invalid")
        precision_denominator = sum(confusion_counts[f"{actual}->{label}"] for actual in _LABELS)
        recall_denominator = sum(confusion_counts[f"{label}->{predicted}"] for predicted in _LABELS)
        if (
            value["precision_denominator"] != precision_denominator
            or value["recall_denominator"] != recall_denominator
        ):
            raise ValueError("evaluation per-class denominators are invalid")
        true_positive = confusion_counts[f"{label}->{label}"]
        _ratio_value(value["precision"], true_positive, precision_denominator)
        _ratio_value(value["recall"], true_positive, recall_denominator)


def _label_counts(value: JsonValue, field: str) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != set(_LABELS):
        raise ValueError(f"{field} are invalid")
    result: dict[str, int] = {}
    for label in _LABELS:
        item = value[label]
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise ValueError(f"{field} are invalid")
        result[label] = item
    return result


def _count(value: JsonValue, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field} is invalid")
    return value


def _winner(counts: dict[str, int]) -> str:
    return max(_LABELS, key=lambda label: (counts[label], -_LABELS.index(label)))


def _ratio_value(value: JsonValue, numerator: int, denominator: int) -> None:
    if not isinstance(value, dict) or set(value) != {"reason", "value"}:
        raise ValueError("evaluation ratio is invalid")
    if denominator == 0:
        if value != {"reason": "zero_denominator", "value": None}:
            raise ValueError("evaluation ratio is invalid")
        return
    actual = value.get("value")
    if (
        value.get("reason") is not None
        or not isinstance(actual, (int, float))
        or isinstance(actual, bool)
        or not math.isfinite(float(actual))
        or actual != numerator / denominator
    ):
        raise ValueError("evaluation ratio is invalid")


def _verify_population(
    candidate: dict[str, JsonValue], population: tuple[dict[str, JsonValue], ...]
) -> None:
    logical = _mapping(candidate["logical"], "model logical")
    expected = _population_logical(_mapping(logical["population"], "model population"))
    rows: list[tuple[str, str]] = []
    for row in population:
        if set(row) != {"membership_hash", "partition", "schema_version"}:
            raise ValueError("population row schema is invalid")
        member = _digest(row["membership_hash"], "population membership")
        partition = row["partition"]
        if partition not in {"training", "held_out"} or row["schema_version"] != POPULATION_SCHEMA:
            raise ValueError("population row policy is invalid")
        rows.append((partition, member))
    if tuple(rows) != expected:
        raise ValueError("population rows differ from candidate logical content")


def _verify_missingness(candidate: dict[str, JsonValue], missingness: dict[str, JsonValue]) -> None:
    expected = {
        "activity_coverage",
        "digest",
        "features",
        "provenance",
        "schema_version",
        "summaries",
    }
    if set(missingness) != expected or missingness["schema_version"] != MISSINGNESS_SCHEMA:
        raise ValueError("missingness artifact schema is invalid")
    unsigned = dict(missingness)
    value = unsigned.pop("digest")
    if _digest(value, "missingness digest") != digest(unsigned):
        raise ValueError("missingness artifact digest is invalid")
    if (
        missingness["activity_coverage"] != "legacy_partial_snapshot"
        or missingness["features"] != list(FEATURES)
        or missingness["provenance"] != ACTIVITY_PROVENANCE
    ):
        raise ValueError("missingness artifact policy is invalid")
    logical = _mapping(candidate["logical"], "model logical")
    if _mapping(logical["dataset"], "model dataset").get("provenance") != ACTIVITY_PROVENANCE:
        raise ValueError("missingness artifact does not match candidate provenance")
    summaries = missingness["summaries"]
    population = _mapping(logical["population"], "model population")
    if not isinstance(summaries, dict) or set(summaries) != {"training", "held_out"}:
        raise ValueError("missingness summaries are invalid")
    expected_denominators = {
        "training": len(_digest_list(population["training_members"], "training membership")),
        "held_out": len(_digest_list(population["held_out_members"], "held-out membership")),
    }
    for partition, denominator in expected_denominators.items():
        summary = summaries[partition]
        if (
            not isinstance(summary, dict)
            or summary.get("denominator") != denominator
            or not isinstance(summary.get("features"), dict)
            or not isinstance(summary.get("reasons"), dict)
        ):
            raise ValueError("missingness summaries are invalid")
        feature_summary = summary["features"]
        if not isinstance(feature_summary, dict) or set(feature_summary) != set(FEATURES):
            raise ValueError("missingness summaries are invalid")
        for feature in FEATURES:
            counts = feature_summary[feature]
            if (
                not isinstance(counts, dict)
                or set(counts) != {"denominator", "missing"}
                or counts.get("denominator") != denominator
                or not isinstance(counts.get("missing"), int)
                or isinstance(counts.get("missing"), bool)
            ):
                raise ValueError("missingness summaries are invalid")
            missing = counts["missing"]
            if (
                not isinstance(missing, int)
                or isinstance(missing, bool)
                or not 0 <= missing <= denominator
            ):
                raise ValueError("missingness summaries are invalid")


def _verify_comparison(
    descriptor: ComparisonAcceptanceDescriptor, comparison: dict[str, JsonValue]
) -> None:
    if set(comparison) != {"comparison_id", "logical", "schema_version"}:
        raise ValueError("comparison schema is invalid")
    if (
        comparison["schema_version"] != COMPARISON_SCHEMA
        or comparison["comparison_id"] != descriptor.comparison_id
    ):
        raise ValueError("comparison identity is invalid")
    logical = _mapping(comparison["logical"], "comparison logical")
    required = {
        "left_evaluation_id",
        "left_metrics",
        "metrics_contract",
        "population_digest",
        "right_evaluation_id",
        "right_metrics",
    }
    if set(logical) != required or digest(logical) != descriptor.comparison_logical_digest:
        raise ValueError("comparison logical content is invalid")
    if descriptor.comparison_id != f"comparison-{descriptor.comparison_logical_digest[:24]}":
        raise ValueError("comparison identifier is noncanonical")
    if (
        logical["left_evaluation_id"] != descriptor.raw["left_evaluation_id"]
        or logical["right_evaluation_id"] != descriptor.raw["right_evaluation_id"]
    ):
        raise ValueError("comparison descriptor linkage is invalid")


def _verify_train_descriptor_files(
    descriptor: ModelAcceptanceDescriptor,
    root: Path,
    candidate: Path,
    population: Path,
    missingness: Path,
    evaluation: Path,
    budget: ReadBudget,
) -> None:
    expected = {
        f"models/{descriptor.model_id}/candidate.json": candidate,
        f"models/{descriptor.model_id}/population.ndjson": population,
        f"models/{descriptor.model_id}/missingness.json": missingness,
        f"evaluations/{descriptor.evaluation_id}/evaluation.json": evaluation,
    }
    _verify_inventory(descriptor.inventory, root, budget, expected)


def _verify_evaluation_descriptor_files(
    descriptor: EvaluationAcceptanceDescriptor,
    root: Path,
    evaluation: Path,
    budget: ReadBudget,
) -> None:
    expected = {f"evaluations/{descriptor.evaluation_id}/evaluation.json": evaluation}
    _verify_inventory(descriptor.inventory, root, budget, expected)


def _verify_inventory(
    inventory: tuple[OutputInventory, ...],
    root: Path,
    budget: ReadBudget,
    expected_paths: dict[str, Path] | None = None,
) -> None:
    if expected_paths is not None and {item.relative_path for item in inventory} != set(
        expected_paths
    ):
        raise ValueError("descriptor inventory is incomplete or has unexpected files")
    for item in inventory:
        path = root / item.relative_path
        if expected_paths is not None and expected_paths[item.relative_path] != path:
            raise ValueError("descriptor inventory path is invalid")
        metadata = regular_file(path, "model descriptor inventory")
        if metadata.st_size != item.byte_count:
            raise ValueError("descriptor inventory byte count is invalid")
        if digest_file(path, budget, maximum_file_bytes=MAX_ARTIFACT_FILE_BYTES) != item.sha256:
            raise ValueError("descriptor inventory checksum is invalid")


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
