"""Metric math tests for the #125 evaluation (issue #125.3)."""

from __future__ import annotations

from src.sales_prediction.evaluation.metrics import (
    bootstrap_metric,
    brier_score,
    compute_binary_metrics,
    expected_calibration_error,
    lift_at_capacity,
    log_loss_score,
    pr_auc,
    precision_at_capacity,
)
from src.sales_prediction.models import DatasetRow


def _row(row_id: str, label: int) -> DatasetRow:
    return DatasetRow(
        row_id=row_id,
        entity_key="eko",
        deal_key="d1",
        as_of_at="2026-01-15T00:00:00Z",
        month="2026-01",
        label=label,
        label_status="positive" if label else "negative",
        label_reason="test",
        sufficiency="sufficient",
    )


def test_precision_at_capacity_hand_computed() -> None:
    rows = [_row("a", 1), _row("b", 0), _row("c", 1), _row("d", 0), _row("e", 0)]
    probs = {"a": 0.9, "b": 0.8, "c": 0.7, "d": 0.6, "e": 0.5}
    # capacity = ceil(0.10 * 5) = 1, top row is "a" (label=1) -> precision=1.0
    prec, cap = precision_at_capacity(rows, probs, capacity_fraction=0.10)
    assert cap == 1
    assert prec == 1.0


def test_precision_at_capacity_ties_broken_by_row_id() -> None:
    rows = [_row("a", 0), _row("b", 1)]
    probs = {"a": 0.5, "b": 0.5}
    # tie at 0.5 -> sorted by row_id ascending -> "a" first
    # capacity = ceil(0.10 * 2) = 1, top row is "a" (label=0) -> precision=0.0
    prec, cap = precision_at_capacity(rows, probs, capacity_fraction=0.10)
    assert prec == 0.0


def test_lift_at_capacity_zero_base_rate() -> None:
    rows = [_row("a", 0), _row("b", 0)]
    probs = {"a": 0.9, "b": 0.1}
    assert lift_at_capacity(rows, probs) == 0.0


def test_pr_auc_perfect_classifier() -> None:
    rows = [_row("a", 1), _row("b", 0)]
    probs = {"a": 1.0, "b": 0.0}
    assert pr_auc(rows, probs) == 1.0


def test_brier_score_hand_computed() -> None:
    rows = [_row("a", 1), _row("b", 0)]
    probs = {"a": 0.8, "b": 0.2}
    # (0.8-1)^2 + (0.2-0)^2 = 0.04 + 0.04 = 0.08, /2 = 0.04
    assert abs(brier_score(rows, probs) - 0.04) < 1e-9


def test_log_loss_finite() -> None:
    rows = [_row("a", 1), _row("b", 0)]
    probs = {"a": 0.9, "b": 0.1}
    ll = log_loss_score(rows, probs)
    assert ll > 0
    assert ll < 1.0


def test_ece_perfect_calibration() -> None:
    rows = [_row("a", 1), _row("b", 0)]
    probs = {"a": 1.0, "b": 0.0}
    # Both in the top bin; accuracy=0.5, confidence=0.5 -> ECE=0
    ece = expected_calibration_error(rows, probs, bins=10)
    # With 2 rows in top bin: acc = 1/2 = 0.5, conf = (1.0+0.0)/2 = 0.5
    assert ece == 0.0


def test_compute_binary_metrics_complete() -> None:
    rows = [_row("a", 1), _row("b", 0), _row("c", 1), _row("d", 0)]
    probs = {"a": 0.9, "b": 0.1, "c": 0.8, "d": 0.2}
    m = compute_binary_metrics(rows, probs, capacity_fraction=0.50)
    assert m.test_count == 4
    assert m.positive_count == 2
    assert m.capacity == 2
    assert 0.0 <= m.precision_at_capacity <= 1.0
    assert 0.0 <= m.brier <= 1.0


def _precision_only(
    rows: list[DatasetRow], probabilities: dict[str, float], *, capacity_fraction: float = 0.10
) -> float:
    prec, _ = precision_at_capacity(rows, probabilities, capacity_fraction=capacity_fraction)
    return prec


def test_bootstrap_deterministic() -> None:
    rows = [_row(f"r{i}", i % 2) for i in range(20)]
    probs = {f"r{i}": (i % 2) * 0.9 + 0.05 for i in range(20)}
    b1 = bootstrap_metric(rows, probs, _precision_only, seed=42, resamples=100)
    b2 = bootstrap_metric(rows, probs, _precision_only, seed=42, resamples=100)
    assert b1 == b2
    assert b1.lower <= b1.point_estimate <= b1.upper or b1.lower <= b1.mean <= b1.upper
