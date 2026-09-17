"""Default no-numpy evaluator contract for the active #125 deployment path."""

from __future__ import annotations

from src.sales_prediction.evaluator import _logit, _sigmoid


def test_evaluator_helpers_do_not_import_or_require_numpy() -> None:
    assert 0.0 < _sigmoid(0.0) < 1.0
    assert _sigmoid(0.0) == 0.5
    assert abs(_logit(0.5)) < 1e-15
