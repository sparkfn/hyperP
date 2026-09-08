"""Picklable supervised command handlers for CRM deal-reference extraction and replay."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from intelligence.artifacts import sha256_file
from intelligence.crm_deal_refs.checkpoints import (
    copy_regular_file,
    regular_inventory,
    require_confined_directory,
)
from intelligence.crm_deal_refs.export import (
    capture_matching_boundary,
    export_snapshot,
    read_boundary,
    resume_snapshot,
    seal_boundary,
    verify_snapshot,
)
from intelligence.crm_deal_refs.models import safe_identifier
from intelligence.registry import Cancelled
from intelligence.repositories.deps import get_crm_deal_refs_repository


@dataclass(frozen=True)
class ExtractRequest:
    source_instance_id: str
    as_of: str
    max_records: int
    page_size: int


@dataclass(frozen=True)
class ResumeRequest:
    run_id: str
    accepted: bool


def extract_handler(staging: Path, cancelled: Cancelled, request: ExtractRequest) -> None:
    """Seal, export, then reconcile before parent-owned publication."""
    if cancelled():
        return
    repository = get_crm_deal_refs_repository()
    try:
        boundary, deals, identities = seal_boundary(
            repository,
            source_instance_id=request.source_instance_id,
            as_of=request.as_of,
            page_size=request.page_size,
            max_records=request.max_records,
        )
        if cancelled():
            return
        export_snapshot(staging, boundary, deals, identities)
        if cancelled():
            return
        capture_matching_boundary(repository, boundary)
    finally:
        repository.close()


def resume_handler(staging: Path, cancelled: Cancelled, request: ResumeRequest) -> None:
    """Replay accepted evidence artifact-only; source-reconcile a sealed partial boundary."""
    source = _source_root(staging, request)
    workspace = staging.parent.parent
    if request.accepted:
        verify_snapshot(source)
        _copy_verified_snapshot(source, staging / "snapshots" / "crm" / "deal-refs", workspace)
        return
    boundary = read_boundary(source / "boundary.json")
    repository = get_crm_deal_refs_repository()
    try:
        deals, identities = capture_matching_boundary(repository, boundary)
        if cancelled():
            return
        if (source / "snapshot-manifest.json").is_file():
            verify_snapshot(source)
            _copy_verified_snapshot(source, staging / "snapshots" / "crm" / "deal-refs", workspace)
        else:
            resume_snapshot(staging, source, boundary, deals, identities)
        capture_matching_boundary(repository, boundary)
    finally:
        repository.close()


def accepted_snapshot_root(workspace: Path, run_id: str) -> Path:
    safe_identifier(run_id, "run identifier")
    return workspace / "outputs" / run_id / "snapshots" / "crm" / "deal-refs"


def partial_snapshot_root(workspace: Path, run_id: str) -> Path:
    safe_identifier(run_id, "run identifier")
    return workspace / "staging" / run_id / "snapshots" / "crm" / "deal-refs"


def _source_root(staging: Path, request: ResumeRequest) -> Path:
    workspace = staging.parent.parent
    root = (
        accepted_snapshot_root(workspace, request.run_id)
        if request.accepted
        else partial_snapshot_root(workspace, request.run_id)
    )
    return require_confined_directory(workspace, root)


def _copy_verified_snapshot(source: Path, target: Path, workspace: Path) -> None:
    require_confined_directory(workspace, target, allow_missing=True)
    if target.exists() or target.is_symlink():
        raise FileExistsError("resume target already exists")
    target.mkdir(mode=0o700, parents=True)
    for relative in regular_inventory(source):
        source_path = source.joinpath(*relative.split("/"))
        target_path = target.joinpath(*relative.split("/"))
        copy_regular_file(
            source_path, target_path, sha256_file(source_path), source_path.stat().st_size
        )
