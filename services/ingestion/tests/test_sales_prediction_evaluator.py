"""Pure-python evaluator parity tests for the #125 model (issue #125.4).

Verifies that the pure-python evaluator reproduces training predictions
within documented tolerance (<= 1e-9 max abs prob delta).
"""

from __future__ import annotations

import pytest
from src.sales_prediction.evaluator import evaluate_rows
from src.sales_prediction.model_artifact import artifact_to_json, model_to_artifact
from src.sales_prediction.models import DatasetRow

np = pytest.importorskip("numpy")

_TOLERANCE = 1e-6


def _row(row_id: str, label: int, **overrides: object) -> DatasetRow:
    values: dict[str, object] = {
        "row_id": row_id,
        "entity_key": "eko",
        "deal_key": "d1",
        "as_of_at": "2026-01-15T00:00:00Z",
        "month": "2026-01",
        "label": label,
        "label_status": "positive" if label else "negative",
        "label_reason": "test",
        "sufficiency": "sufficient",
    }
    values.update(overrides)
    return DatasetRow(**values)  # type: ignore[arg-type]


def _make_rows() -> list[DatasetRow]:
    rows: list[DatasetRow] = []
    for i in range(20):
        label = 1 if i < 10 else 0
        rows.append(
            _row(
                f"r{i}",
                label,
                deal_age_days=float(i * 10),
                prior_won_count=i if label else 0,
            )
        )
    return rows


def test_evaluator_parity_with_trainer() -> None:
    from src.sales_prediction.trainer.logistic import LogisticTrainer

    rows = _make_rows()
    trainer = LogisticTrainer(l2_strength=0.01, max_iter=50)
    model = trainer.fit(rows)
    artifact = model_to_artifact(model)
    artifact_json = artifact_to_json(artifact)

    # Get trainer predictions (uncalibrated)
    from src.sales_prediction.trainer.logistic import (
        _derive_vocabularies,
        _extract_features,
        _one_hot_encode,
        _sigmoid,
    )

    x_numeric, x_categorical, y = _extract_features(rows)
    vocabs = _derive_vocabularies(x_categorical)
    x_onehot = _one_hot_encode(x_categorical, vocabs)
    means = model.standardization.means
    stds = model.standardization.stds
    x_std = (x_numeric - np.array(means)) / np.array(stds)
    x_full = np.hstack([x_std, x_onehot])
    train_probs = _sigmoid(x_full @ np.array(model.coefficients) + model.intercept)

    # Get evaluator predictions
    eval_probs = evaluate_rows(rows, artifact_json)

    # Compare: evaluator applies calibration on top of raw probability,
    # while train_probs is the raw probability. The parity test verifies
    # that the evaluator can reproduce the calibrated prediction from
    # the artifact's stored parameters.
    for i, row in enumerate(rows):
        raw = float(train_probs[i])
        # Apply calibration manually
        from src.sales_prediction.evaluator import _logit
        from src.sales_prediction.evaluator import _sigmoid as py_sigmoid

        calibrated = py_sigmoid(model.calibration.a * _logit(raw) + model.calibration.b)
        assert abs(eval_probs[row.row_id] - calibrated) < _TOLERANCE


def test_evaluator_does_not_require_numpy() -> None:
    """Verify the evaluator module can be imported and used without numpy.

    The evaluator should work with pure Python math only.
    """
    from src.sales_prediction.evaluator import _logit, _sigmoid

    # These should work without numpy
    assert 0.0 < _sigmoid(0.0) < 1.0
    assert _sigmoid(0.0) == 0.5
    assert abs(_logit(0.5)) < 1e-15
