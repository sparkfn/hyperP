"""Model-artifact path admission built on shared link-free confinement rules."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from intelligence.crm.activities.path_safety import confined_directory
from intelligence.datasets.models import safe_component


def output_run_root(workspace: Path, run_id: str) -> Path:
    """Return the only admitted root for one published model-workflow run."""
    safe_component(run_id, "model run")
    return confined_directory(workspace, ("outputs", run_id), create=False)


def terminal_manifest_root(workspace: Path) -> Path:
    """Return the admitted parent of parent-owned terminal manifests."""
    return confined_directory(workspace, ("runs", "manifests"), create=False)


def terminal_log_root(workspace: Path) -> Path:
    """Return the admitted parent of parent-owned run logs."""
    return confined_directory(workspace, ("runs", "logs"), create=False)


def staged_run_root(workspace: Path, run_id: str) -> Path:
    """Return a pre-created, link-free runtime staging directory."""
    safe_component(run_id, "model run")
    return confined_directory(workspace, ("staging", run_id), create=False)


def regular_file(path: Path, label: str) -> os.stat_result:
    """Require an unlinked single-link regular file without following it."""
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or _is_reparse(metadata):
        raise ValueError(f"{label} is unsafe")
    return metadata


def safe_directory(path: Path, label: str) -> None:
    """Require a link-free directory after its ancestry has already been confined."""
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or _is_reparse(metadata):
        raise ValueError(f"{label} is unsafe")


def _is_reparse(metadata: os.stat_result) -> bool:
    raw_attributes = getattr(metadata, "st_file_attributes", 0)
    attributes = raw_attributes if isinstance(raw_attributes, int) else 0
    raw_reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    reparse = raw_reparse if isinstance(raw_reparse, int) else 0
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse)
