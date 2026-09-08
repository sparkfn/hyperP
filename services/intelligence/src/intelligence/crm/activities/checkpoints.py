"""Public durable checkpoint facade for CRM activity archive attempts."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from intelligence.crm.activities.checkpoint_limits import CheckpointLimits
from intelligence.crm.activities.checkpoint_resume import (
    page_count,
    phase,
    request_digest,
    validate_boundary_digest,
    validate_boundary_evidence,
    validate_page_inventory,
    validate_page_shape,
    validate_request_evidence,
    validate_state_evidence,
)
from intelligence.crm.activities.checkpoint_storage import (
    checkpoint_directory,
    checkpoint_file,
    create_checkpoint_root,
    current_usage,
    evidence_name,
    is_record_name,
    read_json,
    record_name,
    write_exact,
    write_new,
    write_replace,
)
from intelligence.crm.activities.model_parsing import record_from_mapping
from intelligence.crm.activities.models import ArchiveRequest, SealedBoundary

_SCHEMA = "crm-activities-checkpoint-v1"


def checkpoint_root(run_staging: Path, snapshot_id: str, limits: CheckpointLimits) -> Path:
    """Create or admit the fixed private checkpoint directory below staging."""
    return create_checkpoint_root(run_staging, snapshot_id, limits)


def initialize(root: Path, request: ArchiveRequest, limits: CheckpointLimits) -> None:
    """Write immutable request evidence on first creation, rejecting conflicts."""
    write_exact(root, ("request.json",), request.as_public_dict(), limits)
    state_path = checkpoint_file(root, ("checkpoint.json",), limits)
    if state_path.exists():
        state(root, limits)
        return
    write_new(
        root,
        ("checkpoint.json",),
        {
            "schema_version": _SCHEMA,
            "phase": "new",
            "request_digest": _request_digest(root, limits),
        },
        limits,
    )


def write_boundary(root: Path, boundary: SealedBoundary, limits: CheckpointLimits) -> None:
    if not boundary_capture_allowed(root, limits):
        if load_boundary(root, limits) != boundary.as_dict():
            raise RuntimeError("checkpoint boundary conflicts with existing sealed boundary")
        return
    write_exact(root, ("boundary.json",), boundary.as_dict(), limits)
    _write_state(root, "sealed", boundary.digest, 0, limits)


def load_request(root: Path, limits: CheckpointLimits) -> ArchiveRequest:
    return validate_request_evidence(read_json(root, ("request.json",), limits))


def load_boundary(root: Path, limits: CheckpointLimits) -> Mapping[str, object]:
    return validate_boundary_evidence(read_json(root, ("boundary.json",), limits))


def write_record(
    root: Path,
    source_record_pk: str,
    value: Mapping[str, object],
    limits: CheckpointLimits,
) -> None:
    write_exact(root, ("records", record_name(source_record_pk)), dict(value), limits)


def read_record(
    root: Path, source_record_pk: str, limits: CheckpointLimits
) -> Mapping[str, object]:
    value = read_json(root, ("records", record_name(source_record_pk)), limits)
    record = _validate_record(value)
    if record["source_record_pk"] != source_record_pk:
        raise ValueError("checkpoint record identity or shape does not match its file")
    return record


def records(root: Path, limits: CheckpointLimits) -> tuple[Mapping[str, object], ...]:
    current_usage(root, limits)
    directory = checkpoint_directory(root, ("records",), limits)
    paths = tuple(sorted(directory.iterdir(), key=lambda item: item.name))
    result: list[Mapping[str, object]] = []
    for path in paths:
        if not is_record_name(path.name):
            raise ValueError("checkpoint record inventory is unsafe")
        result.append(_mapping(read_json(root, ("records", path.name), limits), "record"))
    return tuple(result)


def write_page(
    root: Path, ordinal: int, value: Mapping[str, object], limits: CheckpointLimits
) -> None:
    if ordinal < 1:
        raise ValueError("page ordinal must be positive")
    validate_page_shape(value)
    current = state(root, limits)
    current_phase = phase(current)
    committed = page_count(current.get("pages", 0)) if current_phase != "new" else 0
    if current_phase == "new" or ordinal > committed + 1:
        raise RuntimeError("checkpoint page ordinal skips durable progress")
    if current_phase == "completed" and ordinal > committed:
        raise RuntimeError("completed checkpoint cannot accept a new page")
    write_exact(root, ("pages", f"page-{ordinal:08d}.json"), dict(value), limits)


def write_evidence(
    root: Path,
    name: str,
    value: Mapping[str, object],
    limits: CheckpointLimits,
) -> None:
    """Persist immutable single-file domain evidence below a checkpoint root."""
    evidence_name(name)
    write_exact(root, (name,), dict(value), limits)


def read_evidence(root: Path, name: str, limits: CheckpointLimits) -> Mapping[str, object]:
    evidence_name(name)
    return _mapping(read_json(root, (name,), limits), "checkpoint evidence")


def bounded_usage(root: Path, limits: CheckpointLimits) -> None:
    """Validate current checkpoint contents and resource ceilings before admission."""
    current_usage(root, limits)


def advance(root: Path, boundary_digest: str, ordinal: int, limits: CheckpointLimits) -> None:
    validate_boundary_digest(boundary_digest)
    if ordinal < 1:
        raise ValueError("checkpoint page cursor is invalid")
    current = state(root, limits)
    current_phase = phase(current)
    if current_phase == "new" or current.get("boundary_digest") != boundary_digest:
        raise RuntimeError("checkpoint cursor conflicts with sealed boundary")
    committed = page_count(current.get("pages"))
    if current_phase == "completed":
        if ordinal == committed:
            return
        raise RuntimeError("completed checkpoint cursor cannot change")
    if current_phase == "sealed" and committed != 0:
        raise RuntimeError("sealed checkpoint has an invalid page cursor")
    if ordinal < committed:
        raise RuntimeError("checkpoint cursor cannot regress")
    if ordinal == committed:
        return
    if ordinal != committed + 1:
        raise RuntimeError("checkpoint cursor skips durable progress")
    _write_state(root, "paging", boundary_digest, ordinal, limits)


def complete(root: Path, boundary_digest: str, pages: int, limits: CheckpointLimits) -> None:
    validate_boundary_digest(boundary_digest)
    if pages < 0:
        raise ValueError("checkpoint page cursor is invalid")
    current = state(root, limits)
    current_phase = phase(current)
    if current_phase == "new" or current.get("boundary_digest") != boundary_digest:
        raise RuntimeError("checkpoint completion conflicts with sealed boundary")
    committed = page_count(current.get("pages"))
    if committed != pages:
        raise RuntimeError("checkpoint completion skips durable progress")
    if current_phase == "completed":
        return
    if current_phase == "sealed" and pages != 0:
        raise RuntimeError("sealed checkpoint has an invalid page cursor")
    _write_state(root, "completed", boundary_digest, pages, limits)


def state(root: Path, limits: CheckpointLimits) -> Mapping[str, object]:
    return validate_state_evidence(read_json(root, ("checkpoint.json",), limits))


def boundary_capture_allowed(root: Path, limits: CheckpointLimits) -> bool:
    """Return whether capture may create a boundary for this checkpoint.

    Integration requirement: command orchestration must call this before graph
    capture. A missing boundary after any sealed, paging, or completed state is
    corruption, never permission to recapture a different source population.
    """
    current = state(root, limits)
    current_phase = phase(current)
    boundary_path = checkpoint_file(root, ("boundary.json",), limits)
    boundary_exists = boundary_path.exists()
    if current_phase == "new":
        if not boundary_exists:
            return True
        # A crash may occur after immutable boundary publication and before
        # mutable state replacement.  The durable boundary is authoritative;
        # validate its request binding and seal that exact evidence rather than
        # recapturing a different source population.
        boundary = validate_boundary_evidence(load_boundary(root, limits))
        if boundary.get("request") != load_request(root, limits).as_public_dict():
            raise RuntimeError("new checkpoint boundary conflicts with immutable request")
        boundary_digest = boundary.get("digest")
        if not isinstance(boundary_digest, str):
            raise RuntimeError("new checkpoint boundary digest is invalid")
        _write_state(root, "sealed", boundary_digest, 0, limits)
        return False
    if not boundary_exists:
        raise RuntimeError("sealed checkpoint state is missing its boundary evidence")
    return False


def validate_resume(
    root: Path,
    request: ArchiveRequest,
    boundary: SealedBoundary,
    limits: CheckpointLimits,
) -> int:
    """Return the validated durable page cursor without mutating checkpoint state."""
    current_usage(root, limits)
    if load_request(root, limits) != request:
        raise RuntimeError("checkpoint request conflicts with resume request")
    current = state(root, limits)
    if current.get("request_digest") != request_digest(request):
        raise RuntimeError("checkpoint state conflicts with resume request")
    actual_boundary = validate_boundary_evidence(load_boundary(root, limits))
    if actual_boundary != boundary.as_dict():
        raise RuntimeError("checkpoint boundary conflicts with resume boundary")
    current_phase = phase(current)
    if current_phase == "new":
        raise RuntimeError("CRM activities resume requires a durable sealed checkpoint")
    if current.get("boundary_digest") != boundary.digest:
        raise RuntimeError("checkpoint state conflicts with sealed boundary")
    pages = page_count(current.get("pages"))
    expected_pages = (len(boundary.entries) + request.page_size - 1) // request.page_size
    if pages > expected_pages or (current_phase == "completed" and pages != expected_pages):
        raise RuntimeError("checkpoint page cursor conflicts with boundary")
    if current_phase == "sealed" and pages != 0:
        raise RuntimeError("sealed checkpoint has an invalid page cursor")
    if current_phase == "paging" and pages == 0:
        raise RuntimeError("paging checkpoint has an invalid page cursor")
    durable_pages = validate_page_inventory(root, boundary, pages, limits)
    if durable_pages > expected_pages:
        raise RuntimeError("checkpoint page inventory exceeds sealed boundary")
    return durable_pages


def resume_cursor(
    root: Path,
    request: ArchiveRequest,
    boundary: SealedBoundary,
    limits: CheckpointLimits,
) -> int:
    """Validate and durably acknowledge one valid page written before interruption.

    Integration requirement: resume writers must start strictly after the
    returned cursor. This prevents recomputing or cursor-regressing pages that
    were already committed before the process stopped.
    """
    durable_pages = validate_resume(root, request, boundary, limits)
    current = state(root, limits)
    committed = page_count(current.get("pages", 0)) if phase(current) != "new" else 0
    if durable_pages == committed + 1:
        advance(root, boundary.digest, durable_pages, limits)
    elif durable_pages != committed:
        raise RuntimeError("checkpoint page inventory has more than one unadvanced page")
    return durable_pages


def _write_state(
    root: Path,
    current_phase: str,
    boundary_digest: str,
    pages: int,
    limits: CheckpointLimits,
) -> None:
    validate_boundary_digest(boundary_digest)
    if pages < 0:
        raise ValueError("checkpoint page cursor is invalid")
    write_replace(
        root,
        ("checkpoint.json",),
        {
            "schema_version": _SCHEMA,
            "phase": current_phase,
            "request_digest": _request_digest(root, limits),
            "boundary_digest": boundary_digest,
            "pages": pages,
        },
        limits,
    )


def _request_digest(root: Path, limits: CheckpointLimits) -> str:
    return request_digest(load_request(root, limits))


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{field} must use string keys")
        result[key] = item
    return result


def _validate_record(value: object) -> Mapping[str, object]:
    mapping = _mapping(value, "record")
    record = record_from_mapping(mapping)
    if dict(mapping) != record.as_dict():
        raise ValueError("checkpoint record identity or shape does not match its file")
    return mapping
