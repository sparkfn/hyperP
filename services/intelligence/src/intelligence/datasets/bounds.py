"""Small fixed budgets for dataset artifact and accepted-input evidence reads."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from intelligence.artifacts import canonical_json
from intelligence.models import OutputInventory

MAX_INPUT_BYTES = 100_000_000
MAX_INPUT_ENTRIES = 2_000
MAX_INPUT_FILE_BYTES = 20_000_000
MAX_ARTIFACT_BYTES = 100_000_000
MAX_ARTIFACT_ENTRIES = 64
MAX_ARTIFACT_FILE_BYTES = 50_000_000
MAX_ARTIFACT_ROWS = 120_000
MAX_DESCRIPTOR_BYTES = 1_000_000


@dataclass
class ReadBudget:
    """A non-resettable file/byte/row budget checked before every content operation."""

    maximum_bytes: int
    maximum_entries: int
    maximum_rows: int
    bytes_used: int = 0
    entries_used: int = 0
    rows_used: int = 0

    def entry(self) -> None:
        self.entries_used += 1
        if self.entries_used > self.maximum_entries:
            raise RuntimeError("dataset evidence exceeds entry ceiling")

    def reserve(self, count: int, *, maximum_file_bytes: int) -> None:
        if count < 0 or count > maximum_file_bytes:
            raise RuntimeError("dataset evidence exceeds per-file byte ceiling")
        self.bytes_used += count
        if self.bytes_used > self.maximum_bytes:
            raise RuntimeError("dataset evidence exceeds byte ceiling")

    def rows(self, count: int) -> None:
        self.rows_used += count
        if self.rows_used > self.maximum_rows:
            raise RuntimeError("dataset evidence exceeds row ceiling")


def registered_tree(
    root: Path,
    prefix: str,
    registered: Iterable[OutputInventory],
    budget: ReadBudget,
    *,
    maximum_file_bytes: int,
) -> tuple[OutputInventory, ...]:
    """Preflight every entry and registered byte count before any caller hashes or reads bytes."""
    _directory(root)
    registered_values = tuple(sorted(registered, key=lambda item: item.relative_path))
    expected = {item.relative_path: item for item in registered_values}
    actual: list[OutputInventory] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        children: list[Path] = []
        try:
            for child in directory.iterdir():
                budget.entry()
                children.append(child)
        except OSError as error:
            raise ValueError("dataset evidence directory cannot be read") from error
        for child in sorted(children, key=lambda item: item.name, reverse=True):
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("dataset evidence contains symbolic links")
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(child)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("dataset evidence contains unsafe entries")
            if metadata.st_size > maximum_file_bytes:
                raise RuntimeError("dataset evidence exceeds per-file byte ceiling")
            relative = child.relative_to(root).as_posix()
            output_path = f"{prefix}{relative}"
            registered_item = expected.get(output_path)
            if registered_item is None or registered_item.byte_count != metadata.st_size:
                raise ValueError("dataset evidence is not exactly State-registered")
            actual.append(OutputInventory(output_path, registered_item.sha256, metadata.st_size))
    result = tuple(sorted(actual, key=lambda item: item.relative_path))
    if result != registered_values:
        raise ValueError("dataset evidence registered inventory is incomplete or has extra files")
    return result


def canonical_json_object(
    path: Path, budget: ReadBudget, *, maximum_file_bytes: int
) -> dict[str, object]:
    """Read one canonical object after static safety and fixed budget checks."""
    raw = read_bytes(path, budget, maximum_file_bytes=maximum_file_bytes)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("dataset JSON evidence is corrupt") from error
    if not isinstance(value, dict) or raw != canonical_json(value).encode("utf-8"):
        raise ValueError("dataset JSON evidence is noncanonical")
    return dict(value)


def canonical_ndjson(
    path: Path,
    budget: ReadBudget,
    *,
    maximum_file_bytes: int,
) -> list[dict[str, object]]:
    """Read canonical LF-delimited objects and charge rows before returning them."""
    raw = read_bytes(path, budget, maximum_file_bytes=maximum_file_bytes)
    if raw and not raw.endswith(b"\n"):
        raise ValueError("dataset NDJSON must use LF termination")
    lines = raw.splitlines()
    budget.rows(len(lines))
    values: list[dict[str, object]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("dataset NDJSON evidence is corrupt") from error
        if not isinstance(value, dict) or line != canonical_json(value).encode("utf-8"):
            raise ValueError("dataset NDJSON evidence is noncanonical")
        values.append(dict(value))
    return values


def digest_file(path: Path, budget: ReadBudget, *, maximum_file_bytes: int) -> str:
    """Hash one stat-validated file while reserving the read before opening it."""
    budget.entry()
    metadata = _regular(path)
    budget.reserve(metadata.st_size, maximum_file_bytes=maximum_file_bytes)
    digest = hashlib.sha256()
    read = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            read += len(chunk)
            digest.update(chunk)
    if read != metadata.st_size:
        raise ValueError("dataset evidence changed during hash")
    return digest.hexdigest()


def read_bytes(path: Path, budget: ReadBudget, *, maximum_file_bytes: int) -> bytes:
    """Read a stat-validated file only after reserving its byte budget."""
    budget.entry()
    metadata = _regular(path)
    budget.reserve(metadata.st_size, maximum_file_bytes=maximum_file_bytes)
    with path.open("rb") as handle:
        raw = handle.read(metadata.st_size + 1)
    if len(raw) != metadata.st_size:
        raise ValueError("dataset evidence changed during read")
    return raw


def _directory(path: Path) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("dataset evidence directory is unsafe")


def _regular(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ValueError("dataset evidence file is unsafe")
    return metadata
