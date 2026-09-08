"""Strict canonical snapshot verification and inventory helpers."""

from __future__ import annotations

import stat
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from intelligence.artifacts import canonical_json, sha256_file
from intelligence.crm.activities.manifests import _cleanup_records, _object, _read_json
from intelligence.crm.activities.models import (
    PROVENANCE,
    Disposition,
    SealedBoundary,
    parse_boundary,
    record_from_mapping,
    sha256_json,
)

_MANIFEST_SCHEMA = "crm-activities-manifest-v1"


def verify_snapshot(snapshot: Path) -> Mapping[str, object]:
    """Strictly verify the complete canonical domain inventory without Neo4j access."""
    _safe_directory(snapshot)
    paths = _inventory_paths(snapshot)
    manifest = _json_object(snapshot / "manifest.json", "manifest")
    _validate_manifest_shape(manifest)
    original_digest = manifest.get("digest")
    unsigned = dict(manifest)
    unsigned.pop("digest", None)
    if not isinstance(original_digest, str) or sha256_json(unsigned) != original_digest:
        raise ValueError("archive manifest digest is invalid")
    if (
        manifest.get("schema_version") != _MANIFEST_SCHEMA
        or manifest.get("provenance") != PROVENANCE
    ):
        raise ValueError("archive manifest contract/provenance is invalid")
    raw_boundary = _json_object(snapshot / "boundary.json", "boundary")
    _validate_boundary_shape(raw_boundary)
    boundary = parse_boundary(raw_boundary)
    if boundary.digest != manifest.get("boundary_digest"):
        raise ValueError("archive boundary linkage is invalid")
    if manifest.get("snapshot_id") != boundary.logical_snapshot_id:
        raise ValueError("logical snapshot identity is invalid")
    cleanup = _json_object(snapshot / "cleanup-identities.json", "cleanup identities")
    _validate_cleanup_shape(cleanup)
    cleanup_unsigned = dict(cleanup)
    cleanup_digest = cleanup_unsigned.pop("digest", None)
    if (
        cleanup_digest != manifest.get("cleanup_identity_digest")
        or sha256_json(cleanup_unsigned) != cleanup_digest
    ):
        raise ValueError("cleanup identity set linkage is invalid")
    identities = cleanup.get("identities")
    if not isinstance(identities, list) or cleanup.get("count") != len(identities):
        raise ValueError("cleanup identity set is invalid")
    keys = [item.get("source_record_pk") for item in identities if isinstance(item, dict)]
    if len(keys) != len(identities) or keys != sorted(set(keys)):
        raise ValueError("cleanup identity set is not sorted unique")
    pages = manifest.get("record_page_digests")
    if not isinstance(pages, list) or not all(isinstance(item, str) for item in pages):
        raise ValueError("record page inventory is invalid")
    actual_pages = tuple(sorted((snapshot / "records").glob("page-*.json")))
    if len(actual_pages) != len(pages):
        raise ValueError("record page count is invalid")
    records = []
    for path, digest in zip(actual_pages, pages, strict=True):
        page = _json_object(path, "record page")
        if (
            set(page) != {"schema_version", "records", "digest"}
            or page.get("schema_version") != "crm-activities-page-v1"
        ):
            raise ValueError("record page schema is invalid")
        expected = page.get("digest")
        unsigned_page = dict(page)
        unsigned_page.pop("digest", None)
        if expected != digest or sha256_json(unsigned_page) != expected:
            raise ValueError("record page digest is invalid")
        values = page.get("records")
        if not isinstance(values, list):
            raise ValueError("record page is invalid")
        for raw in values:
            if not isinstance(raw, dict):
                raise ValueError("record page contains a non-object")
            record = record_from_mapping(cast(Mapping[str, object], raw))
            if raw != record.as_dict():
                raise ValueError("accepted record schema is not canonical")
            records.append(record)
    if len(records) != manifest.get("accepted_count") or len(identities) != len(records):
        raise ValueError("accepted/cleanup identity counts do not agree")
    accepted_ids = [item.source_record_pk for item in records]
    if accepted_ids != sorted(set(accepted_ids)):
        raise ValueError("accepted records are not globally sorted unique")
    if accepted_ids != keys:
        raise ValueError("cleanup identity set differs from accepted records")
    expected_cleanup = _cleanup_records(
        records,
        tuple(Disposition(item.source_record_pk, "accepted", None) for item in records),
    )
    if identities != expected_cleanup:
        raise ValueError("cleanup identities are not exactly the accepted cleanup set")
    rejected_object = _json_object(snapshot / "rejected.json", "rejected evidence")
    quarantined_object = _json_object(snapshot / "quarantined.json", "quarantined evidence")
    unresolved_object = _json_object(
        snapshot / "unresolved-references.json",
        "unresolved evidence",
    )
    rejected = _digest_rows(rejected_object, "rejected")
    quarantined = _digest_rows(quarantined_object, "quarantined")
    unresolved = _digest_rows(unresolved_object, "unresolved")
    if not isinstance(rejected, list) or not isinstance(quarantined, list):
        raise ValueError("non-accepted evidence is invalid")
    nonaccepted_ids = _outcome_ids(rejected) + _outcome_ids(quarantined)
    _validate_outcome_rows(rejected, boundary, "rejected")
    _validate_outcome_rows(quarantined, boundary, "quarantined")
    if len(set(accepted_ids) | set(nonaccepted_ids)) != len(accepted_ids) + len(nonaccepted_ids):
        raise ValueError("archive disposition evidence overlaps")
    selected = {item.source_record_pk for item in boundary.entries}
    if set(accepted_ids) | set(nonaccepted_ids) != selected:
        raise ValueError("disposition identities differ from sealed boundary")
    unresolved_ids = _unresolved_ids(unresolved)
    if not set(unresolved_ids).issubset(selected):
        raise ValueError("unresolved evidence contains an unselected identity")
    if len(accepted_ids) + len(rejected) + len(quarantined) != manifest.get("selected_count"):
        raise ValueError("archive disposition evidence has an unexplained remainder")
    expected = {
        Path("boundary.json"),
        Path("cleanup-identities.json"),
        Path("manifest.json"),
        Path("rejected.json"),
        Path("quarantined.json"),
        Path("unresolved-references.json"),
        Path("records"),
        *(Path("records") / path.name for path in actual_pages),
    }
    if paths != expected:
        raise ValueError("snapshot inventory has missing or extra evidence")
    return {
        "snapshot_id": manifest.get("snapshot_id"),
        "manifest_digest": original_digest,
        "verified": True,
    }


