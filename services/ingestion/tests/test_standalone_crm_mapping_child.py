"""Mapping child topology and strict cross-payload parsing checks."""

from __future__ import annotations

import pytest
from src.standalone_crm_mapping_child import MAPPING_CHILD_TASK_NAME, parse_mapping_publication
from src.standalone_crm_source_child_authority import parse_publication_payload


def _mapping_payload() -> dict[str, object]:
    return {
        "census_id": "census-a",
        "generation": 1,
        "stream_kind": "contact",
        "frozen_upper_id": None,
        "revision_id": "revision-a",
        "task_name": MAPPING_CHILD_TASK_NAME,
        "task_id": "task-a",
        "queue": "ingestion",
        "payload_version": "standalone-crm-child-v1",
    }


def test_mapping_child_rejects_source_payload_before_runtime_construction() -> None:
    payload = _mapping_payload()
    payload["frozen_upper_id"] = 10
    payload["revision_id"] = None
    payload["task_name"] = "src.standalone_crm_census_tasks.run_standalone_crm_census_unit"
    with pytest.raises(ValueError, match="source"):
        parse_mapping_publication(payload)


def test_source_child_rejects_mapping_payload_before_client_construction() -> None:
    with pytest.raises(ValueError, match="mapping"):
        parse_publication_payload(_mapping_payload())


def test_mapping_child_accepts_only_its_exact_registered_payload() -> None:
    payload_json, envelope = parse_mapping_publication(_mapping_payload())
    assert envelope.task_name == MAPPING_CHILD_TASK_NAME
    assert envelope.frozen_upper_id is None
    assert '"revision_id":"revision-a"' in payload_json


def test_generation_zero_mapping_activation_preserves_deterministic_absent_heads() -> None:
    from src.standalone_crm_census_requests import MappingPrepareAuthority

    authority = MappingPrepareAuthority(
        "revision-a",
        "sha256:" + "a" * 64,
        "mapping-head-a",
        "release-a",
        "sha256:" + "b" * 64,
        None,
        None,
        None,
        "projection-head-a",
        None,
        None,
        None,
    )
    assert authority.expected_current_head_id == "mapping-head-a"
    assert authority.expected_projection_head_id == "projection-head-a"
