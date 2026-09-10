"""Bounded State-backed discovery for immutable inactive model workflow artifacts."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

from intelligence.artifacts_manifest import validate_manifest
from intelligence.datasets.bounds import (
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACT_ENTRIES,
    MAX_ARTIFACT_FILE_BYTES,
    MAX_ARTIFACT_ROWS,
    MAX_DESCRIPTOR_BYTES,
    ReadBudget,
    digest_file,
)
from intelligence.model_workflows.artifacts import (
    ComparisonAcceptanceDescriptor,
    EvaluationAcceptanceDescriptor,
    ModelAcceptanceDescriptor,
    TrainBundle,
    artifact_budget,
    verify_comparison_bundle,
    verify_evaluation_bundle,
    verify_train_bundle,
)
from intelligence.model_workflows.codec import JsonValue, canonical_object
from intelligence.model_workflows.contracts import safe_id
from intelligence.model_workflows.path_safety import (
    output_run_root,
    regular_file,
    safe_directory,
    terminal_log_root,
    terminal_manifest_root,
)
from intelligence.models import OutputInventory, Run, RunLogInventory
from intelligence.state_readonly import ReadOnlyState

MAX_CATALOG_RUNS = 1_000
MAX_LIST_LIMIT = 100


@dataclass(frozen=True)
class ModelEntry:
    """One terminal-State-verified inactive candidate and its required evaluation."""

    model_id: str
    run_id: str
    candidate: dict[str, object]
    evaluation: dict[str, object]
    descriptor: ModelAcceptanceDescriptor
    evaluation_descriptor: EvaluationAcceptanceDescriptor

    def summary(self) -> dict[str, object]:
        return {"active": False, "model_id": self.model_id, "run_id": self.run_id}


@dataclass(frozen=True)
class EvaluationEntry:
    """One terminal-State-verified held-out or independent evaluation."""

    evaluation_id: str
    run_id: str
    evaluation: dict[str, object]
    descriptor: EvaluationAcceptanceDescriptor

    def summary(self) -> dict[str, object]:
        return {
            "evaluation_id": self.evaluation_id,
            "model_id": self.descriptor.model_id,
            "run_id": self.run_id,
        }


@dataclass(frozen=True)
class ComparisonEntry:
    """One terminal-State-verified comparison report."""

    comparison_id: str
    run_id: str
    comparison: dict[str, object]
    descriptor: ComparisonAcceptanceDescriptor

    def summary(self) -> dict[str, object]:
        return {"comparison_id": self.comparison_id, "run_id": self.run_id}


def find(workspace: Path, model_id: str, run_id: str) -> ModelEntry:
    """Resolve one exact candidate directly; never trust caller-selected paths."""
    safe_id(model_id, "model id")
    safe_id(run_id, "model run")
    entry = _model_entry(workspace, run_id)
    if entry.model_id != model_id:
        raise ValueError("model candidate is absent or ambiguous")
    return entry


def find_evaluation(workspace: Path, evaluation_id: str, run_id: str) -> EvaluationEntry:
    """Resolve one exact train or evaluation-run evaluation publication."""
    safe_id(evaluation_id, "evaluation id")
    safe_id(run_id, "evaluation run")
    entry = _evaluation_entry(workspace, run_id)
    if entry.evaluation_id != evaluation_id:
        raise ValueError("evaluation is absent or ambiguous")
    return entry


def evaluation_for_run(workspace: Path, run_id: str) -> EvaluationEntry:
    """Resolve the sole fully verified evaluation publication from an exact named run."""
    safe_id(run_id, "evaluation run")
    return _evaluation_entry(workspace, run_id)


def find_comparison(workspace: Path, comparison_id: str, run_id: str) -> ComparisonEntry:
    """Resolve one exact persisted comparison publication."""
    safe_id(comparison_id, "comparison id")
    safe_id(run_id, "comparison run")
    entry = _comparison_entry(workspace, run_id)
    if entry.comparison_id != comparison_id:
        raise ValueError("comparison is absent or ambiguous")
    return entry


def entries(workspace: Path, budget: ReadBudget | None = None) -> tuple[ModelEntry, ...]:
    """Enumerate all bounded, complete train candidates; overflow fails closed."""
    run_ids = _completed_ids(workspace, "train_run")
    read_budget = budget or _catalog_budget()
    return tuple(_model_entry(workspace, run_id, read_budget) for run_id in run_ids)


def evaluation_entries(
    workspace: Path, budget: ReadBudget | None = None
) -> tuple[EvaluationEntry, ...]:
    """Enumerate all bounded train and independent evaluation publications."""
    read_budget = budget or _catalog_budget()
    train = tuple(
        _evaluation_entry(workspace, run_id, read_budget)
        for run_id in _completed_ids(workspace, "train_run")
    )
    independent = tuple(
        _evaluation_entry(workspace, run_id, read_budget)
        for run_id in _completed_ids(workspace, "evaluate_run")
    )
    return tuple(sorted((*train, *independent), key=lambda item: (item.run_id, item.evaluation_id)))


def comparison_entries(
    workspace: Path, budget: ReadBudget | None = None
) -> tuple[ComparisonEntry, ...]:
    """Enumerate all bounded, complete comparison publications."""
    read_budget = budget or _catalog_budget()
    return tuple(
        _comparison_entry(workspace, run_id, read_budget)
        for run_id in _completed_ids(workspace, "evaluate_compare")
    )


def list_entries(workspace: Path, limit: int) -> tuple[ModelEntry, ...]:
    """Return a page only after validating the complete bounded model catalog."""
    _limit(limit)
    return entries(workspace)[:limit]


def list_evaluations(workspace: Path, limit: int) -> tuple[EvaluationEntry, ...]:
    """Return a page only after validating the complete bounded evaluation catalog."""
    _limit(limit)
    return evaluation_entries(workspace)[:limit]


def list_comparisons(workspace: Path, limit: int) -> tuple[ComparisonEntry, ...]:
    """Return a page only after validating the complete bounded comparison catalog."""
    _limit(limit)
    return comparison_entries(workspace)[:limit]


def check_model_replay_conflict(workspace: Path, bundle: TrainBundle) -> None:
    """Permit identical replay only; reject any request/id/content disagreement."""
    for prior in entries(workspace):
        if prior.descriptor.request_digest != bundle.model_descriptor.request_digest:
            continue
        if (
            prior.model_id != bundle.model_descriptor.model_id
            or prior.descriptor.model_logical_digest != bundle.model_descriptor.model_logical_digest
            or prior.descriptor.missingness_digest != bundle.model_descriptor.missingness_digest
            or prior.candidate != bundle.model
            or prior.evaluation != bundle.evaluation
        ):
            raise RuntimeError("model replay conflicts with immutable accepted content")


def check_evaluation_replay_conflict(
    workspace: Path,
    descriptor: EvaluationAcceptanceDescriptor,
    evaluation: dict[str, JsonValue],
) -> None:
    """Reject a prior accepted request that would require mutable evaluation output."""
    for prior in evaluation_entries(workspace):
        if prior.descriptor.request_digest == descriptor.request_digest and (
            prior.evaluation_id != descriptor.evaluation_id
            or prior.descriptor.evaluation_logical_digest != descriptor.evaluation_logical_digest
            or prior.evaluation != evaluation
        ):
            raise RuntimeError("evaluation replay conflicts with immutable accepted content")


def check_comparison_replay_conflict(
    workspace: Path,
    descriptor: ComparisonAcceptanceDescriptor,
    comparison: dict[str, JsonValue],
) -> None:
    """Reject a prior accepted comparison request with different immutable output."""
    for prior in comparison_entries(workspace):
        if prior.descriptor.request_digest == descriptor.request_digest and (
            prior.comparison_id != descriptor.comparison_id
            or prior.descriptor.comparison_logical_digest != descriptor.comparison_logical_digest
            or prior.comparison != comparison
        ):
            raise RuntimeError("comparison replay conflicts with immutable accepted content")


def _model_entry(workspace: Path, run_id: str, budget: ReadBudget | None = None) -> ModelEntry:
    run, accepted = _accepted_run(workspace, run_id, "train_run")
    read_budget = budget or artifact_budget()
    _terminal_manifest(workspace, run, accepted, read_budget)
    bundle = verify_train_bundle(
        output_run_root(workspace, run_id), run_id, accepted=accepted, budget=read_budget
    )
    return ModelEntry(
        bundle.model_descriptor.model_id,
        run_id,
        _public(bundle.model),
        _public(bundle.evaluation),
        bundle.model_descriptor,
        bundle.evaluation_descriptor,
    )


def _evaluation_entry(
    workspace: Path, run_id: str, budget: ReadBudget | None = None
) -> EvaluationEntry:
    run, accepted = _accepted_run_any(workspace, run_id, {"train_run", "evaluate_run"})
    read_budget = budget or artifact_budget()
    _terminal_manifest(workspace, run, accepted, read_budget)
    root = output_run_root(workspace, run_id)
    if run.command == "train_run":
        bundle = verify_train_bundle(root, run_id, accepted=accepted, budget=read_budget)
        return EvaluationEntry(
            bundle.evaluation_descriptor.evaluation_id,
            run_id,
            _public(bundle.evaluation),
            bundle.evaluation_descriptor,
        )
    evaluation, descriptor = verify_evaluation_bundle(
        root, run_id, accepted=accepted, budget=read_budget
    )
    model = _model_entry(workspace, descriptor.model_run_id)
    if (
        model.model_id != descriptor.model_id
        or model.descriptor.model_logical_digest != descriptor.raw["model_logical_digest"]
    ):
        raise ValueError("evaluation candidate linkage is invalid")
    model_population = model.candidate.get("logical")
    evaluation_logical = evaluation.get("logical")
    if not isinstance(model_population, dict) or not isinstance(evaluation_logical, dict):
        raise ValueError("evaluation candidate linkage is invalid")
    population = model_population.get("population")
    if (
        not isinstance(population, dict)
        or descriptor.raw.get("evaluation_kind") != "independent_held_out_replay"
        or evaluation_logical.get("evaluation_kind") != "independent_held_out_replay"
        or evaluation_logical.get("population_members") != population.get("held_out_members")
        or evaluation_logical.get("population_digest")
        != population.get("held_out_membership_digest")
        or set(population.get("held_out_members", [])) & set(population.get("training_members", []))
    ):
        raise ValueError("independent evaluation population is incompatible")
    return EvaluationEntry(descriptor.evaluation_id, run_id, _public(evaluation), descriptor)


def _comparison_entry(
    workspace: Path, run_id: str, budget: ReadBudget | None = None
) -> ComparisonEntry:
    run, accepted = _accepted_run(workspace, run_id, "evaluate_compare")
    read_budget = budget or artifact_budget()
    _terminal_manifest(workspace, run, accepted, read_budget)
    comparison, descriptor = verify_comparison_bundle(
        output_run_root(workspace, run_id), run_id, accepted=accepted, budget=read_budget
    )
    _verify_comparison_evaluations(workspace, comparison, descriptor)
    return ComparisonEntry(descriptor.comparison_id, run_id, _public(comparison), descriptor)


def _public(value: dict[str, JsonValue]) -> dict[str, object]:
    """Widen a verified JSON object at the legacy model-workflow boundary."""
    return {key: item for key, item in value.items()}


def _verify_comparison_evaluations(
    workspace: Path,
    comparison: dict[str, JsonValue],
    descriptor: ComparisonAcceptanceDescriptor,
) -> None:
    left = find_evaluation(
        workspace,
        str(descriptor.raw["left_evaluation_id"]),
        str(descriptor.raw["left_run_id"]),
    )
    right = find_evaluation(
        workspace,
        str(descriptor.raw["right_evaluation_id"]),
        str(descriptor.raw["right_run_id"]),
    )
    logical = comparison.get("logical")
    left_logical = left.evaluation.get("logical")
    right_logical = right.evaluation.get("logical")
    if (
        not isinstance(logical, dict)
        or not isinstance(left_logical, dict)
        or not isinstance(right_logical, dict)
    ):
        raise ValueError("comparison evaluation logical content is invalid")
    if (
        logical.get("left_metrics") != left_logical.get("metrics")
        or logical.get("right_metrics") != right_logical.get("metrics")
        or logical.get("population_digest") != left_logical.get("population_digest")
        or logical.get("population_digest") != right_logical.get("population_digest")
        or logical.get("metrics_contract") != left_logical.get("metrics_contract")
        or logical.get("metrics_contract") != right_logical.get("metrics_contract")
        or left_logical.get("labels") != right_logical.get("labels")
        or left_logical.get("dataset") != right_logical.get("dataset")
    ):
        raise ValueError("comparison evaluation linkage is invalid")


def _completed_ids(workspace: Path, command: str) -> tuple[str, ...]:
    state = ReadOnlyState.open(workspace)
    try:
        return state.completed_run_ids(command, MAX_CATALOG_RUNS)
    finally:
        state.close()


def _accepted_run(
    workspace: Path, run_id: str, command: str
) -> tuple[Run, tuple[OutputInventory, ...]]:
    run, accepted = _accepted_run_any(workspace, run_id, {command})
    return run, accepted


def _accepted_run_any(
    workspace: Path, run_id: str, commands: set[str]
) -> tuple[Run, tuple[OutputInventory, ...]]:
    state = ReadOnlyState.open(workspace)
    try:
        run = state.inspect(run_id)
        accepted = state.accepted_outputs(run_id)
    finally:
        state.close()
    if run is None or run.state != "completed" or run.command not in commands or not accepted:
        raise ValueError("model workflow run is not accepted")
    root = output_run_root(workspace, run_id)
    budget = artifact_budget()
    _preflight_accepted_tree(root, run_id, accepted, budget)
    for item in accepted:
        prefix = f"outputs/{run_id}/"
        if not item.relative_path.startswith(prefix):
            raise ValueError("State output path is invalid")
        path = root / item.relative_path.removeprefix(prefix)
        metadata = regular_file(path, "State registered model output")
        maximum = (
            MAX_DESCRIPTOR_BYTES
            if "/acceptance-descriptors/" in item.relative_path
            else MAX_ARTIFACT_FILE_BYTES
        )
        if (
            metadata.st_size != item.byte_count
            or digest_file(path, budget, maximum_file_bytes=maximum) != item.sha256
        ):
            raise ValueError("State registered model output checksum is invalid")
    return run, accepted


def _preflight_accepted_tree(
    root: Path, run_id: str, accepted: tuple[OutputInventory, ...], budget: ReadBudget
) -> None:
    """Reject unsafe ancestors/links/extra entries before opening any accepted output bytes."""
    expected: dict[str, OutputInventory] = {}
    prefix = f"outputs/{run_id}/"
    for item in accepted:
        if not item.relative_path.startswith(prefix):
            raise ValueError("State output path is invalid")
        relative = item.relative_path.removeprefix(prefix)
        if relative in expected:
            raise ValueError("State output paths are duplicated")
        expected[relative] = item
    actual: set[str] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        safe_directory(directory, "State registered model output directory")
        try:
            children = tuple(directory.iterdir())
        except OSError as error:
            raise ValueError("State output directory cannot be read") from error
        for child in children:
            budget.entry()
            relative = child.relative_to(root).as_posix()
            metadata = child.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                safe_directory(child, "State registered model output directory")
                pending.append(child)
                continue
            regular = regular_file(child, "State registered model output")
            registered = expected.get(relative)
            if registered is None or regular.st_size != registered.byte_count:
                raise ValueError("State-registered output tree is missing, extra, or size-invalid")
            actual.add(relative)
    if actual != set(expected):
        raise ValueError("State-registered output tree is missing or incomplete")


def _terminal_manifest(
    workspace: Path,
    run: Run,
    accepted: tuple[OutputInventory, ...],
    budget: ReadBudget,
) -> None:
    path = terminal_manifest_root(workspace) / f"{run.run_id}.json"
    try:
        value = canonical_object(path, budget, maximum_file_bytes=MAX_DESCRIPTOR_BYTES)
    except FileNotFoundError:
        raise ValueError("terminal manifest is missing") from None
    run_log = _terminal_run_log(workspace, run.run_id, value, budget)
    validate_manifest(
        value,
        expected_run_id=run.run_id,
        expected_command=run.command,
        expected_state="completed",
        expected_outputs=accepted,
        expected_created_at=run.created_at,
        expected_started_at=run.started_at,
        expected_limits=dict(run.limits) if run.limits else None,
        expected_run_log=run_log,
        expected_command_provenance=(
            None if run.command_provenance is None else dict(run.command_provenance)
        ),
    )


def _terminal_run_log(
    workspace: Path,
    run_id: str,
    manifest: dict[str, JsonValue],
    budget: ReadBudget,
) -> RunLogInventory | None:
    raw = manifest.get("run_log")
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"byte_count", "path", "sha256"}:
        raise ValueError("terminal manifest log evidence is invalid")
    expected_path = f"runs/logs/{run_id}.ndjson"
    path = terminal_log_root(workspace) / f"{run_id}.ndjson"
    byte_count = raw.get("byte_count")
    sha256 = raw.get("sha256")
    if (
        raw.get("path") != expected_path
        or not isinstance(byte_count, int)
        or not isinstance(sha256, str)
    ):
        raise ValueError("terminal manifest log evidence is invalid")
    metadata = regular_file(path, "terminal manifest log")
    if (
        metadata.st_size != byte_count
        or digest_file(path, budget, maximum_file_bytes=MAX_DESCRIPTOR_BYTES) != sha256
    ):
        raise ValueError("terminal manifest log evidence is invalid")
    return RunLogInventory(expected_path, sha256, byte_count)


def _limit(limit: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIST_LIMIT:
        raise ValueError("model catalog limit is invalid")


def _catalog_budget() -> ReadBudget:
    """Bound complete catalog traversal independently from a single-run bundle budget."""
    return ReadBudget(
        MAX_ARTIFACT_BYTES * 3 * MAX_CATALOG_RUNS,
        MAX_ARTIFACT_ENTRIES * 3 * MAX_CATALOG_RUNS,
        MAX_ARTIFACT_ROWS * 3 * MAX_CATALOG_RUNS,
    )
