"""Private checkpoint filesystem admission, usage, and atomic persistence."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from intelligence.artifacts import canonical_json
from intelligence.crm.activities.checkpoint_limits import CheckpointLimits
from intelligence.crm.activities.models import validate_snapshot_id
from intelligence.crm.activities.path_safety import (
    confined_directory,
    confined_file,
    has_link_or_reparse,
)

_TEMP = re.compile(r"^\.(?P<final>[A-Za-z0-9][A-Za-z0-9._-]*\.json)\.(?P<id>[0-9a-f]{32})\.tmp$")
_RECORD = re.compile(r"^record-[0-9a-f]{64}\.json$")
_PAGE = re.compile(r"^page-[0-9]{8}\.json$")


@dataclass(frozen=True)
class _Usage:
    bytes: int
    entries: int


def create_checkpoint_root(run_staging: Path, snapshot_id: str, limits: CheckpointLimits) -> Path:
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
    _limits(limits)
    workspace, root_parts = _root_context(root)
    admitted = confined_directory(workspace, root_parts, create=False)
    _recover_temps(admitted)
    total_bytes = 0
    entries = 0
    stack: list[tuple[Path, tuple[str, ...]]] = [(admitted, ())]
    while stack:
        directory, relative = stack.pop()
        for candidate in sorted(directory.iterdir(), key=lambda item: item.name):
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


def checkpoint_file(root: Path, parts: tuple[str, ...], limits: CheckpointLimits) -> Path:
    current_usage(root, limits)
    workspace, root_parts = _root_context(root)
    return confined_file(workspace, root_parts + parts)


def checkpoint_directory(root: Path, parts: tuple[str, ...], limits: CheckpointLimits) -> Path:
    current_usage(root, limits)
    workspace, root_parts = _root_context(root)
    return confined_directory(workspace, root_parts + parts, create=False)


def read_json(root: Path, parts: tuple[str, ...], limits: CheckpointLimits) -> object:
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


def record_name(source_record_pk: str) -> str:
    return f"record-{sha256(source_record_pk.encode('utf-8')).hexdigest()}.json"


def is_record_name(name: str) -> bool:
    return _RECORD.fullmatch(name) is not None


def evidence_name(name: str) -> None:
    if not _final_name_allowed(name, ()):
        raise ValueError("checkpoint evidence name is unsafe")


def _usage(root: Path, limits: CheckpointLimits) -> _Usage:
    total_bytes = 0
    entries = 0
    workspace, root_parts = _root_context(root)
    admitted = confined_directory(workspace, root_parts, create=False)
    _recover_temps(admitted)
    stack: list[tuple[Path, tuple[str, ...]]] = [(admitted, ())]
    while stack:
        directory, relative = stack.pop()
        for candidate in sorted(directory.iterdir(), key=lambda item: item.name):
            metadata = candidate.lstat()
            entries += 1
            if has_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
                if stat.S_ISDIR(metadata.st_mode) and relative + (candidate.name,) in {
                    ("records",),
                    ("pages",),
                }:
                    stack.append((candidate, relative + (candidate.name,)))
                    continue
                raise ValueError("CRM activities checkpoint contains unsafe file evidence")
            if metadata.st_nlink != 1 or not _final_name_allowed(candidate.name, relative):
                raise ValueError("CRM activities checkpoint contains unsafe file evidence")
            total_bytes += metadata.st_size
    return _Usage(total_bytes, entries)


def _recover_temps(root: Path) -> None:
    for directory, relative in (
        (root, ()),
        (root / "records", ("records",)),
        (root / "pages", ("pages",)),
    ):
        metadata = directory.lstat()
        if has_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("checkpoint directory is unsafe")
        removed = False
        for candidate in directory.iterdir():
            match = _TEMP.fullmatch(candidate.name)
            if match is None:
                continue
            temp_metadata = candidate.lstat()
            if (
                has_link_or_reparse(temp_metadata)
                or not stat.S_ISREG(temp_metadata.st_mode)
                or temp_metadata.st_nlink != 1
                or not _final_name_allowed(match.group("final"), relative)
            ):
                raise ValueError("checkpoint contains unsafe temporary evidence")
            candidate.unlink()
            removed = True
        if removed:
            _fsync(directory)


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


def _read_bytes(path: Path) -> bytes:
    metadata = path.lstat()
    if (
        has_link_or_reparse(metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ValueError("checkpoint evidence is unsafe")
    return path.read_bytes()


def _publish_new(path: Path, payload: bytes) -> None:
    temporary = _temporary_path(path)
    published = False
    try:
        _write_private(temporary, payload)
        os.link(temporary, path)
        published = True
        temporary.unlink()
        _fsync(path.parent)
    except BaseException:
        if published:
            _remove_published(path)
        _remove_if_present(temporary)
        raise


def _publish_replace(path: Path, payload: bytes) -> None:
    temporary = _temporary_path(path)
    try:
        _write_private(temporary, payload)
        os.replace(temporary, path)
        _fsync(path.parent)
    except BaseException:
        _remove_if_present(temporary)
        raise


def _write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _remove_published(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and not has_link_or_reparse(metadata)
    ):
        path.unlink()
        _fsync(path.parent)


def _remove_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _temporary_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")


def _project(usage: _Usage, byte_delta: int, entry_delta: int, limits: CheckpointLimits) -> None:
    if usage.bytes + byte_delta > limits.max_bytes:
        raise RuntimeError("CRM activities checkpoint exceeds byte ceiling")
    if usage.entries + entry_delta > limits.max_entries:
        raise RuntimeError("CRM activities checkpoint exceeds entry ceiling")


def _final_name_allowed(name: str, relative: tuple[str, ...]) -> bool:
    if relative == ():
        return name.endswith(".json") and Path(name).name == name and not name.startswith(".")
    if relative == ("records",):
        return _RECORD.fullmatch(name) is not None
    if relative == ("pages",):
        return _PAGE.fullmatch(name) is not None
    return False


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _limits(limits: CheckpointLimits) -> None:
    if not isinstance(limits, CheckpointLimits):
        raise TypeError("checkpoint APIs require CheckpointLimits")


def _lexical_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _fsync(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
