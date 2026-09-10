"""Request-scoped reviewed command builders with parent-admitted provenance."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from intelligence.artifacts import canonical_json
from intelligence.model_workflows.admission import (
    check_staged_comparison_replay,
    check_staged_evaluation_replay,
    check_staged_train_replay,
)
from intelligence.model_workflows.comparison import write as write_comparison
from intelligence.model_workflows.contracts import (
    CHILD_LIMITS,
    RUNTIME_LIMITS,
    EvaluationRequest,
    VerifyRequest,
    code_fingerprint,
    digest,
)
from intelligence.model_workflows.core import load_model, write_evaluation, write_train
from intelligence.model_workflows.dataset_admission import (
    AdmittedDataset,
    DatasetPin,
    admit_dataset,
)
from intelligence.registry import Cancelled, RegisteredCommand, Registry, SafeRejectionError


@dataclass(frozen=True)
class ModelPin:
    model_id: str
    run_id: str
    logical_digest: str
    seed: int


def train_registry(dataset: DatasetPin) -> Registry:
    """Build a reviewed train command only after parent-side accepted dataset admission."""
    return _registry("train_run", partial(_train, dataset), _dataset_metadata("train", dataset))


def evaluation_registry(
    request: EvaluationRequest, dataset: DatasetPin, model: ModelPin
) -> Registry:
    """Build an evaluation command with pinned model and dataset evidence."""
    return _registry(
        "evaluate_run",
        partial(_evaluate, request, dataset, model),
        _model_metadata("evaluate", request, dataset, model),
    )


def verify_registry(request: VerifyRequest, model: ModelPin) -> Registry:
    """Build artifact-only verification with its complete parent-verified catalog entry."""
    return _registry(
        "model_verify",
        partial(_verify, request, model),
        _model_metadata("verify", request, None, model),
    )


def comparison_registry(
    left_run_id: str, right_run_id: str, comparison: dict[str, object]
) -> Registry:
    """Build a persisted comparison command after parent-side compatibility validation."""
    return _registry(
        "evaluate_compare",
        partial(_compare, comparison, left_run_id, right_run_id),
        {
            "comparison_id": _text(comparison.get("comparison_id")),
            "comparison_logical_digest": digest(comparison.get("logical")),
            "left_run_id": left_run_id,
            "right_run_id": right_run_id,
            "workflow": "compare",
        },
    )


def _registry(
    name: str,
    handler: object,
    metadata: dict[str, str | int | float | bool | None],
) -> Registry:
    if not callable(handler):
        raise ValueError("reviewed handler is invalid")
    metadata = {
        "code_fingerprint": code_fingerprint(),
        "failure_metrics_status": "unavailable",
        **metadata,
    }
    return Registry(
        (
            RegisteredCommand(
                name,
                True,
                handler,
                metadata,
                child_limits=CHILD_LIMITS,
                runtime_limits=RUNTIME_LIMITS,
                rejection_codes=(
                    "dataset_drift",
                    "model_drift",
                    "comparison_drift",
                    "replay_conflict",
                    "incompatible_population",
                    "cancelled_before_work",
                    "malformed_artifact",
                    "incompatible_target",
                ),
            ),
        )
    )


def _dataset_metadata(
    action: str, dataset: DatasetPin
) -> dict[str, str | int | float | bool | None]:
    pin = dataset.pin()
    return {
        "code_fingerprint": code_fingerprint(),
        "coverage": "legacy_partial_snapshot",
        "dataset_config_digest": str(pin["config_digest"]),
        "dataset_content_digest": str(pin["content_digest"]),
        "dataset_id": str(pin["dataset_id"]),
        "dataset_manifest_digest": str(pin["manifest_digest"]),
        "dataset_run_id": str(pin["accepted_run_id"]),
        "recipe": dataset.request.recipe,
        "recipe_version": "1",
        "seed": dataset.request.seed,
        "workflow": action,
        **dataset.source_pin_scalars(),
    }


def _model_metadata(
    action: str,
    request: EvaluationRequest | VerifyRequest,
    dataset: DatasetPin | None,
    model: ModelPin,
) -> dict[str, str | int | float | bool | None]:
    result: dict[str, str | int | float | bool | None] = {
        "model_id": model.model_id,
        "model_logical_digest": model.logical_digest,
        "model_run_id": model.run_id,
        "workflow": action,
    }
    if dataset is None:
        result["request_digest"] = digest(request.as_dict())
    else:
        result.update(_dataset_metadata(action, dataset))
    return result


def _train(dataset: DatasetPin, staging: Path, cancelled: Cancelled) -> None:
    if cancelled():
        raise SafeRejectionError("cancelled_before_work")
    admitted = _admit_dataset_or_reject(staging.parent.parent, dataset)
    if admitted.pin() != dataset.pin():
        raise SafeRejectionError("dataset_drift")
    try:
        write_train(staging, admitted)
    except ValueError as error:
        if str(error) == "insufficient deterministic held-out partitions":
            raise SafeRejectionError("incompatible_population") from error
        raise SafeRejectionError("malformed_artifact") from error
    _check_train_replay(staging)
    _require_dataset_pin(staging.parent.parent, dataset)


def _evaluate(
    request: EvaluationRequest,
    dataset: DatasetPin,
    model: ModelPin,
    staging: Path,
    cancelled: Cancelled,
) -> None:
    if cancelled():
        raise SafeRejectionError("cancelled_before_work")
    candidate = _load_model_or_reject(staging.parent.parent, model)
    if digest(candidate.get("logical")) != model.logical_digest:
        raise SafeRejectionError("model_drift")
    admitted = _admit_dataset_or_reject(staging.parent.parent, dataset)
    if admitted.pin() != dataset.pin():
        raise SafeRejectionError("dataset_drift")
    try:
        write_evaluation(staging, request, candidate)
    except ValueError as error:
        if str(error) in {
            "evaluation population is incompatible",
            "model training and held-out populations overlap",
        }:
            raise SafeRejectionError("incompatible_population") from error
        raise SafeRejectionError("malformed_artifact") from error
    _check_evaluation_replay(staging)
    _require_dataset_pin(staging.parent.parent, dataset)


def _verify(request: VerifyRequest, model: ModelPin, staging: Path, cancelled: Cancelled) -> None:
    if cancelled():
        raise SafeRejectionError("cancelled_before_work")
    candidate = _load_model_or_reject(staging.parent.parent, model)
    if digest(candidate.get("logical")) != model.logical_digest:
        raise SafeRejectionError("model_drift")
    target = staging / "verifications" / "models" / f"{request.model_id}.json"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.write_text(
        canonical_json({"active": False, "model_id": request.model_id, "verified": True}),
        encoding="utf-8",
    )


def _compare(
    comparison: dict[str, object],
    left_run_id: str,
    right_run_id: str,
    staging: Path,
    cancelled: Cancelled,
) -> None:
    if cancelled():
        raise SafeRejectionError("cancelled_before_work")
    from intelligence.model_workflows.comparison import compare

    try:
        recomputed = compare(staging.parent.parent, left_run_id, right_run_id)
    except (ValueError, OSError) as error:
        if isinstance(error, ValueError) and str(error).startswith(
            "incompatible_evaluation_contract:"
        ):
            raise SafeRejectionError("incompatible_target") from error
        raise SafeRejectionError("malformed_artifact") from error
    except RuntimeError as error:
        raise SafeRejectionError("malformed_artifact") from error
    if recomputed != comparison:
        raise SafeRejectionError("comparison_drift")
    try:
        write_comparison(staging, recomputed, left_run_id, right_run_id)
    except ValueError as error:
        raise SafeRejectionError("malformed_artifact") from error
    _check_comparison_replay(staging)


def _require_dataset_pin(workspace: Path, dataset: DatasetPin) -> None:
    """Re-admit before publication and reject either drift or malformed evidence."""
    admitted = _admit_dataset_or_reject(workspace, dataset)
    if admitted.pin() != dataset.pin():
        raise SafeRejectionError("dataset_drift")


def _admit_dataset_or_reject(workspace: Path, dataset: DatasetPin) -> AdmittedDataset:
    """Translate bounded accepted-dataset evidence failures without catching defects broadly."""
    try:
        return admit_dataset(workspace, dataset.request)
    except (ValueError, OSError, RuntimeError) as error:
        raise SafeRejectionError("malformed_artifact") from error


def _load_model_or_reject(workspace: Path, model: ModelPin) -> dict[str, object]:
    """Translate bounded State-backed candidate evidence failures before consumption."""
    try:
        return load_model(workspace, model.model_id, model.run_id)
    except (ValueError, OSError, RuntimeError) as error:
        raise SafeRejectionError("malformed_artifact") from error


def _check_train_replay(staging: Path) -> None:
    """Classify exact replay conflicts and malformed prior catalog evidence."""
    _check_replay(
        staging,
        check_staged_train_replay,
        "model replay conflicts with immutable accepted content",
    )


def _check_evaluation_replay(staging: Path) -> None:
    """Classify exact evaluation replay conflicts and malformed catalog evidence."""
    _check_replay(
        staging,
        check_staged_evaluation_replay,
        "evaluation replay conflicts with immutable accepted content",
    )


def _check_comparison_replay(staging: Path) -> None:
    """Classify exact comparison replay conflicts and malformed catalog evidence."""
    _check_replay(
        staging,
        check_staged_comparison_replay,
        "comparison replay conflicts with immutable accepted content",
    )


def _check_replay(staging: Path, check: Callable[[Path, str], object], conflict: str) -> None:
    """Map only the expected bounded artifact/replay evidence exceptions."""
    try:
        check(staging.parent.parent, staging.name)
    except RuntimeError as error:
        if str(error) == conflict:
            raise SafeRejectionError("replay_conflict") from error
        raise SafeRejectionError("malformed_artifact") from error
    except (ValueError, OSError) as error:
        raise SafeRejectionError("malformed_artifact") from error


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("comparison identity is invalid")
    return value
