"""Bounded sealing, fixed-ceiling reconciliation, and page artifact contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from intelligence.crm_deal_refs import export as deal_export
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

    def iter_deal_reference_pages(self, *_args: object) -> object:
        rows = [self._deal("42", 1, "pk-1")]
        if self.extra:
            rows.append(self._deal("43", 1, "pk-2"))
        yield tuple(rows)

    def iter_identity_revision_pages(self, *_args: object) -> object:
        yield (
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
            },
        )

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
            "raw_payload_oversize": False,
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


def test_snapshot_manifest_write_limit_keeps_oversize_output_unverifiable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = FakeRepository()
    boundary, deals, identities = seal_boundary(
        repository,
        source_instance_id="instance-a",
        as_of="2026-02-01T00:00:00Z",
        page_size=1,
        max_records=2,
    )
    export_snapshot(tmp_path / "normal", boundary, deals, identities)
    assert (
        verify_snapshot(tmp_path / "normal" / "snapshots" / "crm" / "deal-refs")[
            "deal_record_count"
        ]
        == 1
    )

    monkeypatch.setattr(deal_export, "MAX_SNAPSHOT_MANIFEST_BYTES", 1)
    with pytest.raises(ValueError, match="size limit"):
        export_snapshot(tmp_path / "oversize", boundary, deals, identities)
    root = tmp_path / "oversize" / "snapshots" / "crm" / "deal-refs"
    assert not (root / "snapshot-manifest.json").exists()
    with pytest.raises(FileNotFoundError):
        verify_snapshot(root)


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
    original = repository.iter_deal_reference_pages
    repository.iter_deal_reference_pages = lambda *_args: (
        tuple((*next(original()), *next(original()))),
    )
    with pytest.raises(ValueError, match="unordered"):
        seal_boundary(
            repository,
            source_instance_id="instance-a",
            as_of="2026-02-01T00:00:00Z",
            page_size=2,
            max_records=3,
        )


def test_deal_pages_are_mapped_before_the_repository_advances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PageWiseRepository(FakeRepository):
        def __init__(self) -> None:
            super().__init__()
            self.events: list[str] = []

        def iter_deal_reference_pages(self, *_args: object) -> object:
            self.events.append("page-1")
            yield (self._deal("42", 1, "pk-1"),)
            assert self.events == ["page-1", "mapped-42"]
            self.events.append("page-2")
            yield (self._deal("43", 1, "pk-2"),)

    repository = PageWiseRepository()
    original = deal_export.map_deal_reference

    def map_page_row(row: dict[str, object], source: str, cutoff: str) -> object:
        repository.events.append(f"mapped-{row['source_entity_id']}")
        return original(row, source, cutoff)

    monkeypatch.setattr(deal_export, "map_deal_reference", map_page_row)
    boundary, deals, _ = deal_export.seal_boundary(
        repository,
        source_instance_id="instance-a",
        as_of="2026-02-01T00:00:00Z",
        page_size=1,
        max_records=3,
    )
    assert boundary.source_membership_count == 2
    assert [item.source_entity_id for item in deals] == ["42", "43"]
    assert repository.events == ["page-1", "mapped-42", "page-2", "mapped-43"]


def test_category_stage_changes_with_equal_times_preserve_both_versions() -> None:
    class VersionedRepository(FakeRepository):
        def iter_deal_reference_pages(self, *_args: object) -> object:
            first = self._deal("42", 1, "pk-1")
            second = self._deal("42", 2, "pk-2")
            payload = json.loads(str(second["raw_payload"]))
            payload["category_id"] = "2"
            payload["stage_id"] = "C2:WON"
            payload["deal"]["CATEGORY_ID"] = "2"
            payload["deal"]["STAGE_ID"] = "C2:WON"
            payload["deal"]["STAGE_SEMANTIC_ID"] = "S"
            second["stage_id"] = "C2:WON"
            second["raw_payload"] = json.dumps(payload)
            yield (first, second)

    boundary, deals, _ = seal_boundary(
        VersionedRepository(),
        source_instance_id="instance-a",
        as_of="2026-02-01T00:00:00Z",
        page_size=2,
        max_records=3,
    )
    assert boundary.source_membership_count == 2
    assert [
        (item.key.source_record_version, item.category_id, item.stage_id) for item in deals
    ] == [
        (1, "1", "C1:NEW"),
        (2, "2", "C2:WON"),
    ]
    assert deals[0].observed_at == deals[1].observed_at
