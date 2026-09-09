"""Deterministic, artifact-bound evaluation comparison."""

from __future__ import annotations

import os
from pathlib import Path

from intelligence.artifacts import canonical_json, sha256_file
from intelligence.model_workflows.artifacts import (
    COMPARISON_DESCRIPTOR_SCHEMA,
    descriptor_value,
)
from intelligence.model_workflows.catalog import evaluation_for_run
from intelligence.model_workflows.codec import JsonValue
from intelligence.model_workflows.contracts import COMPARISON_SCHEMA, digest


def compare(workspace: Path, left_run_id: str, right_run_id: str) -> dict[str, object]:
    """Compare two exact State-accepted evaluation outputs or reject incompatibility."""
    left_entry = evaluation_for_run(workspace, left_run_id)
    right_entry = evaluation_for_run(workspace, right_run_id)
    left_logical = _mapping(left_entry.evaluation.get("logical"))
    right_logical = _mapping(right_entry.evaluation.get("logical"))
    keys = ("population_digest", "labels", "metrics_contract")
    mismatches = tuple(key for key in keys if left_logical.get(key) != right_logical.get(key))
    if mismatches:
        raise ValueError("incompatible_evaluation_contract:" + ",".join(mismatches))
    if left_logical.get("labels") != ["lost", "open", "won"]:
        raise ValueError("incompatible_evaluation_contract:labels")
    logical = {
        "left_evaluation_id": left_entry.evaluation_id,
        "left_metrics": left_logical.get("metrics"),
        "metrics_contract": left_logical.get("metrics_contract"),
        "population_digest": left_logical.get("population_digest"),
        "right_evaluation_id": right_entry.evaluation_id,
        "right_metrics": right_logical.get("metrics"),
    }
    return {
        "comparison_id": f"comparison-{digest(logical)[:24]}",
        "logical": logical,
        "schema_version": COMPARISON_SCHEMA,
    }


def write(staging: Path, value: dict[str, object], left_run_id: str, right_run_id: str) -> None:
    """Write one canonical no-overwrite comparison report in current staging only."""
    identifier = value.get("comparison_id")
    if not isinstance(identifier, str):
        raise ValueError("comparison identity is invalid")
    target = staging / "comparisons" / identifier / "comparison.json"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(value))
        handle.flush()
        os.fsync(handle.fileno())
    logical = value.get("logical")
    if not isinstance(logical, dict):
        raise ValueError("comparison logical content is invalid")
    left_id, right_id = logical.get("left_evaluation_id"), logical.get("right_evaluation_id")
    if not isinstance(left_id, str) or not isinstance(right_id, str):
        raise ValueError("comparison evaluation identities are invalid")
    report = staging / "comparisons" / identifier / "comparison.json"
    fields: dict[str, JsonValue] = {
        "command": "evaluate_compare",
        "comparison_id": identifier,
        "comparison_logical_digest": digest(logical),
        "inventory": [
            {
                "byte_count": report.stat().st_size,
                "relative_path": f"comparisons/{identifier}/comparison.json",
                "sha256": sha256_file(report),
            }
        ],
        "left_evaluation_id": left_id,
        "left_run_id": left_run_id,
        "request_digest": digest(
            {
                "left_evaluation_id": left_id,
                "left_run_id": left_run_id,
                "right_evaluation_id": right_id,
                "right_run_id": right_run_id,
            }
        ),
        "right_evaluation_id": right_id,
        "right_run_id": right_run_id,
        "run_id": staging.name,
    }
    target = staging / "acceptance-descriptors" / "comparisons" / f"{identifier}.json"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with os.fdopen(
        os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w", encoding="utf-8"
    ) as handle:
        handle.write(canonical_json(descriptor_value(COMPARISON_DESCRIPTOR_SCHEMA, fields)))
        handle.flush()
        os.fsync(handle.fileno())


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("evaluation logical content is invalid")
    return value
