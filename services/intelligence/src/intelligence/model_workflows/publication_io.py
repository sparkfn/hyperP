"""Canonical no-overwrite staging-write primitives for model artifacts."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from intelligence.artifacts import canonical_json, sha256_file
from intelligence.model_workflows.artifacts import MISSINGNESS_SCHEMA
from intelligence.model_workflows.codec import JsonValue
from intelligence.model_workflows.contracts import ACTIVITY_PROVENANCE, FEATURES, digest


def write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write(path, canonical_json(value).encode("utf-8"))


def write_ndjson(path: Path, values: tuple[dict[str, object], ...]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write(path, b"".join(canonical_json(value).encode("utf-8") + b"\n" for value in values))


def inventory(path: Path, relative: str) -> dict[str, JsonValue]:
    return {
        "byte_count": path.stat().st_size,
        "relative_path": relative,
        "sha256": sha256_file(path),
    }


def missingness() -> dict[str, JsonValue]:
    unsigned = {
        "activity_coverage": "legacy_partial_snapshot",
        "features": list(FEATURES),
        "provenance": ACTIVITY_PROVENANCE,
        "schema_version": MISSINGNESS_SCHEMA,
    }
    return cast(dict[str, JsonValue], {**unsigned, "digest": digest(unsigned)})


def _write(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
