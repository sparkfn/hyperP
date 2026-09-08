"""Checkpoint admission, bounded inventory, and interrupted-link recovery."""

from __future__ import annotations

import os
import re
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Final

from intelligence.crm.activities.checkpoint_atomic import fsync
from intelligence.crm.activities.checkpoint_limits import (
    TEMP_RECOVERY_MAX_SECONDS,
    CheckpointLimits,
)
from intelligence.crm.activities.models import validate_snapshot_id
from intelligence.crm.activities.path_safety import (
    confined_directory,
    confined_file,
    has_link_or_reparse,
)

_TEMP = re.compile(r"^\.(?P<final>[A-Za-z0-9][A-Za-z0-9._-]*\.json)\.(?P<id>[0-9a-f]{32})\.tmp$")
_RECORD = re.compile(r"^record-[0-9a-f]{64}\.json$")
_PAGE = re.compile(r"^page-[0-9]{8}\.json$")
_RECOVERY_EXTRA_ENTRIES: Final[int] = 1


@dataclass(frozen=True)
class Usage:
    """Current admitted checkpoint resource consumption."""

    bytes: int
    entries: int


@dataclass(frozen=True)
class _RecoveryEntry:
    path: Path
    relative: tuple[str, ...]
    metadata: os.stat_result
    temporary: re.Match[str] | None


@dataclass
class _RecoveryBudget:
    limits: CheckpointLimits
    started_at: float
    entries: int = 0
    bytes: int = 0
    _inodes: set[tuple[int, int]] = field(default_factory=set)

    def inspect(self, metadata: os.stat_result) -> None:
        self._check_time()
        self.entries += 1
        if self.entries > self.limits.max_entries + _RECOVERY_EXTRA_ENTRIES:
            raise RuntimeError("CRM activities checkpoint recovery exceeds entry ceiling")
        if has_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
            return
        inode = (metadata.st_dev, metadata.st_ino)
        if inode in self._inodes:
            return
        self._inodes.add(inode)
        self.bytes += metadata.st_size
        if self.bytes > self.limits.max_bytes:
            raise RuntimeError("CRM activities checkpoint recovery exceeds byte ceiling")

    def before_removal(self) -> None:
        self._check_time()

    def _check_time(self) -> None:
        if time.monotonic() - self.started_at > TEMP_RECOVERY_MAX_SECONDS:
            raise RuntimeError("CRM activities checkpoint recovery exceeds time ceiling")


def create_checkpoint_root(run_staging: Path, snapshot_id: str, limits: CheckpointLimits) -> Path:
    """Create or admit the fixed private checkpoint root below staging."""
    _limits(limits)
    validate_snapshot_id(snapshot_id)
    if run_staging.parent.name != "staging" or not run_staging.name:
        raise ValueError("run staging directory is outside the Intelligence workspace")
    workspace = run_staging.parent.parent
    expected = workspace / "staging" / run_staging.name
    if _lexical_path(expected) != _lexical_path(run_staging):
        raise ValueError("run staging directory is outside the Intelligence workspace")
    confined_directory(workspace, ("staging", run_staging.name), create=False)
    root = confined_directory(workspace, ("staging", ".crm-activities", snapshot_id), create=True)
    confined_directory(
        workspace, ("staging", ".crm-activities", snapshot_id, "records"), create=True
    )
    confined_directory(workspace, ("staging", ".crm-activities", snapshot_id, "pages"), create=True)
    current_usage(root, limits)
    return root


def current_usage(root: Path, limits: CheckpointLimits) -> None:
    """Validate admitted checkpoint contents and configured resource ceilings."""
    _limits(limits)
    usage(root, limits)


def checkpoint_file(root: Path, parts: tuple[str, ...], limits: CheckpointLimits) -> Path:
    """Return an admitted file path below a bounded checkpoint root."""
    current_usage(root, limits)
    workspace, root_parts = _root_context(root)
    return confined_file(workspace, root_parts + parts)


def checkpoint_directory(root: Path, parts: tuple[str, ...], limits: CheckpointLimits) -> Path:
    """Return an admitted checkpoint subdirectory."""
    current_usage(root, limits)
    workspace, root_parts = _root_context(root)
    return confined_directory(workspace, root_parts + parts, create=False)


def record_name(source_record_pk: str) -> str:
    """Return the non-reversible private file name for one source identity."""
    return f"record-{sha256(source_record_pk.encode('utf-8')).hexdigest()}.json"


def is_record_name(name: str) -> bool:
    """Return whether a filename is a fixed-format checkpoint record name."""
    return _RECORD.fullmatch(name) is not None


def evidence_name(name: str) -> None:
    """Reject unsafe single-file checkpoint evidence names."""
    if not _final_name_allowed(name, ()):
        raise ValueError("checkpoint evidence name is unsafe")


def usage(root: Path, limits: CheckpointLimits) -> Usage:
    """Return current usage after bounded interrupted-publication recovery."""
    workspace, root_parts = _root_context(root)
    admitted = confined_directory(workspace, root_parts, create=False)
    _recover_temps(admitted, limits)
    return _scan_usage(admitted, limits)


