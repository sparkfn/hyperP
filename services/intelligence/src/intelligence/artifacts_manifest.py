"""Versioned immutable manifest construction, validation, and quarantine."""

from __future__ import annotations

import json
import os
import stat
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from intelligence.artifacts_core import (
    _ensure_directory,
    _fsync_directory,
    _read_regular_text,
    _validate_public_text,
    _validate_relative_path,
    _validate_run_id,
    _write_new_file,
    canonical_json,
    workspace_layout,
)
from intelligence.models import OutputInventory, RunLogInventory, TerminalRunState

MANIFEST_SCHEMA_VERSION = 2
LEGACY_MANIFEST_SCHEMA_VERSION = 1
MANIFEST_LIMIT_KEYS = frozenset(
    {"max_log_bytes", "max_output_bytes", "max_output_entries", "max_runtime_seconds"}
)
DEFAULT_MANIFEST_LIMITS: dict[str, int] = {
    "max_log_bytes": 1_000_000,
    "max_output_bytes": 100_000_000,
    "max_output_entries": 10_000,
    "max_runtime_seconds": 3_600,
}
_MANIFEST_BASE_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "command",
        "created_at",
        "started_at",
        "ended_at",
        "state",
        "limits",
        "outputs",
        "run_log",
    }
)


def write_manifest(
    workspace: Path,
    run_id: str,
    command: str,
    state: TerminalRunState,
    output: OutputInventory | None = None,
    outputs: Sequence[OutputInventory] = (),
    reason: str | None = None,
    *,
    created_at: float | None = None,
    started_at: float | None = None,
    ended_at: float | None = None,
    limits: Mapping[str, int] | None = None,
    run_log: RunLogInventory | None = None,
) -> dict[str, object]:
    """Write one canonical secret-free terminal manifest, accepting identical retries."""
    _validate_run_id(run_id)
    _validate_public_text(command, "command")
    if reason is not None:
        _validate_public_text(reason, "manifest reason")
    all_outputs = tuple(outputs) if outputs else (() if output is None else (output,))
    now = time.time() if ended_at is None else ended_at
    value: dict[str, object] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "command": command,
        "created_at": created_at if created_at is not None else now,
        "started_at": started_at
        if started_at is not None
        else created_at
        if created_at is not None
        else now,
        "ended_at": now,
        "state": state,
        "limits": dict(sorted((limits or DEFAULT_MANIFEST_LIMITS).items())),
        "outputs": [
            {"byte_count": item.byte_count, "path": item.relative_path, "sha256": item.sha256}
            for item in sorted(all_outputs, key=lambda item: item.relative_path)
        ],
        "run_log": None
        if run_log is None
        else {
            "byte_count": run_log.byte_count,
            "path": run_log.relative_path,
            "sha256": run_log.sha256,
        },
    }
    if reason is not None:
        value["reason"] = reason
    created_value = _manifest_number(value["created_at"], "created_at")
    started_value = _manifest_number(value["started_at"], "started_at")
    validate_manifest(
        value,
        expected_run_id=run_id,
        expected_command=command,
        expected_state=state,
        expected_outputs=all_outputs,
        expected_reason=reason,
        expected_created_at=created_value,
        expected_started_at=started_value,
        expected_run_log=run_log,
    )
    path = workspace_layout(workspace).manifests / f"{run_id}.json"
    encoded = canonical_json(value)
    try:
        _write_new_file(path, encoded)
        _fsync_directory(path.parent)
    except FileExistsError:
        if _read_regular_text(path) != encoded:
            raise RuntimeError("terminal manifest already exists with different content") from None
    return value


def quarantine_manifest(workspace: Path, run_id: str) -> Path | None:
    """Move an untrusted pre-existing manifest aside without reading or following it."""
    _validate_run_id(run_id)
    layout = workspace_layout(workspace)
    source = layout.manifests / f"{run_id}.json"
    try:
        metadata = source.lstat()
    except FileNotFoundError:
        return None
    destination_directory = layout.rejected_manifests / run_id
    _ensure_directory(destination_directory)
    for _ in range(3):
        destination = destination_directory / f".rejected-manifest-{uuid.uuid4().hex}.json"
        if destination.exists() or destination.is_symlink():
            continue
        if stat.S_ISREG(metadata.st_mode):
            os.link(source, destination, follow_symlinks=False)
            try:
                source.unlink()
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
        else:
            os.rename(source, destination)
        _fsync_directory(source.parent)
        _fsync_directory(destination.parent)
        return destination
    raise RuntimeError("could not reserve a safe manifest quarantine path")


