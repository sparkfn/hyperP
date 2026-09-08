"""Ancestor-confined bounded readers for private archive evidence."""

from __future__ import annotations

import json
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from intelligence.artifacts import canonical_json
from intelligence.crm.activities.models import validate_snapshot_id
from intelligence.crm.activities.path_safety import has_link_or_reparse


@dataclass(frozen=True)
class ReadLimits:
    """Explicit ceilings for an untrusted evidence read."""

    maximum_bytes: int
    maximum_entries: int
    maximum_rows: int

    def __post_init__(self) -> None:
        if min(self.maximum_bytes, self.maximum_entries, self.maximum_rows) < 1:
            raise ValueError("evidence read limits must be positive")


CANDIDATE_READ_LIMITS = ReadLimits(4_000_000, 10_000, 10_000)
STATUS_READ_LIMITS = ReadLimits(100_000_000, 100_000, 100_000)


@dataclass
class ReadBudget:
    """One non-resettable budget shared across a status/read operation."""

    limits: ReadLimits
    bytes_read: int = 0
    entries_read: int = 0
    rows_read: int = 0

    def add_entry(self) -> None:
        self.entries_read += 1
        if self.entries_read > self.limits.maximum_entries:
            raise RuntimeError("CRM activities evidence exceeds entry ceiling")

    def add_bytes(self, count: int) -> None:
        self.bytes_read += count
        if self.bytes_read > self.limits.maximum_bytes:
            raise RuntimeError("CRM activities evidence exceeds byte ceiling")

    def add_rows(self, count: int) -> None:
        self.rows_read += count
        if self.rows_read > self.limits.maximum_rows:
            raise RuntimeError("CRM activities evidence exceeds row ceiling")


def checkpoint_root(workspace: Path, checkpoint_id: str) -> Path | None:
    """Return an existing private checkpoint only through safe fixed ancestors."""
    validate_snapshot_id(checkpoint_id)
    current = workspace
    if not _directory_or_absent(current, "workspace"):
        return None
    for component, label in (
        ("staging", "workspace staging"),
        (".crm-activities", "CRM activities checkpoint root"),
        (checkpoint_id, "CRM activities checkpoint"),
    ):
        current = current / component
        if not _directory_or_absent(current, label):
            return None
    return current


def accepted_snapshot(workspace: Path, run_id: str, snapshot_id: str) -> Path:
    """Return an accepted snapshot only through safe fixed workspace ancestors."""
    validate_snapshot_id(snapshot_id)
    current = workspace
    _required_directory(current, "workspace")
    for component, label in (
        ("outputs", "workspace outputs"),
        (run_id, "accepted run output"),
        ("snapshots", "accepted snapshots"),
        ("crm", "accepted CRM snapshots"),
        ("activities", "accepted activity snapshots"),
        (snapshot_id, "accepted activity snapshot"),
    ):
        current = current / component
        _required_directory(current, label)
    return current


def read_published_evidence(
    workspace: Path,
    run_id: str,
    relative_path: str,
    budget: ReadBudget,
) -> tuple[dict[str, object], bytes]:
    """Read canonical published JSON through fixed safe output ancestors."""
    if not _safe_output_path(relative_path):
        raise ValueError("published evidence path is unsafe")
    _run_id(run_id)
    parts = PurePosixPath(relative_path).parts
    current = workspace
    _required_directory(current, "workspace")
    for component, label in (("outputs", "workspace outputs"), (run_id, "published run output")):
        current = current / component
        _required_directory(current, label)
    for component in parts[:-1]:
        current = current / component
        _required_directory(current, "published evidence directory")
    path = current / parts[-1]
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise ValueError("published evidence is missing") from error
    _regular_file(metadata, "published evidence")
    budget.add_entry()
    raw = _read_bounded(path, metadata, budget, "published evidence")
    value = _json_object(raw, "published evidence")
    if raw != canonical_json(value).encode("utf-8"):
        raise ValueError("published evidence is noncanonical")
    return value, raw


def read_evidence(root: Path, name: str, budget: ReadBudget) -> dict[str, object] | None:
    """Read one optional canonical checkpoint JSON object inside the fixed root."""
    if Path(name).name != name or not name.endswith(".json"):
        raise ValueError("checkpoint evidence name is unsafe")
    path = root / name
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    _regular_file(metadata, "checkpoint evidence")
    budget.add_entry()
    raw = _read_bounded(path, metadata, budget, "checkpoint evidence")
    value = _json_object(raw, "checkpoint evidence")
    if raw != canonical_json(value).encode("utf-8"):
        raise ValueError("checkpoint evidence is noncanonical")
    return value


