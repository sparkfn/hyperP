"""Canonical snapshot, cleanup, and verification artifact writers."""

from __future__ import annotations

import json
import os
import stat
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import cast

from intelligence.artifacts import canonical_json
from intelligence.crm.activities.models import (
    PROVENANCE,
    ArchiveRecord,
    Disposition,
    SealedBoundary,
    sha256_json,
)
from intelligence.crm.activities.reconciliation import counts

_MANIFEST_SCHEMA = "crm-activities-manifest-v1"
_LOGICAL_PAGE_SIZE = 100


def write_snapshot(
    run_staging: Path,
    boundary: SealedBoundary,
    records: tuple[ArchiveRecord, ...],
    outcomes: tuple[Disposition, ...],
) -> Mapping[str, object]:
    """Create deterministic domain files below the foundation-controlled current run staging."""
    destination = run_staging / "snapshots" / "crm" / "activities" / boundary.logical_snapshot_id
    _directory(destination)
    _directory(destination / "records")
    accepted = {item.source_record_pk for item in outcomes if item.disposition == "accepted"}
    records_by_id = {item.source_record_pk: item for item in records}
    accepted_records = tuple(records_by_id[item] for item in sorted(accepted))
    page_digests: list[str] = []
    for ordinal, chunk in enumerate(_chunks(accepted_records, _LOGICAL_PAGE_SIZE), start=1):
        page = {
            "schema_version": "crm-activities-page-v1",
            "records": [item.as_dict() for item in chunk],
        }
        page["digest"] = sha256_json(page)
        _write_exact(destination / "records" / f"page-{ordinal:08d}.json", page)
        page_digests.append(cast(str, page["digest"]))
    rejected = _outcome_rows(records_by_id, outcomes, "rejected")
    quarantined = _outcome_rows(records_by_id, outcomes, "quarantined")
    unresolved = _unresolved_rows(records_by_id, outcomes)
    cleanup = _cleanup_records(accepted_records, outcomes)
    _write_exact(destination / "boundary.json", boundary.as_dict())
    _write_exact(
        destination / "rejected.json",
        {"records": rejected, "digest": sha256_json(rejected)},
    )
    _write_exact(
        destination / "quarantined.json",
        {"records": quarantined, "digest": sha256_json(quarantined)},
    )
    _write_exact(
        destination / "unresolved-references.json",
        {"records": unresolved, "digest": sha256_json(unresolved)},
    )
    cleanup_payload: dict[str, object] = {
        "schema_version": "crm-activities-cleanup-identities-v1",
        "identities": cleanup,
        "count": len(cleanup),
    }
    cleanup_payload["digest"] = sha256_json(cleanup_payload)
    _write_exact(destination / "cleanup-identities.json", cleanup_payload)
    summary = counts(outcomes)
    if len(records) != sum(summary.values()):
        raise RuntimeError("archive partition count has unexplained remainder")
    manifest: dict[str, object] = {
        "schema_version": _MANIFEST_SCHEMA,
        "snapshot_id": boundary.logical_snapshot_id,
        "boundary_digest": boundary.digest,
        "selected_count": len(records),
        "accepted_count": summary["accepted"],
        "rejected_count": summary["rejected"],
        "quarantined_count": summary["quarantined"],
        "unexplained_remainder": 0,
        "record_page_digests": page_digests,
        "cleanup_identity_count": len(cleanup),
        "cleanup_identity_digest": cleanup_payload["digest"],
        "provenance": dict(PROVENANCE),
    }
    manifest["digest"] = sha256_json(manifest)
    _write_exact(destination / "manifest.json", manifest)
    return manifest


def write_verification(run_staging: Path, evidence: Mapping[str, object]) -> None:
    target = run_staging / "verifications" / "crm" / "activities"
    _directory(target)
    payload = dict(evidence)
    payload["schema_version"] = "crm-activities-verification-v1"
    payload["digest"] = sha256_json(payload)
    snapshot_id = payload.get("snapshot_id")
    if not isinstance(snapshot_id, str):
        raise ValueError("verification snapshot identity is invalid")
    _write_exact(target / f"{snapshot_id}.json", payload)


