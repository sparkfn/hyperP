"""Bounded complete-bundle verification for model workflow publications."""

from __future__ import annotations

from pathlib import Path

from intelligence.datasets.bounds import (
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACT_ENTRIES,
    MAX_ARTIFACT_FILE_BYTES,
    MAX_DESCRIPTOR_BYTES,
    ReadBudget,
    registered_tree,
)
from intelligence.model_workflows.artifact_descriptors import (
    MAX_MODEL_ROWS,
    ComparisonAcceptanceDescriptor,
    EvaluationAcceptanceDescriptor,
    ModelAcceptanceDescriptor,
    TrainBundle,
    parse_comparison_descriptor,
    parse_evaluation_descriptor,
    parse_model_descriptor,
)
from intelligence.model_workflows.artifact_validation import (
    _verify_comparison,
    _verify_evaluation,
    _verify_evaluation_descriptor_files,
    _verify_inventory,
    _verify_missingness,
    _verify_model,
    _verify_population,
    _verify_train_descriptor_files,
)
from intelligence.model_workflows.codec import JsonValue, canonical_ndjson, canonical_object
from intelligence.model_workflows.contracts import digest, safe_id
from intelligence.model_workflows.path_safety import regular_file, safe_directory
from intelligence.models import OutputInventory


def artifact_budget() -> ReadBudget:
    """Return a conservative non-resettable read budget for one model run."""
    return ReadBudget(MAX_ARTIFACT_BYTES * 3, MAX_ARTIFACT_ENTRIES * 3, MAX_MODEL_ROWS * 3)


def verify_train_bundle(
    root: Path,
    run_id: str,
    *,
    accepted: tuple[OutputInventory, ...] | None = None,
    budget: ReadBudget | None = None,
) -> TrainBundle:
    """Verify a complete train bundle with exact files and optional State inventory."""
    safe_id(run_id, "model run")
    read_budget = budget or artifact_budget()
    safe_directory(root, "model artifact root")
    if accepted is not None:
        registered_tree(
            root,
            f"outputs/{run_id}/",
            accepted,
            read_budget,
            maximum_file_bytes=MAX_ARTIFACT_FILE_BYTES,
        )
    descriptors = _descriptor_files(root, "models", read_budget)
    if len(descriptors) != 1:
        raise ValueError("train bundle must contain exactly one model descriptor")
    model_descriptor = parse_model_descriptor(_read_descriptor(descriptors[0], read_budget), run_id)
    model_id = model_descriptor.model_id
    evaluation_id = model_descriptor.evaluation_id
    model_path = root / "models" / model_id / "candidate.json"
    population_path = root / "models" / model_id / "population.ndjson"
    missingness_path = root / "models" / model_id / "missingness.json"
    evaluation_path = root / "evaluations" / evaluation_id / "evaluation.json"
    evaluation_descriptor_path = (
        root / "acceptance-descriptors" / "evaluations" / f"{evaluation_id}.json"
    )
    expected = {
        "models/" + model_id + "/candidate.json",
        "models/" + model_id + "/population.ndjson",
        "models/" + model_id + "/missingness.json",
        "evaluations/" + evaluation_id + "/evaluation.json",
        "acceptance-descriptors/models/" + model_id + ".json",
        "acceptance-descriptors/evaluations/" + evaluation_id + ".json",
    }
    _exact_tree(root, expected, read_budget)
    candidate = _read_json(model_path, read_budget)
    evaluation = _read_json(evaluation_path, read_budget)
    population = canonical_ndjson(
        population_path,
        read_budget,
        maximum_file_bytes=MAX_ARTIFACT_FILE_BYTES,
        maximum_rows=MAX_MODEL_ROWS,
    )
    missingness = _read_json(missingness_path, read_budget)
    evaluation_descriptor = parse_evaluation_descriptor(
        _read_descriptor(evaluation_descriptor_path, read_budget), run_id
    )
    _verify_model(model_descriptor, candidate)
    _verify_evaluation(evaluation_descriptor, evaluation, model_id, run_id)
    _verify_population(candidate, population)
    _verify_missingness(candidate, missingness)
    if digest(missingness) != model_descriptor.missingness_digest:
        raise ValueError("model descriptor missingness digest is invalid")
    _verify_train_descriptor_files(
        model_descriptor,
        root,
        model_path,
        population_path,
        missingness_path,
        evaluation_path,
        read_budget,
    )
    _verify_evaluation_descriptor_files(evaluation_descriptor, root, evaluation_path, read_budget)
    _verify_train_linkage(model_descriptor, evaluation_descriptor, candidate, evaluation)
    return TrainBundle(
        candidate,
        evaluation,
        population,
        missingness,
        model_descriptor,
        evaluation_descriptor,
    )


