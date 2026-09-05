"""Bounded staged-output scanning and atomic publication."""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from pathlib import Path

from intelligence.artifacts_core import (
    _fsync_directory,
    _rename_noreplace,
    _validate_relative_path,
    _validate_run_id,
    sha256_file,
    workspace_layout,
)
from intelligence.models import OutputInventory


def scan_staged_outputs(
    workspace: Path, run_id: str, maximum_bytes: int, maximum_entries: int = 10_000
) -> tuple[OutputInventory, ...]:
    """Return a sorted inventory after rejecting unsafe staged filesystem entries."""
    layout = workspace_layout(workspace)
    _validate_run_id(run_id)
    staging = layout.staging / run_id
    if staging.is_symlink() or not staging.is_dir():
        raise ValueError("run staging directory is missing or unsafe")
    scan_staged_usage(workspace, run_id, maximum_bytes, maximum_entries)
    total = 0
    inventory: list[OutputInventory] = []
    seen_inodes: set[tuple[int, int]] = set()
    for candidate in sorted(staging.rglob("*"), key=lambda path: path.as_posix()):
        relative = candidate.relative_to(staging)
        _validate_relative_path(relative)
        metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("staged outputs must not contain symbolic links")
        if candidate.is_dir():
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("staged outputs must contain regular files only")
        if metadata.st_nlink != 1:
            raise ValueError("staged outputs must not contain hard links")
        inode = (metadata.st_dev, metadata.st_ino)
        if inode in seen_inodes:
            raise ValueError("staged outputs must not contain duplicate file inodes")
        seen_inodes.add(inode)
        byte_count = metadata.st_size
        total += byte_count
        if byte_count > maximum_bytes or total > maximum_bytes:
            raise RuntimeError("staged outputs exceed configured byte limit")
        inventory.append(OutputInventory(relative.as_posix(), sha256_file(candidate), byte_count))
    return tuple(inventory)


def publish_inventory(
    workspace: Path,
    run_id: str,
    inventory: Sequence[OutputInventory],
    maximum_bytes: int,
    maximum_entries: int = 10_000,
) -> tuple[OutputInventory, ...]:
    """Atomically publish a complete pre-scanned run directory without replacement."""
    layout = workspace_layout(workspace)
    _validate_run_id(run_id)
    staging = layout.staging / run_id
    expected = tuple(sorted(inventory, key=lambda item: item.relative_path))
    actual = scan_staged_outputs(layout.root, run_id, maximum_bytes, maximum_entries)
    if actual != expected:
        raise RuntimeError("staged output inventory changed before publication")
    destination = layout.outputs / run_id
    try:
        _rename_noreplace(staging, destination)
    except FileExistsError as error:
        raise FileExistsError("accepted output directory already exists") from error
    except OSError as error:
        raise RuntimeError("output publication requires the workspace same volume") from error
    _fsync_directory(destination.parent)
    return tuple(
        OutputInventory(f"outputs/{run_id}/{item.relative_path}", item.sha256, item.byte_count)
        for item in expected
    )


def publish_file(
    workspace: Path,
    run_id: str,
    source: Path,
    maximum_bytes: int,
    maximum_entries: int = 10_000,
) -> OutputInventory:
    """Publish a one-file staging directory through the public compatibility seam."""
    layout = workspace_layout(workspace)
    try:
        relative = source.relative_to(layout.staging / run_id)
    except ValueError as error:
        raise ValueError("output must be inside this run's staging directory") from error
    inventory = scan_staged_outputs(layout.root, run_id, maximum_bytes, maximum_entries)
    matches = tuple(item for item in inventory if item.relative_path == relative.as_posix())
    if len(matches) != 1:
        raise ValueError("output must be one regular file inside this run's staging directory")
    published = publish_inventory(layout.root, run_id, inventory, maximum_bytes, maximum_entries)
    return published[
        tuple(item.relative_path for item in inventory).index(matches[0].relative_path)
    ]


def scan_staged_usage(
    workspace: Path, run_id: str, maximum_bytes: int, maximum_entries: int = 10_000
) -> None:
    """Bound live staged usage without hashing or materializing the complete tree."""
    layout = workspace_layout(workspace)
    _validate_run_id(run_id)
    if maximum_bytes < 1 or maximum_entries < 1:
        raise ValueError("staged output limits must be positive")
    staging = layout.staging / run_id
    if staging.is_symlink() or not staging.is_dir():
        raise ValueError("run staging directory is missing or unsafe")
    total_bytes = 0
    entries = 0
    pending = [staging]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    entries += 1
                    if entries > maximum_entries:
                        raise RuntimeError("staged outputs exceed configured entry limit")
                    candidate = Path(entry.path)
                    relative = candidate.relative_to(staging)
                    _validate_relative_path(relative)
                    try:
                        metadata = os.stat(entry.path, follow_symlinks=False)
                    except OSError as error:
                        raise ValueError("staged output could not be inspected") from error
                    if stat.S_ISLNK(metadata.st_mode):
                        raise ValueError("staged outputs must not contain symbolic links")
                    if stat.S_ISDIR(metadata.st_mode):
                        pending.append(candidate)
                        continue
                    if not stat.S_ISREG(metadata.st_mode):
                        raise ValueError("staged outputs must contain regular files only")
                    if metadata.st_nlink != 1:
                        raise ValueError("staged outputs must not contain hard links")
                    total_bytes += metadata.st_size
                    if total_bytes > maximum_bytes:
                        raise RuntimeError("staged outputs exceed configured byte limit")
        except OSError as error:
            raise ValueError("staged output directory could not be inspected") from error
