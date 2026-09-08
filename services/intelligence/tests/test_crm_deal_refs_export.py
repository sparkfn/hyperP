"""Bounded sealing, fixed-ceiling reconciliation, and page artifact contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from intelligence.crm_deal_refs.export import (
    capture_matching_boundary,
    export_snapshot,
    seal_boundary,
    verify_snapshot,
)
from intelligence.crm_deal_refs.models import IDENTITY_POLICY_VERSION


class FakeRepository:
    def __init__(self) -> None:
        self.current_revision = 2
        self.lifecycle = "active"
        self.extra = False

    def close(self) -> None:
        return None

    def validate_source_instance(self, source_instance_id: str) -> None:
        assert source_instance_id == "instance-a"

    def read_identity_boundary(self) -> dict[str, object]:
        return {"current_revision": self.current_revision, "baseline_ready": True}

    def list_deal_references(self, *_args: object) -> list[dict[str, object]]:
        rows = [self._deal("42", 1, "pk-1")]
        if self.extra:
            rows.append(self._deal("43", 1, "pk-2"))
        return rows

    def list_identity_revisions(self, *_args: object) -> list[dict[str, object]]:
        return [
            {
                "event_id": "event-1",
                "global_revision": 1,
                "source_instance_id": "instance-a",
                "source_entity_id": "42",
                "identity_policy_version": IDENTITY_POLICY_VERSION,
                "link_status": "unresolved",
                "hyperp_person_id": None,
                "person_status": None,
                "resolution_kind": "baseline",
                "resolution_revision": 1,
                "effective_at": "2026-01-01T00:00:00Z",
                "created_at": "2026-01-02T00:00:00Z",
            }
        ]

    def _deal(self, deal_id: str, version: int, pk: str) -> dict[str, object]:
        return {
            "source_record_id": f"bitrix-crm-deal-{deal_id}",
            "source_record_version": version,
            "source_record_pk": pk,
            "record_hash": "a" * 64,
            "source_entity_type": "deal",
            "source_entity_id": deal_id,
            "identity_policy_version": IDENTITY_POLICY_VERSION,
            "record_entity_key": None,
            "owned_entity_keys": [],
            "owned_entity_count": 0,
            "stage_id": "C1:NEW",
            "observed_at": "2026-01-01T00:00:00Z",
            "ingested_at": "2026-01-02T00:00:00Z",
            "lifecycle_status": self.lifecycle,
            "link_status": "unresolved",
            "raw_payload": json.dumps(
                {
                    "crm_deal_id": deal_id,
                    "category_id": "1",
                    "stage_id": "C1:NEW",
                    "crm_deal_identity_policy_version": IDENTITY_POLICY_VERSION,
                    "deal": {
                        "ID": deal_id,
                        "CATEGORY_ID": "1",
                        "STAGE_ID": "C1:NEW",
                        "DATE_CREATE": "2026-01-01T00:00:00Z",
                        "DATE_MODIFY": "2026-01-01T00:00:00Z",
                    },
                }
            ),
        }


def test_fixed_ceiling_ignores_unrelated_later_revision_and_detects_selected_drift(
    tmp_path: Path,
) -> None:
    repository = FakeRepository()
    boundary, deals, identities = seal_boundary(
        repository,
        source_instance_id="instance-a",
        as_of="2026-02-01T00:00:00Z",
        page_size=1,
        max_records=2,
    )
    repository.current_revision = 99
    matched_deals, _ = capture_matching_boundary(repository, boundary)
    assert matched_deals == deals
    export_snapshot(tmp_path, boundary, deals, identities)
    assert verify_snapshot(tmp_path / "snapshots" / "crm" / "deal-refs")["deal_record_count"] == 1
    repository.lifecycle = "superseded"
    with pytest.raises(RuntimeError, match="drifted"):
        capture_matching_boundary(repository, boundary)


def test_new_membership_and_total_limit_overflow_fail_closed() -> None:
    repository = FakeRepository()
    boundary, _, _ = seal_boundary(
        repository,
        source_instance_id="instance-a",
        as_of="2026-02-01T00:00:00Z",
        page_size=1,
        max_records=2,
    )
    repository.extra = True
    with pytest.raises(RuntimeError, match="drifted"):
        capture_matching_boundary(repository, boundary)
    with pytest.raises(RuntimeError, match="max-records"):
        seal_boundary(
            repository,
            source_instance_id="instance-a",
            as_of="2026-02-01T00:00:00Z",
            page_size=1,
            max_records=1,
        )


def test_duplicate_source_versions_fail_closed() -> None:
    repository = FakeRepository()
    original = repository.list_deal_references
    repository.list_deal_references = lambda *_args: [*original(), *original()]
    with pytest.raises(ValueError, match="unordered"):
        seal_boundary(
            repository,
            source_instance_id="instance-a",
            as_of="2026-02-01T00:00:00Z",
            page_size=2,
            max_records=3,
        )
