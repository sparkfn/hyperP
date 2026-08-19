"""Logistic trainer tests for the #125 CRM win MVP (issue #125.4).

Tests are guarded by numpy availability: if numpy is not installed,
the training tests are skipped (numpy is a training-only dependency).
"""

from __future__ import annotations

import pytest
from src.sales_prediction.models import DatasetRow

np = pytest.importorskip("numpy")


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


def _make_separable_rows() -> list[DatasetRow]:
    """Create linearly separable rows for logistic regression."""
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


def test_trainer_fits_on_separable_data() -> None:
    from src.sales_prediction.trainer.logistic import LogisticTrainer

    trainer = LogisticTrainer(l2_strength=0.01, max_iter=50)
    rows = _make_separable_rows()
    model = trainer.fit(rows)
    assert model.intercept is not None
    assert len(model.coefficients) > 0
    assert len(model.feature_order) == len(model.coefficients)
    assert model.numeric_feature_count > 0


def test_calibration_params_are_finite() -> None:
    from src.sales_prediction.trainer.logistic import LogisticTrainer

    trainer = LogisticTrainer(l2_strength=0.1, max_iter=50)
    model = trainer.fit(_make_separable_rows())
    assert np.isfinite(model.calibration.a)
    assert np.isfinite(model.calibration.b)


def test_model_artifact_roundtrip() -> None:
    from src.sales_prediction.model_artifact import (
        artifact_from_json,
        artifact_sha256,
        artifact_to_json,
        model_to_artifact,
    )
    from src.sales_prediction.trainer.logistic import LogisticTrainer

    model = LogisticTrainer(l2_strength=0.1, max_iter=50).fit(_make_separable_rows())
    artifact = model_to_artifact(model)
    json_str = artifact_to_json(artifact)
    restored = artifact_from_json(json_str)
    assert restored == artifact
    assert artifact_sha256(artifact) == artifact_sha256(restored)
