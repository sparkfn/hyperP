"""Exact checkpoint evidence and resume validation helpers."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from intelligence.crm.activities.checkpoint_limits import CheckpointLimits
from intelligence.crm.activities.checkpoint_storage import checkpoint_directory, read_json
from intelligence.crm.activities.model_parsing import (
    parse_boundary,
    parse_request,
    record_from_mapping,
)
from intelligence.crm.activities.models import (
    ArchiveRequest,
    Disposition,
    SealedBoundary,
    sha256_json,
)

_SCHEMA = "crm-activities-checkpoint-v1"
_PAGE_SCHEMA = "crm-activities-checkpoint-page-v1"
_REQUEST_KEYS = frozenset(
    {
        "snapshot_id",
        "source_instance_id",
        "source_key",
        "page_size",
        "max_rows",
        "max_pages",
        "database_identity",
        "selection_contract_version",
        "max_references_per_record",
    }
)


def validate_request_evidence(value: object) -> ArchiveRequest:
    mapping = _mapping(value, "request")
    _exact_keys(mapping, _REQUEST_KEYS, "checkpoint request")
    request = parse_request(mapping)
    if dict(mapping) != request.as_public_dict():
        raise ValueError("checkpoint request is not canonical")
    return request


def validate_boundary_evidence(value: object) -> Mapping[str, object]:
    mapping = _mapping(value, "boundary")
    _exact_keys(mapping, {"schema_version", "request", "entries", "digest"}, "sealed boundary")
    boundary = parse_boundary(mapping)
    if dict(mapping) != boundary.as_dict():
        raise ValueError("sealed boundary is not canonical")
    return mapping


def validate_state_evidence(value: object) -> Mapping[str, object]:
    mapping = _mapping(value, "checkpoint")
    current_phase = mapping.get("phase")
    if current_phase == "new":
        _exact_keys(mapping, {"schema_version", "phase", "request_digest"}, "checkpoint state")
        _digest(mapping.get("request_digest"), "checkpoint request digest")
    elif current_phase in {"sealed", "paging", "completed"}:
        _exact_keys(
            mapping,
            {"schema_version", "phase", "request_digest", "boundary_digest", "pages"},
            "checkpoint state",
        )
        _digest(mapping.get("request_digest"), "checkpoint request digest")
        _digest(mapping.get("boundary_digest"), "checkpoint boundary digest")
        page_count(mapping.get("pages"))
    else:
        raise ValueError("checkpoint phase is corrupt")
    if mapping.get("schema_version") != _SCHEMA:
        raise ValueError("unsupported CRM activities checkpoint")
    return mapping


def validate_page_shape(value: Mapping[str, object]) -> None:
    _exact_keys(
        value,
        {
            "schema_version",
            "request_digest",
            "boundary_digest",
            "identities",
            "records",
            "dispositions",
            "digest",
        },
        "checkpoint page",
    )
    if value.get("schema_version") != _PAGE_SCHEMA:
        raise ValueError("checkpoint page schema is invalid")
    _digest(value.get("request_digest"), "checkpoint page request digest")
    _digest(value.get("boundary_digest"), "checkpoint page boundary digest")
    identities = value.get("identities")
    if not isinstance(identities, list) or not all(isinstance(item, str) for item in identities):
        raise ValueError("checkpoint page identities are invalid")
    records = value.get("records")
    dispositions = value.get("dispositions")
    if not isinstance(records, list) or not isinstance(dispositions, list):
        raise ValueError("checkpoint page collections are invalid")
    digest = value.get("digest")
    _digest(digest, "checkpoint page digest")
    unsigned = dict(value)
    unsigned.pop("digest")
    if digest != sha256_json(unsigned):
        raise ValueError("checkpoint page digest is corrupt")


def validate_page_inventory(
    root: Path,
    boundary: SealedBoundary,
    pages: int,
    limits: CheckpointLimits,
) -> int:
    """Validate committed pages plus at most one post-write cursor interruption.

    The returned cursor is the durable page count. Callers that advance state
    must persist it only after this function returns successfully.
    """
    directory = checkpoint_directory(root, ("pages",), limits)
    files = tuple(sorted(directory.iterdir(), key=lambda item: item.name))
    if len(files) not in {pages, pages + 1}:
        raise RuntimeError("checkpoint page inventory is incomplete")
    for ordinal, path in enumerate(files, start=1):
        if path.name != f"page-{ordinal:08d}.json":
            raise RuntimeError("checkpoint page inventory is out of order")
        page = _mapping(read_json(root, ("pages", path.name), limits), "checkpoint page")
        validate_page_shape(page)
        _validate_page_evidence(page, boundary, ordinal)
    return len(files)


def request_digest(request: ArchiveRequest) -> str:
    return sha256_json(request.as_public_dict())


def validate_boundary_digest(value: str) -> None:
    _digest(value, "checkpoint boundary digest")


def page_count(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("checkpoint page cursor is corrupt")
    return value


def phase(value: Mapping[str, object]) -> str:
    current_phase = value.get("phase")
    if not isinstance(current_phase, str):
        raise RuntimeError("checkpoint phase is corrupt")
    return current_phase


def _validate_page_evidence(
    page: Mapping[str, object], boundary: SealedBoundary, ordinal: int
) -> None:
    start = (ordinal - 1) * boundary.request.page_size
    entries = boundary.entries[start : start + boundary.request.page_size]
    identities = [entry.source_record_pk for entry in entries]
    if (
        page["request_digest"] != request_digest(boundary.request)
        or page["boundary_digest"] != boundary.digest
        or page["identities"] != identities
    ):
        raise RuntimeError("checkpoint page identities conflict with sealed boundary")
    records = _object_list(page.get("records"), "checkpoint page records")
    dispositions = _object_list(page.get("dispositions"), "checkpoint page dispositions")
    if len(records) != len(entries) or len(dispositions) != len(entries):
        raise RuntimeError("checkpoint page evidence has the wrong cardinality")
    for entry, record_value, disposition_value in zip(entries, records, dispositions, strict=True):
        record_mapping = _mapping(record_value, "checkpoint page record")
        record = record_from_mapping(record_mapping)
        if (
            dict(record_mapping) != record.as_dict()
            or record.source_record_pk != entry.source_record_pk
            or record.digest() != entry.record_digest
            or record.reference_fingerprint() != entry.reference_fingerprint
        ):
            raise RuntimeError("checkpoint page record conflicts with sealed boundary")
        disposition_mapping = _mapping(disposition_value, "checkpoint page disposition")
        _exact_keys(
            disposition_mapping,
            {"source_record_pk", "disposition", "reason_code"},
            "checkpoint page disposition",
        )
        disposition = Disposition(
            _string(disposition_mapping.get("source_record_pk"), "checkpoint disposition id"),
            _disposition(disposition_mapping.get("disposition")),
            _nullable_string(
                disposition_mapping.get("reason_code"), "checkpoint disposition reason"
            ),
        )
        if disposition.source_record_pk != entry.source_record_pk:
            raise RuntimeError("checkpoint page disposition conflicts with sealed boundary")


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{field} must use string keys")
        result[key] = item
    return result


def _object_list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{field} are invalid")
    return list(value)


def _exact_keys(
    value: Mapping[str, object], expected: set[str] | frozenset[str], field: str
) -> None:
    if set(value) != expected:
        raise ValueError(f"{field} has unexpected fields")


def _digest(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{field} is invalid")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} is invalid")
    return value


def _nullable_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _string(value, field)


def _disposition(value: object) -> str:
    if isinstance(value, str) and value in {"accepted", "rejected", "quarantined"}:
        return value
    raise ValueError("checkpoint disposition is invalid")
