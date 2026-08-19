"""Rules and random baselines for the #125 evaluation (issue #125.3).

Pre-registered before any held-out evaluation:
  - random baseline: seeded uniform probability by row_id
  - rules-v1: train base rate (the simplest non-trivial baseline)

Thresholds (from issue #125, frozen before held-out inspection):
  - rules lift at capacity >= 1.25 over random in every eligible fold
    (eligible = >= 50 test rows & >= 5 positives)
  - logistic: >= 10% relative precision@capacity improvement over best
    rules baseline, no worse Brier, no entity with > 10% relative
    precision regression
"""

from __future__ import annotations

import random

from src.sales_prediction.models import DatasetRow

_DEFAULT_CAPACITY_FRACTION = 0.10
_RANDOM_SEED = 42


def random_baseline_probabilities(
    rows: list[DatasetRow], *, seed: int = _RANDOM_SEED
) -> dict[str, float]:
    """Seeded uniform probabilities keyed by row_id (deterministic)."""
    rng = random.Random(seed)
    return {r.row_id: rng.random() for r in rows}


def rules_v1_probabilities(
    train_rows: list[DatasetRow], test_rows: list[DatasetRow]
) -> dict[str, float]:
    """Rules-v1: assign the training base rate to every test row.

    This is the simplest non-trivial baseline: a constant probability
    equal to the fraction of positives in the training set.
    """
    if not train_rows:
        return {r.row_id: 0.0 for r in test_rows}
    base_rate = sum(1 for r in train_rows if r.label == 1) / len(train_rows)
    return {r.row_id: base_rate for r in test_rows}


def rules_lift_threshold() -> float:
    """Pre-registered minimum lift@capacity for rules to qualify."""
    return 1.25


def logistic_improvement_threshold() -> float:
    """Pre-registered minimum relative precision@capacity improvement for logistic."""
    return 0.10


def entity_regression_tolerance() -> float:
    """Pre-registered maximum relative precision regression per entity."""
    return 0.10


def min_test_rows_for_eligible_fold() -> int:
    """Minimum test rows for a fold to be eligible for lift thresholding."""
    return 50


def min_positives_for_eligible_fold() -> int:
    """Minimum positives for a fold to be eligible for lift thresholding."""
    return 5
