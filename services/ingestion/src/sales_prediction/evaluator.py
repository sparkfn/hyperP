"""Pure-python evaluator for the #125 safe JSON model artifact (issue #125.4).

Evaluates the model artifact WITHOUT numpy — this is the production-style
evaluator that #126 will use for shadow scoring. The parity test verifies
that this evaluator reproduces training predictions within tolerance.
"""

from __future__ import annotations

import math
from typing import cast

from src.sales_prediction.model_artifact import ModelArtifact
from src.sales_prediction.models import DatasetRow

_NUMERIC_FEATURES = (
    "deal_age_days",
    "days_since_prev_event",
    "prior_transition_count",
    "prior_won_count",
    "prior_lost_count",
    "episode_index",
    "amount_value",
    "amount_known",
    "amount_nonzero",
    "assigned_known",
    "contact_count",
    "person_linked_at_s",
    "entity_version_age_days",
    "month_sin",
    "month_cos",
    "missingness_count",
)

_CATEGORICAL_FEATURES = (
    "stage_id",
    "category_id",
    "source_semantic",
    "amount_state",
    "currency_status",
)


def evaluate_row(row: DatasetRow, artifact_json: str) -> float:
    """Compute the calibrated probability for one row from the JSON artifact."""
    from src.sales_prediction.model_artifact import artifact_from_json

    artifact = artifact_from_json(artifact_json)
    features = _build_feature_vector(row, artifact)
    z = sum(w * f for w, f in zip(artifact.coefficients, features, strict=True))
    z += artifact.intercept
    raw = _sigmoid(z)
    return _sigmoid(artifact.calibration_a * _logit(raw) + artifact.calibration_b)


def evaluate_rows(rows: list[DatasetRow], artifact_json: str) -> dict[str, float]:
    """Compute calibrated probabilities for all rows, keyed by row_id."""
    return {row.row_id: evaluate_row(row, artifact_json) for row in rows}


def _build_feature_vector(row: DatasetRow, artifact: ModelArtifact) -> list[float]:
    """Build the standardized + one-hot feature vector for one row."""
    numeric = [_get_numeric(row, f) for f in _NUMERIC_FEATURES]
    standardized = [
        (v - m) / s
        for v, m, s in zip(numeric, artifact.numeric_means, artifact.numeric_stds, strict=True)
    ]
    onehot: list[float] = []
    for vocab in artifact.vocabularies:
        feature_name = str(vocab["feature_name"])
        values = cast(list[str], vocab["values"])
        row_value = _get_categorical(row, feature_name)
        for val in values:
            onehot.append(1.0 if row_value == str(val) else 0.0)
    return standardized + onehot


def _get_numeric(row: DatasetRow, feature: str) -> float:
    value = getattr(row, feature)
    if value is None:
        return 0.0
    return float(value)


def _get_categorical(row: DatasetRow, feature: str) -> str:
    value = getattr(row, feature)
    if value is None:
        return "_missing_"
    return str(value)


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _logit(p: float) -> float:
    p = max(min(p, 1.0 - 1e-15), 1e-15)
    return math.log(p / (1.0 - p))
