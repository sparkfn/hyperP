"""Public facade for durable CRM activity checkpoint filesystem persistence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from intelligence.artifacts import canonical_json
from intelligence.crm.activities import checkpoint_atomic, checkpoint_usage
from intelligence.crm.activities.checkpoint_limits import CheckpointLimits

checkpoint_directory = checkpoint_usage.checkpoint_directory
checkpoint_file = checkpoint_usage.checkpoint_file
create_checkpoint_root = checkpoint_usage.create_checkpoint_root
current_usage = checkpoint_usage.current_usage
evidence_name = checkpoint_usage.evidence_name
is_record_name = checkpoint_usage.is_record_name
record_name = checkpoint_usage.record_name

_Usage = checkpoint_usage.Usage
_exists = checkpoint_atomic.exists
_publish_new = checkpoint_atomic.publish_new
_publish_replace = checkpoint_atomic.publish_replace
_read_bytes = checkpoint_atomic.read_bytes
_remove_published = checkpoint_atomic.remove_published
_usage = checkpoint_usage.usage


def read_json(root: Path, parts: tuple[str, ...], limits: CheckpointLimits) -> object:
    """Read and decode one admitted checkpoint JSON file."""
    path = checkpoint_file(root, parts, limits)
    try:
        return json.loads(_read_bytes(path).decode("utf-8"))
    except UnicodeDecodeError as error:
        raise ValueError("checkpoint evidence is not UTF-8") from error
    except json.JSONDecodeError as error:
        raise ValueError("checkpoint evidence is corrupt") from error


def write_exact(
    root: Path,
    parts: tuple[str, ...],
    value: Mapping[str, object],
    limits: CheckpointLimits,
) -> None:
    """Write immutable evidence or validate exact equality with an existing file."""
    path = checkpoint_file(root, parts, limits)
    if _exists(path):
        if read_json(root, parts, limits) != dict(value):
            raise RuntimeError("checkpoint evidence conflicts with immutable existing evidence")
        return
    write_new(root, parts, value, limits)


def write_new(
    root: Path,
    parts: tuple[str, ...],
    value: Mapping[str, object],
    limits: CheckpointLimits,
) -> None:
    """Atomically publish one new immutable evidence file within projected limits."""
    path = checkpoint_file(root, parts, limits)
    payload = canonical_json(dict(value)).encode("utf-8")
    usage = _usage(root, limits)
    if _exists(path):
        raise RuntimeError("checkpoint evidence already exists")
    _project(usage, len(payload), 1, limits)
    _publish_new(path, payload)
    try:
        current_usage(root, limits)
    except BaseException:
        _remove_published(path)
        raise


def write_replace(
    root: Path,
    parts: tuple[str, ...],
    value: Mapping[str, object],
    limits: CheckpointLimits,
) -> None:
    """Atomically replace mutable checkpoint state within projected limits."""
    path = checkpoint_file(root, parts, limits)
    payload = canonical_json(dict(value)).encode("utf-8")
    usage = _usage(root, limits)
    prior = _read_bytes(path)
    _project(usage, len(payload) - len(prior), 0, limits)
    _publish_replace(path, payload)
    try:
        current_usage(root, limits)
    except BaseException:
        _publish_replace(path, prior)
        raise


def _project(usage: _Usage, byte_delta: int, entry_delta: int, limits: CheckpointLimits) -> None:
    if usage.bytes + byte_delta > limits.max_bytes:
        raise RuntimeError("CRM activities checkpoint exceeds byte ceiling")
    if usage.entries + entry_delta > limits.max_entries:
        raise RuntimeError("CRM activities checkpoint exceeds entry ceiling")