def records(root: Path, budget: ReadBudget) -> tuple[dict[str, object], ...]:
    """Read bounded canonical checkpoint record files without following links."""
    directory = root / "records"
    _required_directory(directory, "checkpoint record directory")
    paths: list[Path] = []
    try:
        for candidate in directory.iterdir():
            budget.add_entry()
            if not candidate.name.startswith("record-") or candidate.suffix != ".json":
                raise ValueError("checkpoint record inventory is unsafe")
            _regular_file(candidate.lstat(), "checkpoint record")
            paths.append(candidate)
    except OSError as error:
        raise ValueError("checkpoint record directory could not be read") from error
    result = tuple(_read_required_json(path, budget, "checkpoint record") for path in sorted(paths))
    budget.add_rows(len(result))
    return result


def verify_snapshot_input(snapshot: Path, limits: ReadLimits) -> None:
    """Bound unsafe verification input before the strict artifact verifier parses it."""
    _required_directory(snapshot, "accepted activity snapshot")
    budget = ReadBudget(limits)
    selected_count = _selected_count(snapshot, budget)
    row_ceiling = max(limits.maximum_rows, selected_count * _ROWS_PER_SELECTED_RECORD)
    budget = ReadBudget(
        ReadLimits(limits.maximum_bytes, limits.maximum_entries, row_ceiling),
        bytes_read=budget.bytes_read,
        entries_read=budget.entries_read,
    )
    pending = [snapshot]
    while pending:
        directory = pending.pop()
        try:
            children = tuple(directory.iterdir())
        except OSError as error:
            raise ValueError("accepted activity snapshot could not be inspected") from error
        for candidate in children:
            if candidate == snapshot / "manifest.json":
                continue
            budget.add_entry()
            metadata = candidate.lstat()
            if has_link_or_reparse(metadata):
                raise ValueError("accepted activity snapshot contains unsafe link evidence")
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(candidate)
                continue
            _regular_file(metadata, "accepted activity snapshot")
            raw = _read_bounded(candidate, metadata, budget, "accepted activity snapshot")
            _count_snapshot_rows(_json_object(raw, "accepted activity snapshot"), budget)


_ROWS_PER_SELECTED_RECORD = 6


def _selected_count(snapshot: Path, budget: ReadBudget) -> int:
    path = snapshot / "manifest.json"
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise ValueError("accepted activity snapshot is missing its manifest") from error
    _regular_file(metadata, "accepted activity snapshot")
    budget.add_entry()
    raw = _read_bounded(path, metadata, budget, "accepted activity snapshot")
    value = _json_object(raw, "manifest")
    selected = value.get("selected_count")
    if not isinstance(selected, int) or isinstance(selected, bool) or selected < 0:
        raise ValueError("accepted activity snapshot manifest selected count is invalid")
    return selected


def _count_snapshot_rows(value: Mapping[str, object], budget: ReadBudget) -> None:
    for key in ("entries", "identities", "records", "outcomes"):
        rows = value.get(key)
        if isinstance(rows, list):
            budget.add_rows(len(rows))


def _read_required_json(path: Path, budget: ReadBudget, label: str) -> dict[str, object]:
    metadata = path.lstat()
    _regular_file(metadata, label)
    raw = _read_bounded(path, metadata, budget, label)
    value = _json_object(raw, label)
    if raw != canonical_json(value).encode("utf-8"):
        raise ValueError(f"{label} is noncanonical")
    return value


def _read_bounded(path: Path, metadata: stat.stat_result, budget: ReadBudget, label: str) -> bytes:
    if metadata.st_size > budget.limits.maximum_bytes - budget.bytes_read:
        raise RuntimeError("CRM activities evidence exceeds byte ceiling")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ValueError(f"{label} could not be read") from error
    if len(raw) != metadata.st_size:
        raise ValueError(f"{label} changed during read")
    budget.add_bytes(len(raw))
    return raw


def _json_object(raw: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError(f"{label} is corrupt") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _safe_output_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        value not in {"", ".", ".."}
        and "\\" not in value
        and not path.is_absolute()
        and ".." not in path.parts
        and path.as_posix() == value
        and path.suffix == ".json"
    )


def _run_id(value: str) -> None:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("published run identity is unsafe")


def _directory_or_absent(path: Path, label: str) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if has_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} is unsafe")
    return True


def _required_directory(path: Path, label: str) -> None:
    if not _directory_or_absent(path, label):
        raise ValueError(f"{label} is missing")


def _regular_file(metadata: stat.stat_result, label: str) -> None:
    if (
        has_link_or_reparse(metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ValueError(f"{label} is unsafe")
