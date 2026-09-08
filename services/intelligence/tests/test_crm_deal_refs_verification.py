"""Accepted artifact verification must not depend on a later Neo4j read."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from intelligence.artifacts import canonical_json
from intelligence.crm_deal_refs import snapshot_validation
from intelligence.crm_deal_refs.checkpoints import read_json
from intelligence.crm_deal_refs.export import export_snapshot, seal_boundary, verify_snapshot
from intelligence.crm_deal_refs.models import (
    MAX_METADATA_BYTES,
    MAX_RECORD_BYTES,
    MAX_RECORDS,
    Boundary,
    canonical_digest,
)
from intelligence.crm_deal_refs.snapshot_validation import page_rows
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


def test_complete_checkpoint_limits_reject_before_snapshot_manifest_parsing(
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
    export_snapshot(tmp_path, boundary, deals, identities)
    root = tmp_path / "snapshots" / "crm" / "deal-refs"
    checkpoint_path = root / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["identity_pages"] = MAX_RECORDS
    checkpoint["identity_records"] = 1
    checkpoint_path.write_text(canonical_json(checkpoint), encoding="utf-8")
    monkeypatch.setattr(
        snapshot_validation,
        "_snapshot_manifests",
        lambda *_args: (_ for _ in ()).throw(AssertionError("manifest must not be parsed")),
    )
    with pytest.raises(ValueError, match="checkpoint limits"):
        verify_snapshot(root)


def test_snapshot_manifest_page_count_rejects_before_page_manifest_materialization(
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
    export_snapshot(tmp_path, boundary, deals, identities)
    root = tmp_path / "snapshots" / "crm" / "deal-refs"
    manifest_path = root / "snapshot-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pages = manifest["page_manifests"]
    assert isinstance(pages, list)
    pages.append(pages[0])
    manifest["page_manifests_sha256"] = canonical_digest(pages)
    manifest_path.write_text(canonical_json(manifest), encoding="utf-8")
    monkeypatch.setattr(
        snapshot_validation,
        "_manifest",
        lambda _value: (_ for _ in ()).throw(AssertionError("page manifest must not materialize")),
    )
    with pytest.raises(ValueError, match="page count"):
        verify_snapshot(root)


def test_metadata_and_ndjson_records_reject_oversize_before_materialization(tmp_path: Path) -> None:
    metadata = tmp_path / "boundary.json"
    metadata.write_bytes(b"x" * (MAX_METADATA_BYTES + 1))
    with pytest.raises(ValueError, match="size limit"):
        read_json(metadata)

    repository = FakeRepository()
    boundary, deals, identities = seal_boundary(
        repository,
        source_instance_id="instance-a",
        as_of="2026-02-01T00:00:00Z",
        page_size=1,
        max_records=2,
    )
    export_snapshot(tmp_path, boundary, deals, identities)
    page = (
        tmp_path
        / "snapshots"
        / "crm"
        / "deal-refs"
        / "pages"
        / "deal-references"
        / "page-000001.ndjson"
    )
    page.write_bytes(b"x" * (MAX_RECORD_BYTES + 1))
    with pytest.raises(ValueError, match="size limit"):
        page_rows(page, "deal-references", boundary)


def test_ndjson_crlf_newlines_are_not_canonical(tmp_path: Path) -> None:
    repository = FakeRepository()
    boundary, deals, identities = seal_boundary(
        repository,
        source_instance_id="instance-a",
        as_of="2026-02-01T00:00:00Z",
        page_size=1,
        max_records=2,
    )
    export_snapshot(tmp_path, boundary, deals, identities)
    page = (
        tmp_path
        / "snapshots"
        / "crm"
        / "deal-refs"
        / "pages"
        / "deal-references"
        / "page-000001.ndjson"
    )
    page.write_bytes(page.read_bytes().replace(b"\n", b"\r\n"))
    with pytest.raises(ValueError, match="newline"):
        page_rows(page, "deal-references", boundary)
