"""Typed publication and durable-state inventory helpers."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from intelligence.artifacts import MANIFEST_LIMIT_KEYS, canonical_json, sha256_file
from intelligence.models import OutputInventory, Run, RunState

_TERMINAL: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled", "timed_out", "stale_recovered"}
)


def _row_to_run(row: sqlite3.Row) -> Run:
    state = str(row["state"])
    if state not in {"queued", "running", "publishing", *_TERMINAL}:
        raise RuntimeError("run state is corrupt")
    limits_value = row["limits_json"]
    limits: tuple[tuple[str, int], ...] = ()
    if limits_value is not None:
        try:
            raw_limits = json.loads(str(limits_value))
        except json.JSONDecodeError as error:
            raise RuntimeError("run limits are corrupt") from error
        if not isinstance(raw_limits, dict):
            raise RuntimeError("run limits are corrupt")
        if set(raw_limits) != MANIFEST_LIMIT_KEYS:
            raise RuntimeError("run limits are corrupt")
        parsed: list[tuple[str, int]] = []
        for key, value in raw_limits.items():
            if not isinstance(key, str) or not isinstance(value, int) or isinstance(value, bool):
                raise RuntimeError("run limits are corrupt")
            if value < 1:
                raise RuntimeError("run limits are corrupt")
            parsed.append((key, value))
        limits = tuple(sorted(parsed))
    return Run(
        str(row["id"]),
        str(row["command"]),
        cast(RunState, state),
        int(row["fence"]),
        float(row["created_at"]),
        None if row["heartbeat_at"] is None else float(row["heartbeat_at"]),
        bool(row["cancellation_requested"]),
        None if row["recovery_reason"] is None else str(row["recovery_reason"]),
        None if row["started_at"] is None else float(row["started_at"]),
        None if row["ended_at"] is None else float(row["ended_at"]),
        limits,
        None if row["runtime_epoch"] is None else str(row["runtime_epoch"]),
        bool(row["cleanup_unresolved"]),
        bool(row["execution_may_be_alive"]),
    )


def _encode_inventory(outputs: Sequence[OutputInventory]) -> str:
    for item in outputs:
        _validate_output(item)
    return canonical_json(
        [
            {
                "byte_count": item.byte_count,
                "relative_path": item.relative_path,
                "sha256": item.sha256,
            }
            for item in sorted(outputs, key=lambda item: item.relative_path)
        ]
    )


def _decode_inventory_for_run(
    connection: sqlite3.Connection, run_id: str
) -> tuple[OutputInventory, ...]:
    row = connection.execute(
        "SELECT publishing_inventory_json FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    return _decode_inventory(None if row is None else row[0])


def _decode_inventory(encoded: object) -> tuple[OutputInventory, ...]:
    if not isinstance(encoded, str):
        raise RuntimeError("publishing inventory is missing")
    raw = json.loads(encoded)
    if not isinstance(raw, list):
        raise RuntimeError("publishing inventory is corrupt")
    results: list[OutputInventory] = []
    for item in raw:
        if not isinstance(item, dict):
            raise RuntimeError("publishing inventory is corrupt")
        path, digest, count = item.get("relative_path"), item.get("sha256"), item.get("byte_count")
        if not isinstance(path, str) or not isinstance(digest, str) or not isinstance(count, int):
            raise RuntimeError("publishing inventory is corrupt")
        output = OutputInventory(path, digest, count)
        _validate_output(output)
        results.append(output)
    return tuple(sorted(results, key=lambda item: item.relative_path))


def _staged_from_published(
    run_id: str, outputs: Sequence[OutputInventory]
) -> tuple[OutputInventory, ...]:
    prefix = f"outputs/{run_id}/"
    values: list[OutputInventory] = []
    for item in outputs:
        if not item.relative_path.startswith(prefix):
            raise RuntimeError("published output path is invalid")
        values.append(
            OutputInventory(item.relative_path.removeprefix(prefix), item.sha256, item.byte_count)
        )
    return tuple(sorted(values, key=lambda item: item.relative_path))


def _published_inventory(
    root: Path, run_id: str, expected: Sequence[OutputInventory]
) -> tuple[OutputInventory, ...] | None:
    directory = root / "outputs" / run_id
    try:
        _assert_directory_inventory(directory, {Path(item.relative_path) for item in expected})
        published: list[OutputInventory] = []
        for item in expected:
            path = directory / item.relative_path
            if path.is_symlink() or not path.is_file() or path.stat().st_size != item.byte_count:
                return None
            if sha256_file(path) != item.sha256:
                return None
            published.append(
                OutputInventory(
                    f"outputs/{run_id}/{item.relative_path}", item.sha256, item.byte_count
                )
            )
    except (OSError, RuntimeError, ValueError):
        return None
    return tuple(published)


def _validate_output(item: OutputInventory) -> None:
    if (
        not item.relative_path
        or item.relative_path.startswith("/")
        or ".." in Path(item.relative_path).parts
    ):
        raise ValueError("output inventory path is invalid")
    if (
        item.byte_count < 0
        or len(item.sha256) != 64
        or any(char not in "0123456789abcdef" for char in item.sha256)
    ):
        raise ValueError("output inventory is invalid")


def _assert_directory_inventory(directory: Path, expected: set[Path]) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("accepted output directory is missing or unsafe")
    actual: set[Path] = set()
    for candidate in directory.rglob("*"):
        relative = candidate.relative_to(directory)
        if candidate.is_symlink():
            raise ValueError("accepted output directory contains a symbolic link")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise ValueError("accepted output directory contains unsafe evidence")
        actual.add(relative)
    if actual != expected:
        raise ValueError("accepted output directory contains missing or extra evidence")
