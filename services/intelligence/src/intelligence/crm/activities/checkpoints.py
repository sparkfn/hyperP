"""Durable private checkpoint primitives for CRM activity archive attempts."""

from __future__ import annotations

import json
import os
import stat
import uuid
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import cast

from intelligence.artifacts import canonical_json
from intelligence.crm.activities.models import (
    ArchiveRequest,
    SealedBoundary,
    parse_request,
    validate_snapshot_id,
)

_SCHEMA = "crm-activities-checkpoint-v1"


def checkpoint_root(run_staging: Path, snapshot_id: str) -> Path:
    """Return the fixed private checkpoint directory without escaping staging."""
    if run_staging.is_symlink() or not run_staging.is_dir():
        raise ValueError("run staging directory is unsafe")
    validate_snapshot_id(snapshot_id)
    root = run_staging.parent.parent
    staging = root / "staging"
    if staging.resolve() != run_staging.parent.resolve():
        raise ValueError("run staging is outside the Intelligence workspace")
    private = staging / ".crm-activities"
    _directory(private)
    target = private / snapshot_id
    _directory(target)
    for child in (target / "records", target / "pages"):
        _directory(child)
    return target


def initialize(root: Path, request: ArchiveRequest) -> None:
    """Write immutable request evidence on first creation, reject a conflict later."""
    _write_exact(root / "request.json", request.as_public_dict())
    state_path = root / "checkpoint.json"
    if state_path.exists() or state_path.is_symlink():
        state(root)
        return
    _write_exact(state_path, {"schema_version": _SCHEMA, "phase": "new"})


def write_boundary(root: Path, boundary: SealedBoundary) -> None:
    _write_exact(root / "boundary.json", boundary.as_dict())
    _write_state(root, "sealed", boundary.digest, 0)


def load_request(root: Path) -> ArchiveRequest:
    return parse_request(_object(_read_json(root / "request.json"), "request"))


def load_boundary(root: Path) -> Mapping[str, object]:
    value = _object(_read_json(root / "boundary.json"), "boundary")
    digest = value.get("digest")
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("sealed boundary digest is missing")
    return value


def write_record(root: Path, source_record_pk: str, value: Mapping[str, object]) -> None:
    _write_exact(root / "records" / _record_name(source_record_pk), dict(value))


def read_record(root: Path, source_record_pk: str) -> Mapping[str, object]:
    value = _object(_read_json(root / "records" / _record_name(source_record_pk)), "record")
    if value.get("source_record_pk") != source_record_pk:
        raise ValueError("checkpoint record identity does not match its file")
    return value


def records(root: Path) -> tuple[Mapping[str, object], ...]:
    directory = root / "records"
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("checkpoint record directory is unsafe")
    return tuple(
        _object(_read_json(path), "record") for path in sorted(directory.glob("record-*.json"))
    )


def write_page(root: Path, ordinal: int, value: Mapping[str, object]) -> None:
    if ordinal < 1:
        raise ValueError("page ordinal must be positive")
    _write_exact(root / "pages" / f"page-{ordinal:08d}.json", dict(value))


def write_evidence(root: Path, name: str, value: Mapping[str, object]) -> None:
    """Persist an immutable, single-file domain evidence object under checkpoint root."""
    if Path(name).name != name or not name.endswith(".json"):
        raise ValueError("checkpoint evidence name is unsafe")
    _write_exact(root / name, dict(value))


def read_evidence(root: Path, name: str) -> Mapping[str, object]:
    if Path(name).name != name or not name.endswith(".json"):
        raise ValueError("checkpoint evidence name is unsafe")
    return _object(_read_json(root / name), "checkpoint evidence")


def bounded_usage(root: Path, maximum_bytes: int, maximum_entries: int) -> None:
    """Reject hidden checkpoint growth outside the foundation's current-run scan."""
    if maximum_bytes < 1 or maximum_entries < 1:
        raise ValueError("checkpoint limits must be positive")
    total_bytes = 0
    entries = 0
    for candidate in root.rglob("*"):
        entries += 1
        if entries > maximum_entries:
            raise RuntimeError("CRM activities checkpoint exceeds entry ceiling")
        metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("CRM activities checkpoint contains unsafe link evidence")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("CRM activities checkpoint contains unsafe file evidence")
        total_bytes += metadata.st_size
        if total_bytes > maximum_bytes:
            raise RuntimeError("CRM activities checkpoint exceeds byte ceiling")


def advance(root: Path, boundary_digest: str, ordinal: int) -> None:
    _write_state(root, "paging", boundary_digest, ordinal)


def complete(root: Path, boundary_digest: str, pages: int) -> None:
    _write_state(root, "completed", boundary_digest, pages)


def state(root: Path) -> Mapping[str, object]:
    value = _object(_read_json(root / "checkpoint.json"), "checkpoint")
    if value.get("schema_version") != _SCHEMA:
        raise ValueError("unsupported CRM activities checkpoint")
    if value.get("phase") not in {"new", "sealed", "paging", "completed"}:
        raise ValueError("checkpoint phase is corrupt")
    return value


def _write_state(root: Path, phase: str, boundary_digest: str, pages: int) -> None:
    if len(boundary_digest) != 64 or pages < 0:
        raise ValueError("checkpoint state is invalid")
    _write_replace(
        root / "checkpoint.json",
        {
            "schema_version": _SCHEMA,
            "phase": phase,
            "boundary_digest": boundary_digest,
            "pages": pages,
        },
    )


def _directory(path: Path) -> None:
    if path.exists():
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("checkpoint path is unsafe")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


def _write_exact(path: Path, value: Mapping[str, object]) -> None:
    if path.exists():
        if _read_json(path) != dict(value):
            raise RuntimeError("checkpoint evidence conflicts with immutable existing evidence")
        return
    _write_new(path, value)


def _write_replace(path: Path, value: Mapping[str, object]) -> None:
    if path.exists() and (path.is_symlink() or not path.is_file() or path.lstat().st_nlink != 1):
        raise ValueError("checkpoint state path is unsafe")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    _write_new(temporary, value)
    os.replace(temporary, path)
    _fsync(path.parent)


def _write_new(path: Path, value: Mapping[str, object]) -> None:
    payload = canonical_json(dict(value)).encode("utf-8")
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    _fsync(path.parent)


def _read_json(path: Path) -> object:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ValueError("checkpoint evidence is unsafe")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("checkpoint evidence is corrupt") from error


def _object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return cast(Mapping[str, object], value)


def _fsync(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _record_name(source_record_pk: str) -> str:
    return f"record-{sha256(source_record_pk.encode('utf-8')).hexdigest()}.json"
