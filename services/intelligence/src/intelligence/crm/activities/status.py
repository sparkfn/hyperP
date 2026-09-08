"""Bounded read-only status for CRM activity archive checkpoints."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from intelligence.crm.activities.acceptance import (
    PublicationCandidate,
    VerificationCandidate,
    _parse_publication,
    _parse_verification,
)
from intelligence.crm.activities.bounded import (
    STATUS_READ_LIMITS,
    ReadBudget,
    ReadLimits,
    checkpoint_root,
    read_evidence,
    records,
)
from intelligence.crm.activities.model_parsing import parse_boundary, record_from_mapping
from intelligence.crm.activities.models import (
    ArchiveRecord,
    Disposition,
    DispositionKind,
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
    """Return bounded checkpoint state without inspecting accepted snapshot content."""
    validate_snapshot_id(checkpoint_id)
    root = checkpoint_root(workspace, checkpoint_id)
    if root is None:
        return {"checkpoint_id": checkpoint_id, "state": "absent"}
    budget = ReadBudget(limits)
    state = _state(_required_evidence(root, "checkpoint.json", budget))
    boundary_value = read_evidence(root, "boundary.json", budget)
    _add_rows(boundary_value, "entries", budget)
    boundary = _boundary(boundary_value)
    dispositions = read_evidence(root, "dispositions.json", budget)
    _add_rows(dispositions, "outcomes", budget)
    manifest = read_evidence(root, "accepted-manifest.json", budget)
    _add_rows(manifest, "record_page_digests", budget)
    publication = _publication(
        _candidate_evidence(root, "publication-candidate.json", budget), checkpoint_id
    )
    verification = _verification(
        _candidate_evidence(root, "verification-candidate.json", budget), checkpoint_id
    )
    outcomes = _validate_dispositions(dispositions, boundary)
    _validate_manifest(manifest, boundary)
    _validate_candidates(publication, verification, manifest, boundary)
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
        "manifest_digest": None if manifest is None else manifest.get("digest"),
        "cleanup_identity_digest": (
            None if manifest is None else manifest.get("cleanup_identity_digest")
        ),
        "verification": None if verification is None else dict(verification.raw),
    }


def _required_evidence(root: Path, name: str, budget: ReadBudget) -> dict[str, object]:
    value = read_evidence(root, name, budget)
    if value is None:
        raise ValueError("checkpoint evidence is missing")
    return value


def _candidate_evidence(root: Path, name: str, budget: ReadBudget) -> dict[str, object] | None:
    value = read_evidence(root, name, budget)
    _add_rows(value, "inventory", budget)
    return value


def _add_rows(value: dict[str, object] | None, key: str, budget: ReadBudget) -> None:
    if value is not None and isinstance(value.get(key), list):
        budget.add_rows(len(value[key]))


def _state(value: dict[str, object]) -> dict[str, object]:
    phase = value.get("phase")
    if phase == "new":
        expected = {"schema_version", "phase"}
    else:
        expected = {"schema_version", "phase", "boundary_digest", "pages"}
    if set(value) != expected or value.get("schema_version") != "crm-activities-checkpoint-v1":
        raise ValueError("checkpoint state is corrupt")
    if phase not in {"new", "sealed", "paging", "completed"}:
        raise ValueError("checkpoint state is corrupt")
    if phase != "new":
        digest = value.get("boundary_digest")
        pages = value.get("pages")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or not isinstance(pages, int)
            or isinstance(pages, bool)
            or pages < 0
        ):
            raise ValueError("checkpoint state is corrupt")
    return value


def _boundary(value: dict[str, object] | None) -> SealedBoundary | None:
    if value is None:
        return None
    boundary = parse_boundary(value)
    if value != boundary.as_dict():
        raise ValueError("sealed boundary is noncanonical")
    return boundary


def _publication(
    value: dict[str, object] | None, checkpoint_id: str
) -> PublicationCandidate | None:
    return None if value is None else _parse_publication(value, checkpoint_id)


def _verification(
    value: dict[str, object] | None, checkpoint_id: str
) -> VerificationCandidate | None:
    return None if value is None else _parse_verification(value, checkpoint_id)


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
            _text(item, "source_record_pk"),
            cast(DispositionKind, kind),
            item.get("reason_code") if isinstance(item.get("reason_code"), str) else None,
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


def _validate_candidates(
    publication: PublicationCandidate | None,
    verification: VerificationCandidate | None,
    manifest: dict[str, object] | None,
    boundary: SealedBoundary | None,
) -> None:
    if publication is not None:
        if boundary is None or manifest is None:
            raise ValueError("publication candidate linkage is incomplete")
        if (
            publication.request != boundary.request
            or publication.boundary_digest != boundary.digest
            or publication.snapshot_id != manifest.get("snapshot_id")
            or publication.manifest_digest != manifest.get("digest")
            or publication.cleanup_identity_digest != manifest.get("cleanup_identity_digest")
        ):
            raise ValueError("publication candidate linkage is inconsistent")
    if verification is not None:
        if publication is None:
            raise ValueError("verification candidate has no publication candidate")
        if (
            verification.accepted_run_id != publication.run_id
            or verification.snapshot_id != publication.snapshot_id
            or verification.accepted_manifest_digest != publication.manifest_digest
        ):
            raise ValueError("verification candidate linkage is inconsistent")


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
    missing_stored = 0
    missing_graph = 0
    conflicting = 0
    resolved = 0
    for record in records:
        stored = record.stored_parent
        if stored.source_record_id is None:
            missing_stored += 1
            continue
        matches = tuple(
            parent
            for parent in record.child_parents
            if (
                parent.source_record_id == stored.source_record_id
                and parent.source_instance_id == stored.source_instance_id
                and parent.record_type == stored.record_type
                and parent.source_system == stored.source_system
            )
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


def _text(value: dict[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"{key} is required")
    return result
