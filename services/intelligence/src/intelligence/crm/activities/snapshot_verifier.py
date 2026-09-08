"""Strict canonical snapshot verification and inventory helpers."""

from __future__ import annotations

import stat
from collections.abc import Mapping
from pathlib import Path

from intelligence.artifacts import canonical_json, sha256_file
from intelligence.crm.activities.dispositions import classify
from intelligence.crm.activities.manifests import (
    _cleanup_records,
    _object,
    _read_json,
    _unresolved_rows,
)
from intelligence.crm.activities.model_parsing import parse_boundary, record_from_mapping
from intelligence.crm.activities.models import (
    PROVENANCE,
    ArchiveRecord,
    BoundaryEntry,
    Disposition,
    DispositionKind,
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
    keys: list[str] = []
    for item in identities:
        if not isinstance(item, dict):
            raise ValueError("cleanup identity set is invalid")
        identity = item.get("source_record_pk")
        if not isinstance(identity, str):
            raise ValueError("cleanup identity set is invalid")
        keys.append(identity)
    if len(keys) != len(identities) or keys != sorted(set(keys)):
        raise ValueError("cleanup identity set is not sorted unique")
    pages = manifest.get("record_page_digests")
    if not isinstance(pages, list) or not all(isinstance(item, str) for item in pages):
        raise ValueError("record page inventory is invalid")
    actual_pages = tuple(sorted((snapshot / "records").glob("page-*.json")))
    if len(actual_pages) != len(pages):
        raise ValueError("record page count is invalid")
    accepted_records: list[ArchiveRecord] = []
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
            record_value = _object(raw, "record page record")
            record = record_from_mapping(record_value)
            if dict(record_value) != record.as_dict():
                raise ValueError("accepted record schema is not canonical")
            accepted_records.append(record)
    if len(accepted_records) != manifest.get("accepted_count") or len(identities) != len(
        accepted_records
    ):
        raise ValueError("accepted/cleanup identity counts do not agree")
    accepted_ids = [item.source_record_pk for item in accepted_records]
    if accepted_ids != sorted(set(accepted_ids)):
        raise ValueError("accepted records are not globally sorted unique")
    if accepted_ids != keys:
        raise ValueError("cleanup identity set differs from accepted records")
    sealed_entries = {entry.source_record_pk: entry for entry in boundary.entries}
    _verify_records_against_boundary(accepted_records, sealed_entries, "accepted")
    accepted_outcomes = tuple(
        Disposition(record.source_record_pk, "accepted", None) for record in accepted_records
    )
    expected_cleanup = _cleanup_records(accepted_records, accepted_outcomes)
    if identities != expected_cleanup:
        raise ValueError("cleanup identities are not exactly the accepted cleanup set")
    rejected_object = _json_object(snapshot / "rejected.json", "rejected evidence")
    quarantined_object = _json_object(snapshot / "quarantined.json", "quarantined evidence")
    unresolved_object = _json_object(
        snapshot / "unresolved-references.json",
        "unresolved evidence",
    )
    rejected_rows = _digest_rows(rejected_object, "rejected")
    quarantined_rows = _digest_rows(quarantined_object, "quarantined")
    unresolved = _digest_rows(unresolved_object, "unresolved")
    rejected_records, rejected_outcomes = _outcomes_from_rows(
        rejected_rows,
        sealed_entries,
        "rejected",
    )
    quarantined_records, quarantined_outcomes = _outcomes_from_rows(
        quarantined_rows,
        sealed_entries,
        "quarantined",
    )
    all_records = tuple(
        sorted(
            accepted_records + rejected_records + quarantined_records,
            key=lambda record: record.source_record_pk,
        )
    )
    actual_outcomes = tuple(
        sorted(
            accepted_outcomes + rejected_outcomes + quarantined_outcomes,
            key=lambda outcome: outcome.source_record_pk,
        )
    )
    expected_outcomes = classify(all_records)
    if actual_outcomes != expected_outcomes:
        raise ValueError("snapshot dispositions do not match closed classification")
    if len({record.source_record_pk for record in all_records}) != len(all_records):
        raise ValueError("archive disposition evidence overlaps")
    selected = {item.source_record_pk for item in boundary.entries}
    if {record.source_record_pk for record in all_records} != selected:
        raise ValueError("disposition identities differ from sealed boundary")
    if len(all_records) != manifest.get("selected_count"):
        raise ValueError("archive disposition evidence has an unexplained remainder")
    if (
        manifest.get("selected_count") != len(boundary.entries)
        or manifest.get("accepted_count") != len(accepted_records)
        or manifest.get("rejected_count") != len(rejected_records)
        or manifest.get("quarantined_count") != len(quarantined_records)
        or manifest.get("cleanup_identity_count") != len(identities)
    ):
        raise ValueError("archive manifest counts disagree with evidence")
    records_by_id = {record.source_record_pk: record for record in all_records}
    if unresolved != _unresolved_rows(records_by_id, expected_outcomes):
        raise ValueError("unresolved evidence does not match archive records")
    unresolved_ids = _unresolved_ids(unresolved)
    if not set(unresolved_ids).issubset(selected):
        raise ValueError("unresolved evidence contains an unselected identity")
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


def _verify_records_against_boundary(
    records: list[ArchiveRecord],
    entries: Mapping[str, BoundaryEntry],
    field: str,
) -> None:
    for record in records:
        if BoundaryEntry.from_record(record) != entries.get(record.source_record_pk):
            raise ValueError(f"{field} record differs from its sealed boundary entry")


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
    try:
        raw = path.read_bytes()
        value = _object(_read_json(path), field)
    except FileNotFoundError as error:
        raise ValueError(f"{field} is missing") from error
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
    for key in counts:
        count = value.get(key)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
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
            "database_identity",
            "selection_contract_version",
            "max_references_per_record",
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


def _outcomes_from_rows(
    rows: list[object],
    entries: Mapping[str, BoundaryEntry],
    disposition: DispositionKind,
) -> tuple[list[ArchiveRecord], tuple[Disposition, ...]]:
    if disposition == "accepted":
        raise ValueError("accepted outcomes must be stored in record pages")
    records: list[ArchiveRecord] = []
    outcomes: list[Disposition] = []
    identities: list[str] = []
    for raw in rows:
        row = _object(raw, "outcome evidence row")
        if set(row) != {
            "source_record_pk",
            "record_type",
            "reason_code",
            "reference_fingerprint",
            "record",
        }:
            raise ValueError("outcome row schema is invalid")
        identity = row.get("source_record_pk")
        reason_code = row.get("reason_code")
        if not isinstance(identity, str) or not isinstance(reason_code, str):
            raise ValueError("outcome evidence row is invalid")
        record_value = _object(row.get("record"), "outcome record")
        record = record_from_mapping(record_value)
        if dict(record_value) != record.as_dict():
            raise ValueError("outcome record schema is not canonical")
        entry = entries.get(identity)
        if (
            identity != record.source_record_pk
            or entry is None
            or row.get("record_type") != record.record_type
            or row.get("reference_fingerprint") != record.reference_fingerprint()
            or BoundaryEntry.from_record(record) != entry
        ):
            raise ValueError("outcome row conflicts with sealed boundary")
        records.append(record)
        outcomes.append(Disposition(identity, disposition, reason_code))
        identities.append(identity)
    if identities != sorted(set(identities)):
        raise ValueError("outcome evidence identities are not sorted unique")
    return records, tuple(outcomes)


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