def snapshot_inventory(snapshot: Path) -> tuple[tuple[str, str, int], ...]:
    """Return strict relative file inventory after validating the domain snapshot."""
    verify_snapshot(snapshot)
    return tuple(
        (path.relative_to(snapshot).as_posix(), sha256_file(path), path.stat().st_size)
        for path in sorted(snapshot.rglob("*.json"))
    )


def _safe_directory(path: Path) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("snapshot directory is unsafe")


def _inventory_paths(snapshot: Path) -> set[Path]:
    paths: set[Path] = set()
    for path in snapshot.rglob("*"):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("snapshot inventory contains unsafe link evidence")
        if stat.S_ISDIR(metadata.st_mode):
            paths.add(path.relative_to(snapshot))
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("snapshot inventory contains unsafe file evidence")
        paths.add(path.relative_to(snapshot))
    return paths


def _json_object(path: Path, field: str) -> Mapping[str, object]:
    raw = path.read_bytes()
    value = _object(_read_json(path), field)
    if raw != canonical_json(dict(value)).encode("utf-8"):
        raise ValueError("snapshot JSON is not canonical")
    return value


def _digest_rows(value: Mapping[str, object], field: str) -> list[object]:
    if set(value) != {"records", "digest"}:
        raise ValueError(f"{field} evidence schema is invalid")
    rows = value.get("records")
    if not isinstance(rows, list) or value.get("digest") != sha256_json(rows):
        raise ValueError(f"{field} evidence digest is invalid")
    return rows


