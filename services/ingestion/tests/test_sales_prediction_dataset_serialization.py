"""Determinism and round-trip tests for dataset serialization (issue #125)."""

from __future__ import annotations

from pathlib import Path

import pytest
from src.sales_prediction.dataset_serialization import (
    content_digest,
    read_dataset_metadata,
    read_dataset_rows,
    write_dataset,
)
from src.sales_prediction.models import DatasetRow


def _row(row_id: str, **overrides: object) -> DatasetRow:
    values: dict[str, object] = {
        "row_id": row_id,
        "entity_key": "eko",
        "deal_key": "deal-2",
        "as_of_at": "2026-01-10T08:00:00Z",
        "month": "2026-01",
        "label": 1,
        "label_status": "positive",
        "label_reason": "first_won_in_horizon",
        "sufficiency": "sufficient",
        "person_key": "person-1",
        "stage_id": "C5:NEW",
        "category_id": "5",
        "source_semantic": "S",
        "deal_age_days": 12.5,
        "days_since_prev_event": 3.0,
        "prior_transition_count": 2,
        "prior_won_count": 1,
        "prior_lost_count": 0,
        "episode_index": 2,
        "amount_value": 1200.5,
        "amount_state": "known",
        "currency_status": "supported",
        "currency": "SGD",
        "amount_known": 1,
        "amount_nonzero": 1,
        "assigned_known": 1,
        "contact_count": 2,
        "person_linked_at_s": 1,
        "entity_version_age_days": 5.0,
        "month_sin": 0.5,
        "month_cos": 0.87,
        "missingness_count": 0,
    }
    values.update(overrides)
    return DatasetRow(**values)  # type: ignore[arg-type]


def test_write_dataset_is_deterministic_across_builds(tmp_path: Path) -> None:
    rows = [_row("b-row"), _row("a-row")]
    first = write_dataset(tmp_path / "one.sqlite3", {"schema": "issue-125-crm-dataset-v1"}, rows)
    second = write_dataset(tmp_path / "two.sqlite3", {"schema": "issue-125-crm-dataset-v1"}, rows)
    assert first == second
    assert first.content_digest == content_digest(rows)
    assert first.row_count == 2
    assert (tmp_path / "one.sqlite3").read_bytes() == (tmp_path / "two.sqlite3").read_bytes()


def test_write_dataset_inserts_in_row_id_order(tmp_path: Path) -> None:
    write_dataset(tmp_path / "dataset.sqlite3", {}, [_row("z"), _row("a"), _row("m")])
    rows = read_dataset_rows(tmp_path / "dataset.sqlite3")
    assert [row.row_id for row in rows] == ["a", "m", "z"]


def test_write_dataset_round_trips_values(tmp_path: Path) -> None:
    write_dataset(
        tmp_path / "dataset.sqlite3",
        {"mapping_version": "crm-stage-map-2026-08-18-v1"},
        [_row("a", amount_value=None, amount_state="not_reconstructable")],
    )
    rows = read_dataset_rows(tmp_path / "dataset.sqlite3")
    assert rows[0] == _row("a", amount_value=None, amount_state="not_reconstructable")
    metadata = read_dataset_metadata(tmp_path / "dataset.sqlite3")
    assert metadata == {"mapping_version": "crm-stage-map-2026-08-18-v1"}


def test_write_dataset_rejects_duplicate_row_ids(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unique row IDs"):
        write_dataset(tmp_path / "dataset.sqlite3", {}, [_row("a"), _row("a")])
