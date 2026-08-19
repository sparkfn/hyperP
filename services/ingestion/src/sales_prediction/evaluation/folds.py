"""Rolling-origin temporal folds for the #125 evaluation (issue #125.3).

Folds are monthly: each test month uses all prior months (minimum 3) as
training data. No deal or Person may appear on both sides of a split.
"""

from __future__ import annotations

from src.sales_prediction.models import DatasetRow, TemporalFold

_MIN_TRAIN_MONTHS = 3


def build_temporal_folds(rows: list[DatasetRow]) -> list[TemporalFold]:
    """Build rolling-origin folds from dataset rows.

    Each fold's test set is one month; training is all months before it
    that are at least _MIN_TRAIN_MONTHS months back. Deals and persons
    in the test month are excluded from training to prevent leakage.
    """
    months = sorted({row.month for row in rows})
    if len(months) < _MIN_TRAIN_MONTHS + 1:
        return []
    folds: list[TemporalFold] = []
    for i, test_month in enumerate(months):
        if i < _MIN_TRAIN_MONTHS:
            continue
        train_months = tuple(months[:i])
        test_rows = [r for r in rows if r.month == test_month]
        train_rows = [r for r in rows if r.month in train_months]
        test_deal_keys = {r.deal_key for r in test_rows}
        test_person_keys = {r.person_key for r in test_rows if r.person_key}
        clean_train = [
            r
            for r in train_rows
            if r.deal_key not in test_deal_keys
            and (r.person_key is None or r.person_key not in test_person_keys)
        ]
        folds.append(
            TemporalFold(
                test_month=test_month,
                train_months=train_months,
                train_row_ids=frozenset(r.row_id for r in clean_train),
                test_row_ids=frozenset(r.row_id for r in test_rows),
                excluded_train_deal_keys=frozenset(test_deal_keys),
                excluded_train_person_keys=frozenset(test_person_keys),
            )
        )
    return folds


def fold_rows(
    fold: TemporalFold, rows: list[DatasetRow]
) -> tuple[list[DatasetRow], list[DatasetRow]]:
    """Split rows into (train, test) for one fold."""
    train = [r for r in rows if r.row_id in fold.train_row_ids]
    test = [r for r in rows if r.row_id in fold.test_row_ids]
    return train, test
