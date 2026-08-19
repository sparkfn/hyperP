"""Temporal fold tests for the #125 evaluation (issue #125.3)."""

from __future__ import annotations

from src.sales_prediction.evaluation.folds import build_temporal_folds, fold_rows
from src.sales_prediction.models import DatasetRow


def _row(row_id: str, month: str, deal: str = "d1", person: str | None = "p1") -> DatasetRow:
    return DatasetRow(
        row_id=row_id,
        entity_key="eko",
        deal_key=deal,
        as_of_at=f"{month}-15T00:00:00Z",
        month=month,
        label=1,
        label_status="positive",
        label_reason="test",
        sufficiency="sufficient",
        person_key=person,
    )


def test_build_folds_requires_min_train_months() -> None:
    rows = [_row("a", "2026-01"), _row("b", "2026-02"), _row("c", "2026-03")]
    assert build_temporal_folds(rows) == []


def test_build_folds_produces_rolling_splits() -> None:
    rows = [
        _row("a", "2026-01", "d1"),
        _row("b", "2026-02", "d2"),
        _row("c", "2026-03", "d3"),
        _row("d", "2026-04", "d4"),
        _row("e", "2026-05", "d5"),
    ]
    folds = build_temporal_folds(rows)
    assert len(folds) == 2
    assert folds[0].test_month == "2026-04"
    assert folds[1].test_month == "2026-05"
    assert folds[0].train_months == ("2026-01", "2026-02", "2026-03")
    assert folds[1].train_months == ("2026-01", "2026-02", "2026-03", "2026-04")


def test_fold_excludes_test_deals_from_train() -> None:
    rows = [
        _row("a", "2026-01", "d1"),
        _row("b", "2026-02", "d2"),
        _row("c", "2026-03", "d3"),
        _row("d", "2026-04", "d1"),  # same deal as test month
        _row("e", "2026-04", "d4"),
    ]
    folds = build_temporal_folds(rows)
    assert len(folds) == 1
    fold = folds[0]
    train, test = fold_rows(fold, rows)
    test_deals = {r.deal_key for r in test}
    train_deals = {r.deal_key for r in train}
    assert test_deals == {"d1", "d4"}
    assert "d1" not in train_deals


def test_fold_excludes_test_persons_from_train() -> None:
    rows = [
        _row("a", "2026-01", "d1", "p1"),
        _row("b", "2026-02", "d2", "p2"),
        _row("c", "2026-03", "d3", "p3"),
        _row("d", "2026-04", "d4", "p1"),  # same person as test
        _row("e", "2026-04", "d5", "p5"),
    ]
    folds = build_temporal_folds(rows)
    fold = folds[0]
    train, test = fold_rows(fold, rows)
    train_persons = {r.person_key for r in train if r.person_key}
    test_persons = {r.person_key for r in test if r.person_key}
    assert "p1" in test_persons
    assert "p1" not in train_persons