def _cleanup_records(
    records: Sequence[ArchiveRecord], outcomes: Sequence[Disposition]
) -> list[dict[str, object]]:
    accepted = {item.source_record_pk for item in outcomes if item.disposition == "accepted"}
    result: list[dict[str, object]] = []
    for record in records:
        if record.source_record_pk not in accepted:
            raise RuntimeError("cleanup set contains a non-accepted record")
        if record.record_type == "call":
            parents = tuple(
                item for item in record.child_parents if item.record_type == "crm_history"
            )
            parent_is_accepted = len(parents) == 1 and parents[0].source_record_pk in accepted
            if not parent_is_accepted:
                message = " ".join(
                    ("cleanup-eligible companion call lacks accepted", "activity parent")
                )
                raise RuntimeError(message)
        result.append(
            {
                "source_record_pk": record.source_record_pk,
                "record_type": record.record_type,
                "source_record_version": record.source_record_version,
                "record_hash": record.record_hash,
                "lifecycle_status": record.lifecycle_status,
                "event_at": record.event_at,
                "observed_at": record.observed_at,
                "ingested_at": record.ingested_at,
                "available_at": record.available_at,
                "stored_parent": asdict(record.stored_parent),
            }
        )
    return sorted(result, key=lambda item: cast(str, item["source_record_pk"]))


def _outcome_rows(
    records: Mapping[str, ArchiveRecord], outcomes: Sequence[Disposition], kind: str
) -> list[dict[str, object]]:
    result = []
    for outcome in outcomes:
        if outcome.disposition == kind:
            record = records[outcome.source_record_pk]
            result.append(
                {
                    "source_record_pk": record.source_record_pk,
                    "record_type": record.record_type,
                    "reason_code": outcome.reason_code,
                    "reference_fingerprint": record.reference_fingerprint(),
                }
            )
    return result


def _unresolved_rows(
    records: Mapping[str, ArchiveRecord], outcomes: Sequence[Disposition]
) -> list[dict[str, object]]:
    outcome_by_id = {item.source_record_pk: item for item in outcomes}
    result: list[dict[str, object]] = []
    for identity, record in sorted(records.items()):
        reasons: list[str] = []
        if record.stored_parent.source_record_id is None:
            reasons.append("missing_stored_parent")
        elif not _stored_parent_resolves(record):
            reasons.append("missing_or_conflicting_graph_parent")
        if record.ingested_at is None:
            reasons.append("missing_ingested_at")
        if len(record.people) != 1:
            reasons.append("unresolved_person_association")
        elif record.people[0].revision is None:
            reasons.append("missing_person_revision")
        if not record.user_capabilities:
            reasons.append("user_capabilities_unavailable")
        outcome = outcome_by_id[identity]
        if outcome.disposition != "accepted" and outcome.reason_code is not None:
            reasons.append(outcome.reason_code)
        if reasons:
            result.append({"source_record_pk": identity, "reasons": sorted(set(reasons))})
    return result


def _stored_parent_resolves(record: ArchiveRecord) -> bool:
    stored = record.stored_parent
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
    return len(matches) == 1 and len(record.child_parents) == 1


def _chunks(values: Sequence[ArchiveRecord], size: int) -> tuple[tuple[ArchiveRecord, ...], ...]:
    return tuple(tuple(values[index : index + size]) for index in range(0, len(values), size))


def _directory(path: Path) -> None:
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise ValueError("snapshot artifact path is unsafe")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


def _write_exact(path: Path, value: Mapping[str, object]) -> None:
    if path.exists():
        if _read_json(path) != dict(value):
            raise RuntimeError("immutable snapshot artifact conflicts")
        return
    payload = canonical_json(dict(value)).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> object:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ValueError("snapshot artifact is unsafe")
    return json.loads(path.read_text(encoding="utf-8"))


def _object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return cast(Mapping[str, object], value)


from intelligence.crm.activities.snapshot_verifier import (  # noqa: E402, F401
    snapshot_inventory,
    verify_snapshot,
)