def read_manifest(
    path: Path,
    *,
    expected_run_id: str,
    expected_command: str,
    expected_state: TerminalRunState,
    expected_outputs: Sequence[OutputInventory] = (),
    expected_reason: str | None = None,
    expected_created_at: float | None = None,
    expected_started_at: float | None = None,
    expected_ended_at: float | None = None,
    expected_limits: Mapping[str, int] | None = None,
    expected_run_log: RunLogInventory | None = None,
) -> dict[str, object]:
    """Read and strictly validate immutable parent-owned terminal evidence."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("terminal manifest is unsafe")
    try:
        encoded = path.read_text(encoding="utf-8")
        raw = json.loads(encoded)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("terminal manifest is invalid") from error
    if canonical_json(raw) != encoded:
        raise ValueError("terminal manifest is not canonical")
    return validate_manifest(
        raw,
        expected_run_id=expected_run_id,
        expected_command=expected_command,
        expected_state=expected_state,
        expected_outputs=expected_outputs,
        expected_reason=expected_reason,
        expected_created_at=expected_created_at,
        expected_started_at=expected_started_at,
        expected_ended_at=expected_ended_at,
        expected_limits=expected_limits,
        expected_run_log=expected_run_log,
    )


def validate_manifest(
    value: object,
    *,
    expected_run_id: str,
    expected_command: str,
    expected_state: TerminalRunState,
    expected_outputs: Sequence[OutputInventory] = (),
    expected_reason: str | None = None,
    expected_created_at: float | None = None,
    expected_started_at: float | None = None,
    expected_ended_at: float | None = None,
    expected_limits: Mapping[str, int] | None = None,
    expected_run_log: RunLogInventory | None = None,
) -> dict[str, object]:
    """Validate exact manifest schema, evidence, limits, and public values."""
    if not isinstance(value, dict):
        raise ValueError("terminal manifest must be an object")
    manifest = cast(dict[str, object], value)
    keys = _MANIFEST_BASE_KEYS | ({"reason"} if expected_reason is not None else set())
    if set(manifest) != keys:
        raise ValueError("terminal manifest schema is invalid")
    schema_version = manifest.get("schema_version")
    if schema_version not in {LEGACY_MANIFEST_SCHEMA_VERSION, MANIFEST_SCHEMA_VERSION}:
        raise ValueError("terminal manifest schema is invalid")
    if manifest.get("run_id") != expected_run_id or manifest.get("command") != expected_command:
        raise ValueError("terminal manifest identity is invalid")
    if manifest.get("state") != expected_state:
        raise ValueError("terminal manifest state is invalid")
    created_at = _manifest_number(manifest.get("created_at"), "created_at")
    started_at = _manifest_number(manifest.get("started_at"), "started_at")
    ended_at = _manifest_number(manifest.get("ended_at"), "ended_at")
    if expected_created_at is not None and created_at != expected_created_at:
        raise ValueError("terminal manifest creation time is invalid")
    if expected_started_at is not None and started_at != expected_started_at:
        raise ValueError("terminal manifest start time is invalid")
    if expected_ended_at is not None and ended_at != expected_ended_at:
        raise ValueError("terminal manifest end time is invalid")
    if started_at < created_at or ended_at < started_at:
        raise ValueError("terminal manifest timestamps are invalid")
    normalized_limits = _normalize_manifest_limits(manifest.get("limits"), schema_version)
    for key, limit in normalized_limits.items():
        if not isinstance(key, str) or not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("terminal manifest limits are invalid")
        if limit < 1:
            raise ValueError("terminal manifest limits are invalid")
    if expected_limits is not None and normalized_limits != dict(expected_limits):
        raise ValueError("terminal manifest limits do not match expected configuration")
    actual_outputs = _manifest_outputs(manifest.get("outputs"))
    expected_output_values = tuple(sorted(expected_outputs, key=lambda item: item.relative_path))
    if actual_outputs != expected_output_values:
        raise ValueError("terminal manifest outputs are invalid")
    actual_log = _manifest_log(manifest.get("run_log"), expected_run_id)
    if actual_log != expected_run_log:
        raise ValueError("terminal manifest log evidence is invalid")
    if expected_reason is None:
        if "reason" in manifest:
            raise ValueError("terminal manifest reason is invalid")
    elif manifest.get("reason") != expected_reason:
        raise ValueError("terminal manifest reason is invalid")
    return manifest


def _normalize_manifest_limits(value: object, schema_version: object) -> dict[str, int]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(limit, int) or isinstance(limit, bool)
        for key, limit in value.items()
    ):
        raise ValueError("terminal manifest limits are invalid")
    if schema_version == MANIFEST_SCHEMA_VERSION and set(value) != MANIFEST_LIMIT_KEYS:
        raise ValueError("terminal manifest limits are invalid")
    legacy_three_key_limits = frozenset(
        {"max_log_bytes", "max_output_bytes", "max_runtime_seconds"}
    )
    if schema_version == LEGACY_MANIFEST_SCHEMA_VERSION and set(value) not in {
        frozenset(),
        legacy_three_key_limits,
    }:
        raise ValueError("terminal manifest limits are invalid")
    normalized = dict(DEFAULT_MANIFEST_LIMITS)
    normalized.update(cast(dict[str, int], value))
    return normalized


def _manifest_number(value: object, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"terminal manifest {field} is invalid")
    return float(value)


def _manifest_outputs(value: object) -> tuple[OutputInventory, ...]:
    if not isinstance(value, list):
        raise ValueError("terminal manifest outputs are invalid")
    outputs: list[OutputInventory] = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"byte_count", "path", "sha256"}:
            raise ValueError("terminal manifest outputs are invalid")
        path, digest, byte_count = entry.get("path"), entry.get("sha256"), entry.get("byte_count")
        if (
            not isinstance(path, str)
            or not isinstance(digest, str)
            or not isinstance(byte_count, int)
            or isinstance(byte_count, bool)
        ):
            raise ValueError("terminal manifest outputs are invalid")
        relative = Path(path)
        _validate_relative_path(relative)
        if (
            len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or byte_count < 0
        ):
            raise ValueError("terminal manifest outputs are invalid")
        outputs.append(OutputInventory(path, digest, byte_count))
    return tuple(sorted(outputs, key=lambda item: item.relative_path))


def _manifest_log(value: object, run_id: str) -> RunLogInventory | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"byte_count", "path", "sha256"}:
        raise ValueError("terminal manifest log evidence is invalid")
    path, digest, byte_count = value.get("path"), value.get("sha256"), value.get("byte_count")
    if (
        not isinstance(path, str)
        or path != f"runs/logs/{run_id}.ndjson"
        or not isinstance(digest, str)
        or not isinstance(byte_count, int)
        or isinstance(byte_count, bool)
        or byte_count < 0
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise ValueError("terminal manifest log evidence is invalid")
    return RunLogInventory(path, digest, byte_count)
