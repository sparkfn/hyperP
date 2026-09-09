"""Fixed CLI surface for default-off offline model controls."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping

from intelligence.config import RuntimeConfig
from intelligence.model_workflows.catalog import find as find_model
from intelligence.model_workflows.catalog import list_entries
from intelligence.model_workflows.commands import (
    ModelPin,
    comparison_registry,
    evaluation_registry,
    train_registry,
    verify_registry,
)
from intelligence.model_workflows.comparison import compare
from intelligence.model_workflows.contracts import (
    RECIPE,
    EvaluationRequest,
    TrainRequest,
    VerifyRequest,
    digest,
)
from intelligence.model_workflows.dataset_admission import admit_dataset_metadata
from intelligence.runtime import IntelligenceRuntime


def add_parser(parent: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Install top-level train/evaluate/model groups without executable escape hatches."""
    train = parent.add_parser("train").add_subparsers(dest="train_action", required=True)
    run = train.add_parser("run")
    _dataset_args(run)
    run.add_argument("--recipe", required=True, choices=(RECIPE,))
    run.add_argument("--seed", type=int, required=True)
    evaluate = parent.add_parser("evaluate").add_subparsers(dest="evaluate_action", required=True)
    rerun = evaluate.add_parser("run")
    _evaluation_args(rerun)
    compare = evaluate.add_parser("compare")
    compare.add_argument("--left-run-id", required=True)
    compare.add_argument("--right-run-id", required=True)
    model = parent.add_parser("model").add_subparsers(dest="model_action", required=True)
    listing = model.add_parser("list")
    listing.add_argument("--limit", default=50, type=int)
    inspect = model.add_parser("inspect")
    inspect.add_argument("model_id")
    inspect.add_argument("--accepted-run-id", required=True)
    verify = model.add_parser("verify")
    verify.add_argument("--model-id", required=True)
    verify.add_argument("--model-run-id", required=True)


def main(arguments: argparse.Namespace) -> int:
    """Run fixed controls; all mutation paths remain disabled unless explicitly enabled."""
    config = RuntimeConfig.from_environment()
    if arguments.command == "train":
        train_request = TrainRequest(
            arguments.dataset_id, arguments.accepted_run_id, arguments.recipe, arguments.seed
        )
        return _run(
            config,
            train_registry(admit_dataset_metadata(config.workspace, train_request)),
            "train_run",
        )
    if arguments.command == "evaluate" and arguments.evaluate_action == "run":
        evaluation_request = _evaluation_request(arguments)
        model = find_model(
            config.workspace, evaluation_request.model_id, evaluation_request.model_run_id
        )
        dataset = admit_dataset_metadata(
            config.workspace,
            TrainRequest(
                evaluation_request.dataset_id,
                evaluation_request.accepted_run_id,
                RECIPE,
                _seed(model.candidate),
            ),
        )
        return _run(
            config,
            evaluation_registry(evaluation_request, dataset, _model_pin(model)),
            "evaluate_run",
        )
    if arguments.command == "evaluate" and arguments.evaluate_action == "compare":
        return _run(
            config,
            comparison_registry(
                arguments.left_run_id,
                arguments.right_run_id,
                compare(config.workspace, arguments.left_run_id, arguments.right_run_id),
            ),
            "evaluate_compare",
        )
    if arguments.command == "model" and arguments.model_action == "verify":
        verification_request = VerifyRequest(arguments.model_id, arguments.model_run_id)
        return _run(
            config,
            verify_registry(
                verification_request,
                _model_pin(
                    find_model(
                        config.workspace,
                        verification_request.model_id,
                        verification_request.model_run_id,
                    )
                ),
            ),
            "model_verify",
        )
    if arguments.command == "model" and arguments.model_action == "inspect":
        print(
            json.dumps(
                find_model(
                    config.workspace, arguments.model_id, arguments.accepted_run_id
                ).candidate,
                sort_keys=True,
            )
        )
        return 0
    if arguments.command == "model" and arguments.model_action == "list":
        print(
            json.dumps(
                [entry.summary() for entry in list_entries(config.workspace, arguments.limit)],
                sort_keys=True,
            )
        )
        return 0
    raise ValueError("model workflow action is unsupported")


def _run(config: RuntimeConfig, registry: object, name: str) -> int:
    if not config.mutations_enabled:
        raise RuntimeError("mutating execution is disabled")
    if not isinstance(
        registry, __import__("intelligence.registry", fromlist=["Registry"]).Registry
    ):
        raise ValueError("reviewed registry is invalid")
    runtime = IntelligenceRuntime(config, registry)
    try:
        run_id = runtime.run(name)
        run = runtime.state.inspect(run_id)
        print(
            json.dumps(
                {"run_id": run_id, "state": None if run is None else run.state}, sort_keys=True
            )
        )
        return 0 if run is not None and run.state == "completed" else 1
    finally:
        runtime.close()


def _seed(model: Mapping[str, object]) -> int:
    logical = model.get("logical")
    if not isinstance(logical, dict):
        raise ValueError("model logical content is invalid")
    seed = logical.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError("model seed is invalid")
    return seed


def _model_pin(model: object) -> ModelPin:
    from intelligence.model_workflows.catalog import ModelEntry

    if not isinstance(model, ModelEntry):
        raise ValueError("model catalog entry is invalid")
    return ModelPin(
        model.model_id, model.run_id, digest(model.candidate.get("logical")), _seed(model.candidate)
    )


def _dataset_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--accepted-run-id", required=True)


def _evaluation_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--model-run-id", required=True)
    _dataset_args(parser)


def _evaluation_request(arguments: argparse.Namespace) -> EvaluationRequest:
    return EvaluationRequest(
        arguments.model_id, arguments.model_run_id, arguments.dataset_id, arguments.accepted_run_id
    )