def _verify_train_linkage(
    model_descriptor: ModelAcceptanceDescriptor,
    evaluation_descriptor: EvaluationAcceptanceDescriptor,
    candidate: dict[str, JsonValue],
    evaluation: dict[str, JsonValue],
) -> None:
    """Bind mandatory held-out evidence to the exact inactive candidate logical population."""
    model_logical = candidate.get("logical")
    evaluation_logical = evaluation.get("logical")
    if not isinstance(model_logical, dict) or not isinstance(evaluation_logical, dict):
        raise ValueError("model/evaluation descriptor linkage is invalid")
    population = model_logical.get("population")
    if not isinstance(population, dict):
        raise ValueError("model/evaluation descriptor linkage is invalid")
    held = population.get("held_out_members")
    held_digest = population.get("held_out_membership_digest")
    if (
        model_descriptor.evaluation_id != evaluation_descriptor.evaluation_id
        or model_descriptor.evaluation_logical_digest
        != evaluation_descriptor.evaluation_logical_digest
        or model_descriptor.evaluation_logical_digest != digest(evaluation_logical)
        or model_descriptor.model_logical_digest
        != evaluation_descriptor.raw.get("model_logical_digest")
        or model_descriptor.model_logical_digest != digest(model_logical)
        or model_descriptor.raw.get("dataset") != evaluation_descriptor.raw.get("dataset")
        or model_descriptor.raw.get("dataset") != model_logical.get("dataset")
        or evaluation_descriptor.raw.get("dataset") != evaluation_logical.get("dataset")
        or evaluation_descriptor.raw.get("evaluation_kind") != "held_out"
        or evaluation_logical.get("evaluation_kind") != "held_out"
        or evaluation_descriptor.model_run_id != model_descriptor.run_id
        or evaluation_logical.get("population_members") != held
        or evaluation_logical.get("population_digest") != held_digest
    ):
        raise ValueError("model/evaluation descriptor linkage is invalid")


def verify_evaluation_bundle(
    root: Path,
    run_id: str,
    *,
    accepted: tuple[OutputInventory, ...] | None = None,
    budget: ReadBudget | None = None,
) -> tuple[dict[str, JsonValue], EvaluationAcceptanceDescriptor]:
    """Verify one exact independently-published evaluation and its descriptor."""
    safe_id(run_id, "evaluation run")
    read_budget = budget or artifact_budget()
    safe_directory(root, "evaluation artifact root")
    if accepted is not None:
        registered_tree(
            root,
            f"outputs/{run_id}/",
            accepted,
            read_budget,
            maximum_file_bytes=MAX_ARTIFACT_FILE_BYTES,
        )
    descriptors = _descriptor_files(root, "evaluations", read_budget)
    if len(descriptors) != 1:
        raise ValueError("evaluation bundle must contain exactly one descriptor")
    descriptor = parse_evaluation_descriptor(_read_descriptor(descriptors[0], read_budget), run_id)
    expected = {
        f"evaluations/{descriptor.evaluation_id}/evaluation.json",
        f"acceptance-descriptors/evaluations/{descriptor.evaluation_id}.json",
    }
    _exact_tree(root, expected, read_budget)
    evaluation_path = root / "evaluations" / descriptor.evaluation_id / "evaluation.json"
    evaluation = _read_json(evaluation_path, read_budget)
    _verify_evaluation(descriptor, evaluation, descriptor.model_id, descriptor.model_run_id)
    _verify_evaluation_descriptor_files(descriptor, root, evaluation_path, read_budget)
    return evaluation, descriptor


