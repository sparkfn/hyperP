"""Admission helpers joining staged model bundles to the verified immutable catalog."""

from __future__ import annotations

from pathlib import Path

from intelligence.model_workflows.artifacts import (
    ComparisonAcceptanceDescriptor,
    EvaluationAcceptanceDescriptor,
    TrainBundle,
    verify_comparison_bundle,
    verify_evaluation_bundle,
    verify_train_bundle,
)
from intelligence.model_workflows.catalog import (
    ComparisonEntry,
    EvaluationEntry,
    ModelEntry,
    check_comparison_replay_conflict,
    check_evaluation_replay_conflict,
    check_model_replay_conflict,
    find,
    find_comparison,
    find_evaluation,
)
from intelligence.model_workflows.codec import JsonValue
from intelligence.model_workflows.path_safety import staged_run_root


def verify_staged_train(workspace: Path, run_id: str) -> TrainBundle:
    """Validate a complete staging bundle before runtime publication can begin."""
    return verify_train_bundle(staged_run_root(workspace, run_id), run_id)


def verify_staged_evaluation(
    workspace: Path, run_id: str
) -> tuple[dict[str, JsonValue], EvaluationAcceptanceDescriptor]:
    """Validate a complete independent-evaluation staging bundle before publication."""
    evaluation, descriptor = verify_evaluation_bundle(staged_run_root(workspace, run_id), run_id)
    return evaluation, descriptor


def verify_staged_comparison(
    workspace: Path, run_id: str
) -> tuple[dict[str, JsonValue], ComparisonAcceptanceDescriptor]:
    """Validate a complete comparison staging bundle before publication."""
    comparison, descriptor = verify_comparison_bundle(staged_run_root(workspace, run_id), run_id)
    return comparison, descriptor


def check_staged_train_replay(workspace: Path, run_id: str) -> TrainBundle:
    """Reject a staged train replay before publication could create conflicting evidence."""
    bundle = verify_staged_train(workspace, run_id)
    check_model_replay_conflict(workspace, bundle)
    return bundle


def check_staged_evaluation_replay(
    workspace: Path, run_id: str
) -> tuple[dict[str, JsonValue], EvaluationAcceptanceDescriptor]:
    """Reject an immutable evaluation replay that conflicts with prior State evidence."""
    evaluation, descriptor = verify_staged_evaluation(workspace, run_id)
    check_evaluation_replay_conflict(workspace, descriptor, evaluation)
    return evaluation, descriptor


def check_staged_comparison_replay(
    workspace: Path, run_id: str
) -> tuple[dict[str, JsonValue], ComparisonAcceptanceDescriptor]:
    """Reject an immutable comparison replay that conflicts with prior State evidence."""
    comparison, descriptor = verify_staged_comparison(workspace, run_id)
    check_comparison_replay_conflict(workspace, descriptor, comparison)
    return comparison, descriptor


def admit_model(workspace: Path, model_id: str, run_id: str) -> ModelEntry:
    """Return only a terminal-manifest and State-backed inactive model candidate."""
    return find(workspace, model_id, run_id)


def admit_evaluation(workspace: Path, evaluation_id: str, run_id: str) -> EvaluationEntry:
    """Return only a terminal-manifest and State-backed evaluation publication."""
    return find_evaluation(workspace, evaluation_id, run_id)


def admit_comparison(workspace: Path, comparison_id: str, run_id: str) -> ComparisonEntry:
    """Return only a terminal-manifest and State-backed comparison publication."""
    return find_comparison(workspace, comparison_id, run_id)
