"""Regularized logistic regression via Newton/IRLS with L2 (issue #125.4).

Uses numpy only at training time (guarded import). The trained model is
exported as safe typed JSON (coefficients, intercept, standardization
stats, calibration params) — no pickle/joblib. Production scoring in #126
evaluates the JSON artifact without importing numpy.

The trainer implements:
  - Standardization of numeric features (mean/std from training data)
  - One-hot encoding of categorical features (train-window vocabulary)
  - L2-regularized logistic regression via Newton's method (IRLS)
  - Platt scaling calibration from out-of-fold train predictions
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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


@dataclass(frozen=True)
class StandardizationStats:
    """Per-feature mean and std for numeric standardization."""

    means: tuple[float, ...]
    stds: tuple[float, ...]


@dataclass(frozen=True)
class VocabularyMapping:
    """One-hot vocabulary for categorical features."""

    feature_name: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class CalibrationParams:
    """Platt scaling parameters: sigmoid(A * z + B)."""

    a: float
    b: float


@dataclass(frozen=True)
class LogisticModel:
    """Trained logistic model ready for JSON artifact export."""

    feature_order: tuple[str, ...]
    standardization: StandardizationStats
    vocabularies: tuple[VocabularyMapping, ...]
    coefficients: tuple[float, ...]
    intercept: float
    calibration: CalibrationParams
    numeric_feature_count: int
    one_hot_feature_count: int


@dataclass
class LogisticTrainer:
    """L2-regularized logistic regression trainer (Newton/IRLS).

    ``l2_strength`` controls regularization; higher = more shrinkage.
    ``max_iter`` limits Newton iterations; ``tol`` is the convergence
    threshold on the log-likelihood change.
    """

    l2_strength: float = 1.0
    max_iter: int = 100
    tol: float = 1e-6
    _model: LogisticModel | None = field(default=None, repr=False)

    def fit(self, rows: list[DatasetRow]) -> LogisticModel:
        """Fit the logistic model on training rows."""
        try:
            import numpy as np
        except ImportError as e:
            raise ImportError(
                "numpy is required for training; install with: uv sync --group training"
            ) from e

        x_numeric, x_categorical, y = _extract_features(rows)
        vocabularies = _derive_vocabularies(x_categorical)
        x_onehot = _one_hot_encode(x_categorical, vocabularies)

        means = tuple(float(np.mean(x_numeric, axis=0)))
        stds = tuple(float(np.std(x_numeric, axis=0)) + 1e-8 for _ in range(x_numeric.shape[1]))
        stats = StandardizationStats(means=means, stds=stds)

        x_std = (x_numeric - np.array(means)) / np.array(stds)
        x_full = np.hstack([x_std, x_onehot])
        n_features = x_full.shape[1]

        weights = np.zeros(n_features)
        bias = 0.0

        for _ in range(self.max_iter):
            z = x_full @ weights + bias
            p = _sigmoid(z)
            grad = x_full.T @ (p - y) + self.l2_strength * weights
            grad_bias = float(np.sum(p - y))

            w_diag = p * (1 - p)
            w_diag = np.clip(w_diag, 1e-10, 1.0 - 1e-10)
            hessian = x_full.T @ (x_full * w_diag[:, None]) + self.l2_strength * np.eye(n_features)

            try:
                delta = np.linalg.solve(hessian, grad)
            except np.linalg.LinAlgError:
                delta = np.linalg.lstsq(hessian, grad, rcond=None)[0]

            new_weights = weights - delta
            new_bias = bias - grad_bias / max(np.sum(w_diag), 1.0)

            ll_old = _log_likelihood(y, _sigmoid(x_full @ weights + bias))
            ll_new = _log_likelihood(y, _sigmoid(x_full @ new_weights + new_bias))

            weights = new_weights
            bias = new_bias

            if abs(ll_new - ll_old) < self.tol:
                break

        train_probs = _sigmoid(x_full @ weights + bias)
        calibration = _platt_calibration(train_probs, y)

        feature_order = _NUMERIC_FEATURES + tuple(
            f"{v.feature_name}={val}" for v in vocabularies for val in v.values
        )

        model = LogisticModel(
            feature_order=feature_order,
            standardization=stats,
            vocabularies=vocabularies,
            coefficients=tuple(float(w) for w in weights),
            intercept=float(bias),
            calibration=calibration,
            numeric_feature_count=len(_NUMERIC_FEATURES),
            one_hot_feature_count=len(feature_order) - len(_NUMERIC_FEATURES),
        )
        self._model = model
        return model

    @property
    def model(self) -> LogisticModel | None:
        return self._model


def _extract_features(
    rows: list[DatasetRow],
) -> tuple[Any, Any, Any]:
    """Extract numeric features, categorical features, and labels."""
    try:
        import numpy as np
    except ImportError as e:
        raise ImportError("numpy is required for training") from e

    numeric = np.array(
        [[_get_numeric(r, f) for f in _NUMERIC_FEATURES] for r in rows],
        dtype=float,
    )
    categorical = [[_get_categorical(r, f) for f in _CATEGORICAL_FEATURES] for r in rows]
    labels = np.array([r.label for r in rows], dtype=float)
    return numeric, categorical, labels


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


def _derive_vocabularies(categorical: list[list[str]]) -> tuple[VocabularyMapping, ...]:
    """Derive one-hot vocabularies from training data."""
    vocabs: list[VocabularyMapping] = []
    for i, feature_name in enumerate(_CATEGORICAL_FEATURES):
        values = sorted({row[i] for row in categorical})
        vocabs.append(VocabularyMapping(feature_name=feature_name, values=tuple(values)))
    return tuple(vocabs)


def _one_hot_encode(
    categorical: list[list[str]], vocabularies: tuple[VocabularyMapping, ...]
) -> Any:
    """One-hot encode categorical features using train vocabularies."""
    try:
        import numpy as np
    except ImportError as e:
        raise ImportError("numpy is required for training") from e

    total_cols = sum(len(v.values) for v in vocabularies)
    encoded = np.zeros((len(categorical), total_cols), dtype=float)
    col = 0
    for i, vocab in enumerate(vocabularies):
        value_to_idx = {val: j for j, val in enumerate(vocab.values)}
        for row_idx, row in enumerate(categorical):
            j = value_to_idx.get(row[i])
            if j is not None:
                encoded[row_idx, col + j] = 1.0
        col += len(vocab.values)
    return encoded


def _sigmoid(z: Any) -> Any:
    try:
        import numpy as np
    except ImportError as e:
        raise ImportError("numpy is required for training") from e
    return np.where(z >= 0, 1.0 / (1.0 + np.exp(-z)), np.exp(z) / (1.0 + np.exp(z)))


def _log_likelihood(y: Any, p: Any) -> float:
    try:
        import numpy as np
    except ImportError as e:
        raise ImportError("numpy is required for training") from e
    eps = 1e-15
    p = np.clip(p, eps, 1.0 - eps)
    return float(np.sum(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _platt_calibration(probs: Any, labels: Any) -> CalibrationParams:
    """Fit Platt scaling: P(y=1|f) = sigmoid(A * f + B)."""
    try:
        import numpy as np
    except ImportError as e:
        raise ImportError("numpy is required for training") from e

    probs = np.clip(probs, 1e-10, 1.0 - 1e-10)
    z = np.log(probs / (1.0 - probs))

    prior1 = float(np.sum(labels))
    prior0 = float(len(labels) - prior1)
    target = (labels * (prior1 + 1.0) - 1.0) / (prior1 + 2.0)
    target += (1.0 - labels) * (prior0 + 1.0) / (prior0 + 2.0)

    a, b = 0.0, float(np.log((prior0 + 1.0) / (prior1 + 1.0)))
    for _ in range(100):
        p = 1.0 / (1.0 + np.exp(-(a * z + b)))
        grad_a = float(np.sum((target - p) * z))
        grad_b = float(np.sum(target - p))
        w = p * (1.0 - p)
        hess_aa = float(np.sum(w * z * z))
        hess_ab = float(np.sum(w * z))
        hess_bb = float(np.sum(w))
        det = hess_aa * hess_bb - hess_ab * hess_ab
        if abs(det) < 1e-12:
            break
        da = (grad_b * hess_ab - grad_a * hess_bb) / det
        db = (grad_a * hess_ab - grad_b * hess_aa) / det
        a += da
        b += db
        if abs(da) + abs(db) < 1e-8:
            break

    return CalibrationParams(a=float(a), b=float(b))