def _scan_usage(root: Path, limits: CheckpointLimits) -> Usage:
    total_bytes = 0
    entries = 0
    stack: list[tuple[Path, tuple[str, ...]]] = [(root, ())]
    while stack:
        directory, relative = stack.pop()
        try:
            candidates = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as error:
            raise ValueError("checkpoint directory could not be inspected") from error
        for candidate in candidates:
            metadata = candidate.lstat()
            entries += 1
            if entries > limits.max_entries:
                raise RuntimeError("CRM activities checkpoint exceeds entry ceiling")
            if has_link_or_reparse(metadata):
                raise ValueError("CRM activities checkpoint contains unsafe link evidence")
            candidate_relative = relative + (candidate.name,)
            if stat.S_ISDIR(metadata.st_mode):
                if candidate_relative not in {("records",), ("pages",)}:
                    raise ValueError(
                        "CRM activities checkpoint contains unknown directory evidence"
                    )
                stack.append((candidate, candidate_relative))
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("CRM activities checkpoint contains unsafe file evidence")
            if not _final_name_allowed(candidate.name, relative):
                raise ValueError("CRM activities checkpoint contains unsafe file evidence")
            total_bytes += metadata.st_size
            if total_bytes > limits.max_bytes:
                raise RuntimeError("CRM activities checkpoint exceeds byte ceiling")
    return Usage(total_bytes, entries)


def _recover_temps(root: Path, limits: CheckpointLimits) -> None:
    """Recover only the exact post-link/pre-unlink publication state.

    Recovery shares the durable checkpoint's entry and byte limits plus a short
    wall-clock ceiling before removing any item. A two-link inode is admitted
    only when the temporary name deterministically names its final sibling.
    """
    budget = _RecoveryBudget(limits, time.monotonic())
    inventory: dict[Path, _RecoveryEntry] = {}
    for directory, relative in _recovery_directories(root):
        try:
            metadata = directory.lstat()
        except FileNotFoundError:
            raise ValueError("checkpoint directory is missing") from None
        if has_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("checkpoint directory is unsafe")
        try:
            for candidate in directory.iterdir():
                metadata = candidate.lstat()
                budget.inspect(metadata)
                inventory[candidate] = _RecoveryEntry(
                    candidate,
                    relative,
                    metadata,
                    _TEMP.fullmatch(candidate.name),
                )
        except OSError as error:
            raise ValueError("checkpoint directory could not be inspected") from error

    removals = [
        _recoverable_temp(entry, inventory)
        for entry in inventory.values()
        if entry.temporary is not None
    ]
    for temporary in sorted(removals, key=lambda item: str(item)):
        budget.before_removal()
        temporary.unlink()
    for directory in sorted({path.parent for path in removals}, key=lambda item: str(item)):
        budget.before_removal()
        fsync(directory)


def _recovery_directories(root: Path) -> tuple[tuple[Path, tuple[str, ...]], ...]:
    return (
        (root, ()),
        (root / "records", ("records",)),
        (root / "pages", ("pages",)),
    )


def _recoverable_temp(
    entry: _RecoveryEntry,
    inventory: Mapping[Path, _RecoveryEntry],
) -> Path:
    match = entry.temporary
    if match is None:
        raise AssertionError("temporary recovery requires a recognized temporary name")
    final_name = match.group("final")
    if (
        has_link_or_reparse(entry.metadata)
        or not stat.S_ISREG(entry.metadata.st_mode)
        or not _final_name_allowed(final_name, entry.relative)
    ):
        raise ValueError("checkpoint contains unsafe temporary evidence")
    final = inventory.get(entry.path.with_name(final_name))
    if entry.metadata.st_nlink == 1:
        if final is not None:
            raise ValueError("checkpoint contains unsafe temporary evidence")
        return entry.path
    if entry.metadata.st_nlink != 2 or final is None:
        raise ValueError("checkpoint contains unsafe temporary evidence")
    if (
        final.relative != entry.relative
        or final.path.name != final_name
        or has_link_or_reparse(final.metadata)
        or not stat.S_ISREG(final.metadata.st_mode)
        or final.metadata.st_nlink != 2
        or not _same_inode_content(entry.metadata, final.metadata)
    ):
        raise ValueError("checkpoint contains unsafe temporary evidence")
    return entry.path


def _same_inode_content(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
    )


def _root_context(root: Path) -> tuple[Path, tuple[str, ...]]:
    if root.parent.name != ".crm-activities" or not root.name:
        raise ValueError("checkpoint root is outside the Intelligence workspace")
    staging = root.parent.parent
    if staging.name != "staging":
        raise ValueError("checkpoint root is outside the Intelligence workspace")
    workspace = staging.parent
    parts = ("staging", ".crm-activities", root.name)
    if _lexical_path(workspace.joinpath(*parts)) != _lexical_path(root):
        raise ValueError("checkpoint root is outside the Intelligence workspace")
    return workspace, parts


def _final_name_allowed(name: str, relative: tuple[str, ...]) -> bool:
    if relative == ():
        return name.endswith(".json") and Path(name).name == name and not name.startswith(".")
    if relative == ("records",):
        return _RECORD.fullmatch(name) is not None
    if relative == ("pages",):
        return _PAGE.fullmatch(name) is not None
    return False


def _limits(limits: CheckpointLimits) -> None:
    if not isinstance(limits, CheckpointLimits):
        raise TypeError("checkpoint APIs require CheckpointLimits")


def _lexical_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))
