from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.crm_stage_mapping import (
    CrmStageInventoryRow,
    CrmStageTuple,
    build_mapping_report,
    load_mapping_policy,
)


def _payload() -> dict[str, object]:
    return {
        "mapping_version": "stage-map-v1",
        "policy_version": "stage-policy-v1",
        "entries": [
            {
                "entity_type_id": "2",
                "category_id": "0",
                "stage_id": "WON",
                "source_semantic": "S",
                "mapped_state": "won",
                "reason": "Bitrix success terminal",
            },
            {
                "entity_type_id": "2",
                "category_id": "0",
                "stage_id": "NEW",
                "source_semantic": None,
                "mapped_state": "open",
                "reason": "Active pipeline stage",
            },
        ],
        "lifecycle": {
            "first_won": "earliest_effective_won",
            "repeated_won": "retain_all_first_is_conversion",
            "reopen": "open_after_won_reopens",
            "revert": "later_effective_state_wins",
            "category_migration": "preserve_event_category",
            "equal_time": "authority_sequence_then_history_id",
        },
    }


def test_mapping_policy_is_versioned_deterministic_and_complete(tmp_path: Path) -> None:
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(_payload()), encoding="utf-8")
    first = load_mapping_policy(path)
    second = load_mapping_policy(path)
    assert first.digest == second.digest
    inventory = (
        CrmStageInventoryRow(
            stage=CrmStageTuple("2", "0", "NEW", None),
            observation_count=7,
            event_identity_count=7,
            first_event_at=datetime(2026, 1, 1, tzinfo=UTC),
            last_event_at=datetime(2026, 2, 1, tzinfo=UTC),
            effective_count=7,
            withheld_count=0,
        ),
        CrmStageInventoryRow(
            stage=CrmStageTuple("2", "0", "WON", "S"),
            observation_count=3,
            event_identity_count=3,
            first_event_at=datetime(2026, 1, 2, tzinfo=UTC),
            last_event_at=datetime(2026, 2, 2, tzinfo=UTC),
            effective_count=3,
            withheld_count=0,
        ),
    )
    report = build_mapping_report(inventory, first)
    assert report.complete is True
    assert report.observed_tuple_count == 2
    assert report.mapped_tuple_count == 2
    assert [row.mapped_state for row in report.rows] == ["open", "won"]


def test_mapping_report_fails_closed_for_unmapped_observed_tuple(tmp_path: Path) -> None:
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(_payload()), encoding="utf-8")
    policy = load_mapping_policy(path)
    inventory = (
        CrmStageInventoryRow(
            stage=CrmStageTuple("2", "9", "UNKNOWN", None),
            observation_count=1,
            event_identity_count=1,
            first_event_at=datetime(2026, 1, 1, tzinfo=UTC),
            last_event_at=datetime(2026, 1, 1, tzinfo=UTC),
            effective_count=0,
            withheld_count=1,
        ),
    )
    report = build_mapping_report(inventory, policy)
    assert report.complete is False
    assert report.mapped_tuple_count == 0


def test_mapping_policy_rejects_duplicate_tuple(tmp_path: Path) -> None:
    payload = _payload()
    entries = payload["entries"]
    assert isinstance(entries, list)
    entries.append(dict(entries[0]))
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate tuples"):
        load_mapping_policy(path)
