"""Evaluation metrics for the #125 CRM win MVP (issue #125.3).

All metrics are deterministic: precision@capacity uses row_id tie-breaking,
bootstrap uses a fixed seed, and PR AUC uses the step-function trapezoid.
No external scientific libraries are required (pure Python).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from src.sales_prediction.models import DatasetRow

_DEFAULT_CAPACITY_FRACTION = 0.10
_BOOTSTRAP_RESAMPLES = 1000
_ECE_BINS = 10


@dataclass(frozen=True)
class BinaryMetricResult:
    """Metrics for one (predictions, labels) pair."""

    precision_at_capacity: float
    lift_at_capacity: float
    pr_auc: float
    brier: float
    log_loss: float
    ece: float
    test_count: int
    positive_count: int
    capacity: int


@dataclass(frozen=True)
class BootstrapResult:
    """Bootstrap confidence interval for one metric."""

    point_estimate: float
    mean: float
    lower: float
    upper: float


def precision_at_capacity(
    rows: list[DatasetRow],
    probabilities: dict[str, float],
    *,
    capacity_fraction: float = _DEFAULT_CAPACITY_FRACTION,
) -> tuple[float, int]:
    """Precision in the top-capacity rows, ties broken by row_id.

    Returns (precision, capacity_count). Capacity is ceil(fraction * N)
    per month for monthly-fold evaluation, or ceil(fraction * N) for
    aggregate evaluation.
    """
    if not rows:
        return 0.0, 0
    capacity = max(1, math.ceil(capacity_fraction * len(rows)))
    ranked = sorted(rows, key=lambda r: (-probabilities.get(r.row_id, 0.0), r.row_id))
    selected = ranked[:capacity]
    correct = sum(1 for r in selected if r.label == 1)
    return correct / capacity, capacity


def lift_at_capacity(
    rows: list[DatasetRow],
    probabilities: dict[str, float],
    *,
    capacity_fraction: float = _DEFAULT_CAPACITY_FRACTION,
) -> float:
    """Lift = precision@capacity / base_rate. Returns 0.0 if base_rate is 0."""
    precision, _ = precision_at_capacity(rows, probabilities, capacity_fraction=capacity_fraction)
    base_rate = sum(1 for r in rows if r.label == 1) / len(rows) if rows else 0.0
    if base_rate == 0.0:
        return 0.0
    return precision / base_rate


def pr_auc(rows: list[DatasetRow], probabilities: dict[str, float]) -> float:
    """Area under the precision-recall curve (step-function trapezoid)."""
    if not rows:
        return 0.0
    ranked = sorted(rows, key=lambda r: (-probabilities.get(r.row_id, 0.0), r.row_id))
    total_pos = sum(1 for r in rows if r.label == 1)
    if total_pos == 0:
        return 0.0
    tp = 0
    fp = 0
    prev_recall = 0.0
    area = 0.0
    for r in ranked:
        if r.label == 1:
            tp += 1
        else:
            fp += 1
        precision = tp / (tp + fp)
        recall = tp / total_pos
        area += precision * (recall - prev_recall)
        prev_recall = recall
    return area


def brier_score(rows: list[DatasetRow], probabilities: dict[str, float]) -> float:
    """Mean squared error between predicted probabilities and labels."""
    if not rows:
        return 0.0
    total = sum((probabilities.get(r.row_id, 0.0) - r.label) ** 2 for r in rows)
    return total / len(rows)


def log_loss_score(rows: list[DatasetRow], probabilities: dict[str, float]) -> float:
    """Negative log-likelihood (natural log). Clipped to avoid log(0)."""
    if not rows:
        return 0.0
    total = 0.0
    for r in rows:
        p = max(min(probabilities.get(r.row_id, 0.0), 1.0 - 1e-15), 1e-15)
        total += r.label * math.log(p) + (1 - r.label) * math.log(1 - p)
    return -total / len(rows)


def expected_calibration_error(
    rows: list[DatasetRow], probabilities: dict[str, float], *, bins: int = _ECE_BINS
) -> float:
    """Expected Calibration Error: weighted average of per-bin |accuracy - confidence|."""
    if not rows:
        return 0.0
    bin_edges = [i / bins for i in range(bins + 1)]
    ece = 0.0
    for i in range(bins):
        lo, hi = bin_edges[i], bin_edges[i + 1]
        in_bin = [
            r
            for r in rows
            if lo <= probabilities.get(r.row_id, 0.0) < hi
            or (i == bins - 1 and probabilities.get(r.row_id, 0.0) == hi)
        ]
        if not in_bin:
            continue
        acc = sum(1 for r in in_bin if r.label == 1) / len(in_bin)
        conf = sum(probabilities.get(r.row_id, 0.0) for r in in_bin) / len(in_bin)
        ece += (len(in_bin) / len(rows)) * abs(acc - conf)
    return ece


def compute_binary_metrics(
    rows: list[DatasetRow],
    probabilities: dict[str, float],
    *,
    capacity_fraction: float = _DEFAULT_CAPACITY_FRACTION,
) -> BinaryMetricResult:
    """Compute all binary metrics for one (rows, probabilities) pair."""
    precision, cap = precision_at_capacity(rows, probabilities, capacity_fraction=capacity_fraction)
    base_rate = sum(1 for r in rows if r.label == 1) / len(rows) if rows else 0.0
    lift = precision / base_rate if base_rate > 0 else 0.0
    return BinaryMetricResult(
        precision_at_capacity=precision,
        lift_at_capacity=lift,
        pr_auc=pr_auc(rows, probabilities),
        brier=brier_score(rows, probabilities),
        log_loss=log_loss_score(rows, probabilities),
        ece=expected_calibration_error(rows, probabilities),
        test_count=len(rows),
        positive_count=sum(1 for r in rows if r.label == 1),
        capacity=cap,
    )


def bootstrap_metric(
    rows: list[DatasetRow],
    probabilities: dict[str, float],
    metric_fn: object,
    *,
    seed: int = 42,
    resamples: int = _BOOTSTRAP_RESAMPLES,
    capacity_fraction: float = _DEFAULT_CAPACITY_FRACTION,
) -> BootstrapResult:
    """Month-block bootstrap: resample months with replacement, recompute metric."""
    if not rows:
        return BootstrapResult(0.0, 0.0, 0.0, 0.0)
    months = sorted({r.month for r in rows})
    rows_by_month: dict[str, list[DatasetRow]] = {m: [] for m in months}
    for r in rows:
        rows_by_month[r.month].append(r)

    point = float(metric_fn(rows, probabilities, capacity_fraction=capacity_fraction))  # type: ignore[call-arg]
    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(resamples):
        sample: list[DatasetRow] = []
        for _ in range(len(months)):
            m = months[rng.randrange(len(months))]
            sample.extend(rows_by_month[m])
        val = float(metric_fn(sample, probabilities, capacity_fraction=capacity_fraction))  # type: ignore[call-arg]
        values.append(val)
    values.sort()
    mean = sum(values) / len(values)
    lower = values[int(0.025 * resamples)]
    upper = values[int(0.975 * resamples)]
    return BootstrapResult(point_estimate=point, mean=mean, lower=lower, upper=upper)
