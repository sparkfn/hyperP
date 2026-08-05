"""Tests for the discovery-only CRM stage mapping artifact."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.sales_prediction_discovery_config import load_stage_mapping


def _mapping(*, won_stage_ids: list[str] | None = None) -> dict[str, object]:
    return {
        "policy_version": "issue-124-v1",
        "claimed_approval_status": "approved",
        "external_approval_reference": "issue-124",
        "entities": [
            {
                "entity_key": "fundbox",
                "open_stage_ids": ["OPEN"],
                "won_stage_ids": ["WON"] if won_stage_ids is None else won_stage_ids,
                "lost_stage_ids": ["LOST"],
                "excluded_stage_ids": [],
                "reopen_revert_policy_status": "pending",
            }
        ],
    }


def test_mapping_hash_is_stable_and_self_declared_approval_is_unverified(tmp_path: Path) -> None:
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(_mapping()), encoding="utf-8")

    mapping = load_stage_mapping(path)

    assert len(mapping.configuration_hash) == 64
    assert mapping.approval_status == "approval_unverified"


def test_mapping_rejects_overlapping_stage_sets(tmp_path: Path) -> None:
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(_mapping(won_stage_ids=["OPEN"])), encoding="utf-8")

    with pytest.raises(ValueError, match="overlapping"):
        load_stage_mapping(path)


def test_draft_mapping_without_reference_is_accepted_but_unverified(tmp_path: Path) -> None:
    payload = _mapping()
    payload["claimed_approval_status"] = "draft"
    payload["external_approval_reference"] = None
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    mapping = load_stage_mapping(path)

    assert mapping.external_approval_reference is None
    assert mapping.approval_status == "approval_unverified"
