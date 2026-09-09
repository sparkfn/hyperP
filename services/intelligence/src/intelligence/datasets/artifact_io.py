"""Canonical immutable dataset artifact I/O and inventory scanning."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

from intelligence.artifacts import canonical_json
from intelligence.datasets.bounds import (
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACT_ENTRIES,
    MAX_ARTIFACT_FILE_BYTES,
    MAX_ARTIFACT_ROWS,
    ReadBudget,
    canonical_json_object,
    canonical_ndjson,
    digest_file,
)
from intelligence.models import OutputInventory

PAYLOAD_FILES = frozenset(
    {"config.json", "definition.json", "dispositions.ndjson", "rows.ndjson", "schema.json"}
)
ALL_FILES = PAYLOAD_FILES | {"manifest.json"}


def inventory(
    root: Path,
    dataset_id: str,
    include_manifest: bool,
    budget: ReadBudget | None = None,
) -> tuple[OutputInventory, ...]:
    read_budget = budget or ReadBudget(MAX_ARTIFACT_BYTES, MAX_ARTIFACT_ENTRIES, MAX_ARTIFACT_ROWS)
    names = ALL_FILES if (root / "manifest.json").exists() else PAYLOAD_FILES
    values: list[OutputInventory] = []
    for path in artifact_paths(root, read_budget, names):
        relative = path.relative_to(root).as_posix()
        if relative == "manifest.json" and not include_manifest:
            continue
        values.append(
            OutputInventory(
                f"datasets/{dataset_id}/{relative}",
                digest_file(path, read_budget, maximum_file_bytes=MAX_ARTIFACT_FILE_BYTES),
                path.lstat().st_size,
            )
        )
    if not values:
        raise ValueError("dataset inventory is empty")
    return tuple(values)


def write_json(path: Path, value: Mapping[str, object]) -> None:
    write_bytes(path, canonical_json(dict(value)).encode("utf-8"))


def write_ndjson(path: Path, values: tuple[dict[str, object], ...]) -> None:
    write_bytes(path, b"".join(canonical_json(item).encode("utf-8") + b"\n" for item in values))


def write_bytes(path: Path, payload: bytes) -> None:
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise RuntimeError("immutable dataset artifact already exists") from error
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def json_object(path: Path, budget: ReadBudget) -> dict[str, object]:
    return canonical_json_object(path, budget, maximum_file_bytes=MAX_ARTIFACT_FILE_BYTES)


def ndjson(path: Path, budget: ReadBudget) -> list[dict[str, object]]:
    return canonical_ndjson(path, budget, maximum_file_bytes=MAX_ARTIFACT_FILE_BYTES)


def artifact_paths(root: Path, budget: ReadBudget, expected: frozenset[str]) -> tuple[Path, ...]:
    safe_directory(root)
    paths: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        children = _children(directory, budget)
        for candidate in sorted(children, key=lambda item: item.name, reverse=True):
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("dataset inventory contains unsafe evidence")
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(candidate)
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                if metadata.st_size > MAX_ARTIFACT_FILE_BYTES:
                    raise RuntimeError("dataset evidence exceeds per-file byte ceiling")
                paths.append(candidate)
            else:
                raise ValueError("dataset inventory contains unsafe evidence")
    result = tuple(sorted(paths, key=lambda item: item.as_posix()))
    names = {item.relative_to(root).as_posix() for item in result}
    if names != expected or len(names) != len(result):
        raise ValueError("dataset inventory has missing or unexpected evidence")
    return result


def safe_directory(path: Path) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("dataset directory is unsafe")


def _children(directory: Path, budget: ReadBudget) -> list[Path]:
    values: list[Path] = []
    try:
        for candidate in directory.iterdir():
            budget.entry()
            values.append(candidate)
    except OSError as error:
        raise ValueError("dataset inventory cannot be read") from error
    return values