def verify_comparison_bundle(
    root: Path,
    run_id: str,
    *,
    accepted: tuple[OutputInventory, ...] | None = None,
    budget: ReadBudget | None = None,
) -> tuple[dict[str, JsonValue], ComparisonAcceptanceDescriptor]:
    """Verify one exact comparison and its State-registerable descriptor."""
    safe_id(run_id, "comparison run")
    read_budget = budget or artifact_budget()
    safe_directory(root, "comparison artifact root")
    if accepted is not None:
        registered_tree(
            root,
            f"outputs/{run_id}/",
            accepted,
            read_budget,
            maximum_file_bytes=MAX_ARTIFACT_FILE_BYTES,
        )
    descriptors = _descriptor_files(root, "comparisons", read_budget)
    if len(descriptors) != 1:
        raise ValueError("comparison bundle must contain exactly one descriptor")
    descriptor = parse_comparison_descriptor(_read_descriptor(descriptors[0], read_budget), run_id)
    expected = {
        f"comparisons/{descriptor.comparison_id}/comparison.json",
        f"acceptance-descriptors/comparisons/{descriptor.comparison_id}.json",
    }
    _exact_tree(root, expected, read_budget)
    comparison_path = root / "comparisons" / descriptor.comparison_id / "comparison.json"
    comparison = _read_json(comparison_path, read_budget)
    _verify_comparison(descriptor, comparison)
    _verify_inventory(
        descriptor.inventory,
        root,
        read_budget,
        {f"comparisons/{descriptor.comparison_id}/comparison.json": comparison_path},
    )
    return comparison, descriptor


def _read_descriptor(path: Path, budget: ReadBudget) -> dict[str, JsonValue]:
    return canonical_object(path, budget, maximum_file_bytes=MAX_DESCRIPTOR_BYTES)


def _read_json(path: Path, budget: ReadBudget) -> dict[str, JsonValue]:
    return canonical_object(path, budget, maximum_file_bytes=MAX_ARTIFACT_FILE_BYTES)


def _descriptor_files(root: Path, kind: str, budget: ReadBudget) -> tuple[Path, ...]:
    directory = root / "acceptance-descriptors" / kind
    safe_directory(root / "acceptance-descriptors", "acceptance descriptor root")
    safe_directory(directory, "acceptance descriptor directory")
    values: list[Path] = []
    try:
        for path in directory.iterdir():
            budget.entry()
            metadata = regular_file(path, "acceptance descriptor")
            if metadata.st_size > MAX_DESCRIPTOR_BYTES or path.suffix != ".json":
                raise ValueError("acceptance descriptor is invalid")
            values.append(path)
    except OSError as error:
        raise ValueError("acceptance descriptor directory cannot be read") from error
    return tuple(sorted(values, key=lambda item: item.name))


def _exact_tree(root: Path, expected: set[str], budget: ReadBudget) -> None:
    actual: set[str] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        safe_directory(directory, "model artifact directory")
        try:
            children = tuple(directory.iterdir())
        except OSError as error:
            raise ValueError("model artifact directory cannot be read") from error
        for child in children:
            budget.entry()
            relative = child.relative_to(root).as_posix()
            metadata = child.lstat()
            if child.is_symlink():
                raise ValueError("model artifact bundle contains a symbolic link")
            if child.is_dir():
                safe_directory(child, "model artifact directory")
                pending.append(child)
            else:
                regular_file(child, "model artifact")
                if metadata.st_size > MAX_ARTIFACT_FILE_BYTES:
                    raise RuntimeError("model artifact exceeds per-file byte ceiling")
                actual.add(relative)
    if actual != expected:
        raise ValueError("model artifact bundle has missing or unexpected unsafe files")
