"""Accepted artifact verification must not depend on a later Neo4j read."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from intelligence.artifacts import canonical_json
from intelligence.crm_deal_refs.export import export_snapshot, seal_boundary, verify_snapshot
from intelligence.crm_deal_refs.models import Boundary, canonical_digest
from test_crm_deal_refs_export import FakeRepository


def test_verify_rejects_missing_snapshot_evidence(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        verify_snapshot(tmp_path)


def test_verify_rejects_extra_and_missing_evidence(tmp_path: Path) -> None:
    repository = FakeRepository()
    boundary, deals, identities = seal_boundary(
        repository,
        source_instance_id="instance-a",
        as_of="2026-02-01T00:00:00Z",
        page_size=1,
        max_records=2,
    )
    export_snapshot(tmp_path, boundary, deals, identities)
    root = tmp_path / "snapshots" / "crm" / "deal-refs"
    (root / "unexpected.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="inventory"):
        verify_snapshot(root)
    (root / "unexpected.txt").unlink()
    (root / "boundary.json").unlink()
    with pytest.raises(FileNotFoundError):
        verify_snapshot(root)


def test_empty_snapshot_and_boundary_checkpoint_tampering(tmp_path: Path) -> None:
    repository = FakeRepository()
    boundary, _, _ = seal_boundary(
        repository,
        source_instance_id="instance-a",
        as_of="2026-02-01T00:00:00Z",
        page_size=1,
        max_records=2,
    )
    empty = Boundary(
        boundary.schema_version,
        boundary.query_version,
        boundary.source_system,
        boundary.source_instance_id,
        boundary.identity_policy_version,
        boundary.as_of,
        boundary.captured_at,
        boundary.page_size,
        boundary.max_records,
        boundary.identity_revision_ceiling,
        0,
        None,
        canonical_digest([]),
        canonical_digest([]),
        canonical_digest([]),
        canonical_digest([]),
    )
    export_snapshot(tmp_path, empty, (), ())
    root = tmp_path / "snapshots" / "crm" / "deal-refs"
    assert verify_snapshot(root)["deal_record_count"] == 0
    boundary_path = root / "boundary.json"
    value = json.loads(boundary_path.read_text(encoding="utf-8"))
    value["source_membership_count"] = 1
    boundary_path.write_text(canonical_json(value), encoding="utf-8")
    with pytest.raises(ValueError):
        verify_snapshot(root)
