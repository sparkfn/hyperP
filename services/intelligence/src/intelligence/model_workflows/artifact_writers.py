"""Immutable canonical artifact writers confined to a model run staging root."""

from __future__ import annotations

import os
from pathlib import Path

from intelligence.artifacts import canonical_json
from intelligence.model_workflows.codec import JsonValue
from intelligence.model_workflows.contracts import digest
from intelligence.model_workflows.path_safety import safe_directory, staged_run_root
from intelligence.models import OutputInventory


def write_staged_json(
    workspace: Path,
    run_id: str,
    relative_path: str,
    value: dict[str, JsonValue],
) -> None:
    """Write one new canonical JSON file below a confined current-run staging root."""
    _write_staged(workspace, run_id, relative_path, canonical_json(value).encode("utf-8"))


def write_staged_ndjson(
    workspace: Path,
    run_id: str,
    relative_path: str,
    values: tuple[dict[str, JsonValue], ...],
) -> None:
    """Write one new canonical LF-delimited file below a confined staging root."""
    payload = b"".join(canonical_json(value).encode("utf-8") + b"\n" for value in values)
    _write_staged(workspace, run_id, relative_path, payload)


def write_json(path: Path, value: dict[str, JsonValue]) -> OutputInventory:
    """Write one canonical immutable JSON artifact and return its local inventory."""
    payload = canonical_json(value).encode("utf-8")
    _write_new(path, payload)
    return OutputInventory(path.as_posix(), digest(value), len(payload))


def write_ndjson(path: Path, values: tuple[dict[str, JsonValue], ...]) -> OutputInventory:
    """Write canonical LF-delimited evidence without overwrite or partial replacement."""
    payload = b"".join(canonical_json(value).encode("utf-8") + b"\n" for value in values)
    _write_new(path, payload)
    return OutputInventory(path.as_posix(), _bytes_digest(payload), len(payload))


def descriptor_value(
    schema_version: str,
    fields: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Add and bind a descriptor digest; callers must still parse it before publish."""
    if "descriptor_digest" in fields or "schema_version" in fields:
        raise ValueError("descriptor construction is invalid")
    unsigned = {"schema_version": schema_version, **fields}
    return {**unsigned, "descriptor_digest": digest(unsigned)}


def _write_staged(workspace: Path, run_id: str, relative_path: str, payload: bytes) -> None:
    if (
        not relative_path
        or relative_path.startswith("/")
        or "\\" in relative_path
        or relative_path.endswith("/")
    ):
        raise ValueError("staged artifact path is invalid")
    parts = tuple(relative_path.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("staged artifact path is invalid")
    root = staged_run_root(workspace, run_id)
    directory = root
    for part in parts[:-1]:
        directory = directory / part
        try:
            safe_directory(directory, "staged model artifact directory")
        except FileNotFoundError:
            directory.mkdir(mode=0o700)
            safe_directory(directory, "staged model artifact directory")
    _write_new(directory / parts[-1], payload)


def _write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise RuntimeError("immutable model artifact already exists") from error
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _bytes_digest(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()
