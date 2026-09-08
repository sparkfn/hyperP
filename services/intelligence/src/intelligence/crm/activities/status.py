"""Bounded read-only status for CRM activity archive checkpoints."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from intelligence.crm.activities.acceptance import (
    AcceptanceDescriptor,
    PublicationPointer,
    VerificationCandidate,
    parse_descriptor,
    read_publication_candidates,
    read_verification_candidates,
)
from intelligence.crm.activities.bounded import (
    STATUS_READ_LIMITS,
    ReadBudget,
    ReadLimits,
    checkpoint_root,
    read_evidence,
    read_published_evidence,
    records,
)
from intelligence.crm.activities.checkpoint_resume import request_digest
from intelligence.crm.activities.model_parsing import (
    parse_boundary,
    parse_request,
    record_from_mapping,
)
from intelligence.crm.activities.models import (
    ArchiveRecord,
    ArchiveRequest,
    Disposition,
    SealedBoundary,
    sha256_json,
    validate_snapshot_id,
)

_MANIFEST_KEYS = {
    "schema_version",
    "snapshot_id",
    "boundary_digest",
    "selected_count",
    "accepted_count",
    "rejected_count",
    "quarantined_count",
    "unexplained_remainder",
    "record_page_digests",
    "cleanup_identity_count",
    "cleanup_identity_digest",
    "provenance",
    "digest",
}


def status(
    workspace: Path, checkpoint_id: str, limits: ReadLimits = STATUS_READ_LIMITS
) -> dict[str, object]:
    """Return bounded checkpoint state without walking accepted snapshot files."""
    validate_snapshot_id(checkpoint_id)
    root = checkpoint_root(workspace, checkpoint_id)
    if root is None:
        return {"checkpoint_id": checkpoint_id, "state": "absent"}
    budget = ReadBudget(limits)
    state = _state(_required(root, "checkpoint.json", budget))
    request = _request(_required(root, "request.json", budget), checkpoint_id)
    if state["request_digest"] != request_digest(request):
        raise ValueError("checkpoint state request digest is invalid")
    boundary_value = read_evidence(root, "boundary.json", budget)
    _add_rows(boundary_value, "entries", budget)
    boundary = _boundary(boundary_value)
    if boundary is not None and boundary.request != request:
        raise ValueError("sealed boundary request conflicts with checkpoint request")
    dispositions = read_evidence(root, "dispositions.json", budget)
    _add_rows(dispositions, "outcomes", budget)
    manifest = read_evidence(root, "accepted-manifest.json", budget)
    _add_rows(manifest, "record_page_digests", budget)
    outcomes = _validate_dispositions(dispositions, boundary)
    _validate_manifest(manifest, boundary)
    publication, pointer = _publication_status(workspace, checkpoint_id)
    verification = _verification_status(workspace, checkpoint_id, publication, pointer)
    _validate_descriptor_linkage(publication, request, boundary, manifest)
    archive_records = _records(root, budget)
    source_instance = None if boundary is None else boundary.request.source_instance_id
    return {
        "checkpoint_id": checkpoint_id,
        "snapshot_id": None if manifest is None else manifest.get("snapshot_id"),
        "source_instance_id": source_instance,
        "state": state,
        "boundary_digest": None if boundary is None else boundary.digest,
        "disposition_counts": _outcome_counts(outcomes),
        "duplicate_delivery_count": 0,
        "parent_resolution": _parent_summary(archive_records),
        "person_resolution": _person_summary(archive_records),
        "accepted_run": None if publication is None else dict(publication.raw),
        "publication_candidate": None if pointer is None else pointer.as_dict(),
        "manifest_digest": None if manifest is None else manifest.get("digest"),
        "cleanup_identity_digest": None
        if manifest is None
        else manifest.get("cleanup_identity_digest"),
        "verification": None if verification is None else dict(verification.raw),
    }


def _required(root: Path, name: str, budget: ReadBudget) -> dict[str, object]:
    value = read_evidence(root, name, budget)
    if value is None:
        raise ValueError("checkpoint evidence is missing")
    return value


def _state(value: Mapping[str, object]) -> dict[str, object]:
    phase = value.get("phase")
    if phase == "new":
        expected = {"schema_version", "phase", "request_digest"}
    else:
        expected = {"schema_version", "phase", "request_digest", "boundary_digest", "pages"}
    if set(value) != expected or value.get("schema_version") != "crm-activities-checkpoint-v1":
        raise ValueError("checkpoint state is corrupt")
    if phase not in {"new", "sealed", "paging", "completed"}:
        raise ValueError("checkpoint state is corrupt")
    _digest(value.get("request_digest"), "checkpoint request digest")
    if phase != "new":
        _digest(value.get("boundary_digest"), "checkpoint boundary digest")
        pages = value.get("pages")
        if not isinstance(pages, int) or isinstance(pages, bool) or pages < 0:
            raise ValueError("checkpoint state is corrupt")
    return dict(value)


def _request(value: Mapping[str, object], checkpoint_id: str) -> ArchiveRequest:
    request = parse_request(value)
    if request.snapshot_id != checkpoint_id or request.as_public_dict() != dict(value):
        raise ValueError("checkpoint request is invalid")
    return request


def _boundary(value: dict[str, object] | None) -> SealedBoundary | None:
    if value is None:
        return None
    boundary = parse_boundary(value)
    if value != boundary.as_dict():
        raise ValueError("sealed boundary is noncanonical")
    return boundary


def _publication_status(
    workspace: Path, checkpoint_id: str
) -> tuple[AcceptanceDescriptor | None, PublicationPointer | None]:
    candidates = read_publication_candidates(workspace, checkpoint_id)
    if len(candidates) > 1:
        return None, None
    if not candidates:
        return None, None
    pointer = candidates[0]
    metadata, raw = read_published_evidence(
        workspace, pointer.run_id, pointer.descriptor_relative_path, ReadBudget(STATUS_READ_LIMITS)
    )
    if len(raw) != pointer.descriptor_byte_count or _sha256(raw) != pointer.descriptor_sha256:
        raise ValueError("publication candidate descriptor bytes are invalid")
    return parse_descriptor(metadata), pointer


def _verification_status(
    workspace: Path,
    checkpoint_id: str,
    publication: AcceptanceDescriptor | None,
    pointer: PublicationPointer | None,
) -> VerificationCandidate | None:
    candidates = read_verification_candidates(workspace, checkpoint_id)
    if len(candidates) > 1:
        return None
    if not candidates:
        return None
    candidate = candidates[0]
    if publication is None or pointer is None:
        raise ValueError("verification candidate has no descriptor-backed publication")
    if (
        candidate.accepted_run_id != publication.run_id
        or candidate.snapshot_id != publication.snapshot_id
        or candidate.accepted_manifest_digest != publication.manifest_digest
        or candidate.accepted_descriptor_sha256 != pointer.descriptor_sha256
    ):
        raise ValueError("verification candidate linkage is inconsistent")
    return candidate


def _validate_descriptor_linkage(
    descriptor: AcceptanceDescriptor | None,
    request: object,
    boundary: SealedBoundary | None,
    manifest: dict[str, object] | None,
) -> None:
    if descriptor is None:
        return
    if boundary is None or manifest is None or descriptor.request != request:
        raise ValueError("publication descriptor linkage is incomplete")
    if (
        descriptor.boundary_digest != boundary.digest
        or descriptor.snapshot_id != manifest.get("snapshot_id")
        or descriptor.manifest_digest != manifest.get("digest")
        or descriptor.cleanup_identity_digest != manifest.get("cleanup_identity_digest")
    ):
        raise ValueError("publication descriptor linkage is inconsistent")


def _validate_dispositions(
    value: dict[str, object] | None, boundary: SealedBoundary | None
) -> tuple[dict[str, object], ...]:
    if value is None:
        return ()
    outcomes = value.get("outcomes")
    if set(value) != {"outcomes", "digest"} or not isinstance(outcomes, list):
        raise ValueError("disposition evidence schema is invalid")
    if value.get("digest") != sha256_json(outcomes):
        raise ValueError("disposition evidence digest is invalid")
    parsed: list[dict[str, object]] = []
    for item in outcomes:
        if not isinstance(item, dict):
            raise ValueError("disposition evidence item is invalid")
        kind = item.get("disposition")
        if kind not in {"accepted", "rejected", "quarantined"}:
            raise ValueError("disposition evidence item is invalid")
        disposition = Disposition(
            _text(item, "source_record_pk"), kind, _nullable_text(item, "reason_code")
        )
        if item != disposition.__dict__:
            raise ValueError("disposition evidence item is noncanonical")
        parsed.append(dict(item))
    identities = [_text(item, "source_record_pk") for item in parsed]
    if identities != sorted(set(identities)):
        raise ValueError("disposition evidence is not sorted unique")
    if boundary is not None and set(identities) != {
        entry.source_record_pk for entry in boundary.entries
    }:
        raise ValueError("disposition evidence does not equal the sealed boundary")
    return tuple(parsed)


def _validate_manifest(value: dict[str, object] | None, boundary: SealedBoundary | None) -> None:
    if value is None:
        return
    if set(value) != _MANIFEST_KEYS or boundary is None:
        raise ValueError("accepted manifest linkage is invalid")
    unsigned = dict(value)
    digest = unsigned.pop("digest")
    if not isinstance(digest, str) or sha256_json(unsigned) != digest:
        raise ValueError("accepted manifest digest is invalid")
    if (
        value.get("snapshot_id") != boundary.logical_snapshot_id
        or value.get("boundary_digest") != boundary.digest
    ):
        raise ValueError("accepted manifest boundary linkage is invalid")


def _records(root: Path, budget: ReadBudget) -> tuple[ArchiveRecord, ...]:
    parsed: list[ArchiveRecord] = []
    for value in records(root, budget):
        record = record_from_mapping(value)
        if value != record.as_dict():
            raise ValueError("checkpoint record is noncanonical")
        parsed.append(record)
    return tuple(parsed)


def _outcome_counts(outcomes: tuple[dict[str, object], ...]) -> dict[str, int]:
    return {
        kind: sum(1 for item in outcomes if item.get("disposition") == kind)
        for kind in ("accepted", "rejected", "quarantined")
    }


def _parent_summary(records: tuple[ArchiveRecord, ...]) -> dict[str, int]:
    missing_stored = missing_graph = conflicting = resolved = 0
    for record in records:
        stored = record.stored_parent
        if stored.source_record_id is None:
            missing_stored += 1
            continue
        matches = tuple(
            parent
            for parent in record.child_parents
            if parent.source_record_id == stored.source_record_id
            and parent.source_instance_id == stored.source_instance_id
            and parent.record_type == stored.record_type
            and parent.source_system == stored.source_system
        )
        if len(matches) == 1 and len(record.child_parents) == 1:
            resolved += 1
        elif not matches:
            missing_graph += 1
        else:
            conflicting += 1
    return {
        "missing_stored": missing_stored,
        "missing_graph": missing_graph,
        "conflicting": conflicting,
        "resolved": resolved,
    }


def _person_summary(records: tuple[ArchiveRecord, ...]) -> dict[str, int]:
    return {
        "missing_or_ambiguous": sum(1 for item in records if len(item.people) != 1),
        "missing_revision": sum(
            1 for item in records if len(item.people) == 1 and item.people[0].revision is None
        ),
    }


def _add_rows(value: dict[str, object] | None, key: str, budget: ReadBudget) -> None:
    if value is not None and isinstance(value.get(key), list):
        budget.add_rows(len(value[key]))


def _digest(value: object, field: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{field} is invalid")


def _sha256(value: bytes) -> str:
    from hashlib import sha256

    return sha256(value).hexdigest()


def _nullable_text(value: Mapping[str, object], key: str) -> str | None:
    result = value.get(key)
    if result is None:
        return None
    if not isinstance(result, str):
        raise ValueError(f"{key} is invalid")
    return result


def _text(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"{key} is required")
    return result
