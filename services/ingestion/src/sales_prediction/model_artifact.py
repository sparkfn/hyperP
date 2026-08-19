"""Safe typed JSON model artifact for the #125 logistic model (issue #125.4).

The artifact contains feature order, transforms/vocabularies, standardized
coefficients/intercept, and calibration parameters. No pickle/joblib.
Production scoring in #126 evaluates this artifact without importing numpy.

The artifact is deterministic: the same trained model always produces
the same JSON bytes and the same SHA-256.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from src.sales_prediction.contracts import MODEL_SCHEMA_VERSION
from src.sales_prediction.trainer.logistic import (
    LogisticModel,
)


@dataclass(frozen=True)
class ModelArtifact:
    """Safe JSON model artifact (no numpy/pickle)."""

    schema_version: str
    feature_order: tuple[str, ...]
    numeric_means: tuple[float, ...]
    numeric_stds: tuple[float, ...]
    vocabularies: tuple[dict[str, object], ...]
    coefficients: tuple[float, ...]
    intercept: float
    calibration_a: float
    calibration_b: float
    numeric_feature_count: int
    one_hot_feature_count: int


def model_to_artifact(model: LogisticModel) -> ModelArtifact:
    """Convert a trained LogisticModel to a safe JSON artifact."""
    return ModelArtifact(
        schema_version=MODEL_SCHEMA_VERSION,
        feature_order=model.feature_order,
        numeric_means=model.standardization.means,
        numeric_stds=model.standardization.stds,
        vocabularies=tuple(
            {"feature_name": v.feature_name, "values": list(v.values)} for v in model.vocabularies
        ),
        coefficients=model.coefficients,
        intercept=model.intercept,
        calibration_a=model.calibration.a,
        calibration_b=model.calibration.b,
        numeric_feature_count=model.numeric_feature_count,
        one_hot_feature_count=model.one_hot_feature_count,
    )


def artifact_to_json(artifact: ModelArtifact) -> str:
    """Serialize the artifact to canonical JSON (deterministic)."""
    return json.dumps(
        _artifact_to_dict(artifact), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def artifact_sha256(artifact: ModelArtifact) -> str:
    """SHA-256 over the canonical artifact JSON."""
    return hashlib.sha256(artifact_to_json(artifact).encode("utf-8")).hexdigest()


def artifact_from_json(data: str) -> ModelArtifact:
    """Deserialize an artifact from canonical JSON."""
    d = json.loads(data)
    return ModelArtifact(
        schema_version=d["schema_version"],
        feature_order=tuple(d["feature_order"]),
        numeric_means=tuple(d["numeric_means"]),
        numeric_stds=tuple(d["numeric_stds"]),
        vocabularies=tuple(d["vocabularies"]),
        coefficients=tuple(d["coefficients"]),
        intercept=d["intercept"],
        calibration_a=d["calibration_a"],
        calibration_b=d["calibration_b"],
        numeric_feature_count=d["numeric_feature_count"],
        one_hot_feature_count=d["one_hot_feature_count"],
    )


def _artifact_to_dict(artifact: ModelArtifact) -> dict[str, object]:
    return {
        "schema_version": artifact.schema_version,
        "feature_order": list(artifact.feature_order),
        "numeric_means": list(artifact.numeric_means),
        "numeric_stds": list(artifact.numeric_stds),
        "vocabularies": list(artifact.vocabularies),
        "coefficients": list(artifact.coefficients),
        "intercept": artifact.intercept,
        "calibration_a": artifact.calibration_a,
        "calibration_b": artifact.calibration_b,
        "numeric_feature_count": artifact.numeric_feature_count,
        "one_hot_feature_count": artifact.one_hot_feature_count,
    }
