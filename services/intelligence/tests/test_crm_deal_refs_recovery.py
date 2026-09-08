"""Committed-prefix replay and recovery evidence tests."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from intelligence.artifacts import canonical_json, sha256_file
from intelligence.crm_deal_refs import snapshot_validation
from intelligence.crm_deal_refs.checkpoints import replace_json
from intelligence.crm_deal_refs.commands import accepted_snapshot_root, partial_snapshot_root
from intelligence.crm_deal_refs.export import (
    export_snapshot,
    resume_snapshot,
    seal_boundary,
    verify_snapshot,
)
from intelligence.crm_deal_refs.models import MAX_RECORDS, json_value
from intelligence.crm_deal_refs.snapshot_validation import read_checkpoint
from test_crm_deal_refs_export import FakeRepository


def _partial(tmp_path: Path) -> tuple[Path, object, tuple[object, ...], tuple[object, ...], bytes]:
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
    deal_bytes = (root / "pages" / "deal-references" / "page-000001.ndjson").read_bytes()
    (root / "snapshot-manifest.json").unlink()
    shutil.rmtree(root / "pages" / "identity-revisions")
    shutil.rmtree(root / "manifests" / "identity-revisions")
    digest = __import__(
        "intelligence.crm_deal_refs.models", fromlist=["canonical_digest"]
    ).canonical_digest(json_value(boundary))
    checkpoint = read_checkpoint(root / "checkpoint.json", digest)
    replace_json(
        root / "checkpoint.json",
        json_value(
            replace(
                checkpoint,
                identity_pages=0,
                identity_records=0,
                identity_next_cursor=None,
                completed=False,
            )
        ),
    )
    return root, boundary, deals, identities, deal_bytes


def test_partial_one_page_prefix_resumes_to_completed_snapshot(tmp_path: Path) -> None:
    source, boundary, deals, identities, prefix = _partial(tmp_path / "source")
    resume_snapshot(tmp_path / "target", source, boundary, deals, identities)
    completed = tmp_path / "target" / "snapshots" / "crm" / "deal-refs"
    assert verify_snapshot(completed)["deal_record_count"] == 1
    assert (completed / "pages" / "deal-references" / "page-000001.ndjson").read_bytes() == prefix


def test_rehashed_changed_prefix_and_one_sided_sidecar_fail(tmp_path: Path) -> None:
    source, boundary, deals, identities, _ = _partial(tmp_path / "source")
    page = source / "pages" / "deal-references" / "page-000001.ndjson"
    row = json.loads(page.read_text(encoding="utf-8"))
    row["lifecycle_status_observed"] = "superseded"
    page.write_bytes((canonical_json(row) + "\n").encode("utf-8"))
    manifest = source / "manifests" / "deal-references" / "page-000001.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["sha256"] = sha256_file(page)
    data["byte_count"] = page.stat().st_size
    manifest.write_text(canonical_json(data), encoding="utf-8")
    with pytest.raises(ValueError, match="prefix"):
        resume_snapshot(tmp_path / "target", source, boundary, deals, identities)
    manifest.unlink()
    with pytest.raises((FileNotFoundError, ValueError)):
        resume_snapshot(tmp_path / "target2", source, boundary, deals, identities)


def test_partial_checkpoint_limits_reject_before_sidecar_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, boundary, _, _, _ = _partial(tmp_path / "source")
    digest = __import__(
        "intelligence.crm_deal_refs.models", fromlist=["canonical_digest"]
    ).canonical_digest(json_value(boundary))
    checkpoint = read_checkpoint(source / "checkpoint.json", digest)
    replace_json(
        source / "checkpoint.json",
        json_value(replace(checkpoint, deal_pages=MAX_RECORDS, deal_records=1)),
    )
    monkeypatch.setattr(
        snapshot_validation,
        "_prefix_manifests",
        lambda *_args: (_ for _ in ()).throw(AssertionError("sidecars must not be traversed")),
    )
    with pytest.raises(ValueError, match="checkpoint limits"):
        snapshot_validation.verify_partial(source)


def test_identity_prefix_without_complete_deal_prefix_is_rejected_before_resume_copy(
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
    export_snapshot(tmp_path / "source", boundary, deals, identities)
    source = tmp_path / "source" / "snapshots" / "crm" / "deal-refs"
    (source / "snapshot-manifest.json").unlink()
    shutil.rmtree(source / "pages" / "deal-references")
    shutil.rmtree(source / "manifests" / "deal-references")
    digest = __import__(
        "intelligence.crm_deal_refs.models", fromlist=["canonical_digest"]
    ).canonical_digest(json_value(boundary))
    checkpoint = read_checkpoint(source / "checkpoint.json", digest)
    replace_json(
        source / "checkpoint.json",
        json_value(
            replace(
                checkpoint,
                deal_pages=0,
                deal_records=0,
                deal_next_cursor=None,
                completed=False,
            )
        ),
    )
    with pytest.raises(ValueError, match="complete deal prefix"):
        snapshot_validation.verify_partial(source)
    target = tmp_path / "target"
    with pytest.raises(ValueError, match="complete deal prefix"):
        resume_snapshot(target, source, boundary, deals, identities)
    assert not (target / "snapshots" / "crm" / "deal-refs").exists()


def test_snapshot_roots_reject_traversal() -> None:
    with pytest.raises(ValueError, match="invalid"):
        accepted_snapshot_root(Path("workspace"), "../escape")
    with pytest.raises(ValueError, match="invalid"):
        partial_snapshot_root(Path("workspace"), "a/b")