def _validate_manifest_shape(value: Mapping[str, object]) -> None:
    expected = {
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
    counts = (
        "selected_count",
        "accepted_count",
        "rejected_count",
        "quarantined_count",
        "unexplained_remainder",
        "cleanup_identity_count",
    )
    if (
        set(value) != expected
        or value.get("schema_version") != _MANIFEST_SCHEMA
        or value.get("provenance") != PROVENANCE
    ):
        raise ValueError("archive manifest schema is invalid")
    if not all(
        isinstance(value.get(key), int)
        and not isinstance(value.get(key), bool)
        and value.get(key) >= 0
        for key in counts
    ):
        raise ValueError("archive manifest counts are invalid")
    if value.get("unexplained_remainder") != 0:
        raise ValueError("archive manifest remainder is invalid")
    if not isinstance(value.get("snapshot_id"), str) or not isinstance(
        value.get("boundary_digest"), str
    ):
        raise ValueError("archive manifest identity is invalid")


def _validate_boundary_shape(value: Mapping[str, object]) -> None:
    if (
        set(value) != {"schema_version", "request", "entries", "digest"}
        or value.get("schema_version") != "crm-activities-boundary-v1"
    ):
        raise ValueError("boundary schema is invalid")
    request = value.get("request")
    entries = value.get("entries")
    if (
        not isinstance(request, dict)
        or set(request)
        != {
            "snapshot_id",
            "source_instance_id",
            "source_key",
            "page_size",
            "max_rows",
            "max_pages",
        }
        or not isinstance(entries, list)
    ):
        raise ValueError("boundary shape is invalid")
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "source_record_pk",
            "record_type",
            "record_digest",
            "reference_fingerprint",
        }:
            raise ValueError("boundary entry shape is invalid")


def _validate_cleanup_shape(value: Mapping[str, object]) -> None:
    if (
        set(value) != {"schema_version", "identities", "count", "digest"}
        or value.get("schema_version") != "crm-activities-cleanup-identities-v1"
    ):
        raise ValueError("cleanup schema is invalid")


def _validate_outcome_rows(rows: list[object], boundary: SealedBoundary, kind: str) -> None:
    entries = {item.source_record_pk: item for item in boundary.entries}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "source_record_pk",
            "record_type",
            "reason_code",
            "reference_fingerprint",
        }:
            raise ValueError("outcome row schema is invalid")
        identity = row.get("source_record_pk")
        entry = entries.get(identity) if isinstance(identity, str) else None
        if (
            entry is None
            or row.get("record_type") != entry.record_type
            or row.get("reference_fingerprint") != entry.reference_fingerprint
            or not isinstance(row.get("reason_code"), str)
        ):
            raise ValueError(f"{kind} outcome row is invalid")


def _outcome_ids(rows: list[object]) -> list[str]:
    ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("outcome evidence row is invalid")
        identity = row.get("source_record_pk")
        reason = row.get("reason_code")
        if not isinstance(identity, str) or not isinstance(reason, str):
            raise ValueError("outcome evidence row is invalid")
        ids.append(identity)
    if ids != sorted(set(ids)):
        raise ValueError("outcome evidence identities are not sorted unique")
    return ids


def _unresolved_ids(rows: list[object]) -> list[str]:
    identities: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"source_record_pk", "reasons"}:
            raise ValueError("unresolved evidence row is invalid")
        identity, reasons = row.get("source_record_pk"), row.get("reasons")
        if not isinstance(identity, str) or not isinstance(reasons, list):
            raise ValueError("unresolved evidence row is invalid")
        if not all(isinstance(reason, str) for reason in reasons) or reasons != sorted(
            set(reasons)
        ):
            raise ValueError("unresolved evidence reasons are invalid")
        identities.append(identity)
    if identities != sorted(set(identities)):
        raise ValueError("unresolved evidence identities are not sorted unique")
    return identities
