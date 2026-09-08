"""Reviewed spawn-safe handlers for bounded CRM activity archive commands."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Literal

from intelligence.artifacts_staging import scan_staged_outputs
from intelligence.crm.activities import checkpoints
from intelligence.crm.activities.acceptance import (
    AcceptanceDescriptor,
    PublicationPointer,
    publication_candidate,
    publication_candidate_name,
    publication_descriptor,
    write_publication_descriptor,
)
from intelligence.crm.activities.bounded import ReadLimits
from intelligence.crm.activities.checkpoint_limits import CheckpointLimits
from intelligence.crm.activities.config import CrmActivitiesConfig
from intelligence.crm.activities.dispositions import classify
from intelligence.crm.activities.manifests import write_snapshot
from intelligence.crm.activities.model_parsing import parse_boundary, record_from_mapping
from intelligence.crm.activities.models import (
    ArchiveRecord,
    ArchiveRequest,
    Disposition,
    SealedBoundary,
    sha256_json,
)
from intelligence.crm.activities.reconciliation import capture, seal, verify_boundary, verify_page
from intelligence.crm.activities.verification import verify_accepted
from intelligence.models import OutputInventory
from intelligence.registry import Cancelled, RegisteredCommand, Registry
from intelligence.repositories.neo4j.crm_activities import Neo4jCrmActivitiesRepository
from intelligence.repositories.protocols.crm_activities import CrmActivitiesRepository

Operation = Literal["extract", "resume"]


def request_from_config(snapshot_id: str, config: CrmActivitiesConfig) -> ArchiveRequest:
    return ArchiveRequest(
        snapshot_id,
        config.source_instance_id,
        config.source_key,
        config.page_size,
        config.max_rows,
        config.max_pages,
        config.database_identity,
        "crm-activities-selection-v2",
        config.max_references_per_record,
    )


def registry(
    operation: Operation, request: ArchiveRequest, config: CrmActivitiesConfig
) -> Registry:
    """Return only one request-scoped reviewed handler; global production registry remains empty."""
    handler = partial(_archive_handler, operation, request, config)
    command = RegisteredCommand(
        f"crm_activities_{operation}",
        True,
        handler,
        {"domain": "crm_activities", "operation": operation, "snapshot_id": request.snapshot_id},
    )
    return Registry((command,))


def verification_registry(
    descriptor: AcceptanceDescriptor,
    pointer: PublicationPointer,
    trusted_accepted_outputs: tuple[OutputInventory, ...],
    checkpoint_limits: CheckpointLimits,
    input_limits: ReadLimits,
    output_maximum_bytes: int,
    output_maximum_entries: int,
) -> Registry:
    handler = partial(
        _verify_handler,
        descriptor,
        pointer,
        trusted_accepted_outputs,
        checkpoint_limits,
        input_limits,
        output_maximum_bytes,
        output_maximum_entries,
    )
    command = RegisteredCommand(
        "crm_activities_verify",
        True,
        handler,
        {
            "domain": "crm_activities",
            "operation": "verify",
            "snapshot_id": descriptor.snapshot_id,
        },
    )
    return Registry((command,))


def _archive_handler(
    operation: Operation,
    request: ArchiveRequest,
    config: CrmActivitiesConfig,
    run_staging: Path,
    cancelled: Cancelled,
) -> None:
    limits = CheckpointLimits(config.max_checkpoint_bytes, config.max_checkpoint_entries)
    checkpoint = checkpoints.checkpoint_root(run_staging, request.snapshot_id, limits)
    checkpoints.initialize(checkpoint, request, limits)
    capture_allowed = checkpoints.boundary_capture_allowed(checkpoint, limits)
    if operation == "resume" and capture_allowed:
        raise RuntimeError("CRM activities resume requires a durable sealed checkpoint")
    repository: CrmActivitiesRepository = Neo4jCrmActivitiesRepository(
        config.neo4j_uri,
        config.neo4j_user,
        config.neo4j_password,
        config.neo4j_database,
    )
    try:
        boundary = _boundary_or_capture(
            repository, checkpoint, request, limits, cancelled, capture_allowed
        )
        records = _validated_rows(repository, boundary, cancelled)
        outcomes = classify(records)
        outcome_values = [item.__dict__ for item in outcomes]
        checkpoints.write_evidence(
            checkpoint,
            "dispositions.json",
            {"outcomes": outcome_values, "digest": sha256_json(outcome_values)},
            limits,
        )
        cursor = checkpoints.resume_cursor(checkpoint, request, boundary, limits)
        _write_checkpoint_pages(checkpoint, boundary, records, outcomes, cursor, limits, cancelled)
        verify_boundary(repository, boundary)
        manifest = write_snapshot(run_staging, boundary, records, outcomes)
        checkpoints.write_evidence(checkpoint, "accepted-manifest.json", manifest, limits)
        inventory = scan_staged_outputs(
            run_staging.parent.parent,
            run_staging.name,
            config.max_checkpoint_bytes,
            config.max_checkpoint_entries,
        )
        snapshot_inventory = tuple(
            item
            for item in inventory
            if item.relative_path.startswith(f"snapshots/crm/activities/{manifest['snapshot_id']}/")
        )
        descriptor = publication_descriptor(
            request.snapshot_id,
            request,
            boundary.digest,
            run_staging.name,
            f"crm_activities_{operation}",
            str(manifest["snapshot_id"]),
            str(manifest["digest"]),
            str(manifest["cleanup_identity_digest"]),
            snapshot_inventory,
        )
        pointer = write_publication_descriptor(run_staging, descriptor)
        checkpoints.write_evidence(
            checkpoint,
            publication_candidate_name(run_staging.name),
            publication_candidate(pointer),
            limits,
        )
        checkpoints.complete(checkpoint, boundary.digest, _page_count(boundary), limits)
        checkpoints.bounded_usage(checkpoint, limits)
    finally:
        repository.close()


def _boundary_or_capture(
    repository: CrmActivitiesRepository,
    root: Path,
    request: ArchiveRequest,
    limits: CheckpointLimits,
    cancelled: Cancelled,
    capture_allowed: bool,
) -> SealedBoundary:
    if not capture_allowed:
        boundary = parse_boundary(checkpoints.load_boundary(root, limits))
        if boundary.request != request:
            raise RuntimeError("resume request conflicts with sealed boundary configuration")
        checkpoints.validate_resume(root, request, boundary, limits)
        _validate_checkpoint_records(root, boundary, limits)
        return boundary
    _cancelled(cancelled)
    records = capture(repository, request, request.max_rows, request.max_pages)
    for record in records:
        _cancelled(cancelled)
        checkpoints.write_record(root, record.source_record_pk, record.as_dict(), limits)
    boundary = seal(records, request)
    checkpoints.write_boundary(root, boundary, limits)
    verify_boundary(repository, boundary)
    return boundary


def _validate_checkpoint_records(
    root: Path, boundary: SealedBoundary, limits: CheckpointLimits
) -> None:
    """A resume refuses missing/corrupt sealed record evidence before source reads."""
    for entry in boundary.entries:
        record = record_from_mapping(checkpoints.read_record(root, entry.source_record_pk, limits))
        if (
            record.digest() != entry.record_digest
            or record.reference_fingerprint() != entry.reference_fingerprint
        ):
            raise RuntimeError("sealed checkpoint record evidence is corrupt")


def _validated_rows(
    repository: CrmActivitiesRepository,
    boundary: SealedBoundary,
    cancelled: Cancelled,
) -> tuple[ArchiveRecord, ...]:
    records: list[ArchiveRecord] = []
    identities = tuple(item.source_record_pk for item in boundary.entries)
    for index in range(0, len(identities), boundary.request.page_size):
        _cancelled(cancelled)
        page_ids = identities[index : index + boundary.request.page_size]
        records.extend(verify_page(repository, boundary, page_ids))
    result = tuple(records)
    if tuple(item.source_record_pk for item in result) != identities:
        raise RuntimeError("sealed page reconstruction lost an identity")
    return result


def _write_checkpoint_pages(
    root: Path,
    boundary: SealedBoundary,
    records: tuple[ArchiveRecord, ...],
    outcomes: tuple[Disposition, ...],
    cursor: int,
    limits: CheckpointLimits,
    cancelled: Cancelled,
) -> None:
    record_by_id = {item.source_record_pk: item for item in records}
    outcome_by_id = {item.source_record_pk: item for item in outcomes}
    for ordinal, offset in enumerate(
        range(
            cursor * boundary.request.page_size, len(boundary.entries), boundary.request.page_size
        ),
        start=cursor + 1,
    ):
        _cancelled(cancelled)
        entries = boundary.entries[offset : offset + boundary.request.page_size]
        identities = tuple(item.source_record_pk for item in entries)
        page: dict[str, object] = {
            "schema_version": "crm-activities-checkpoint-page-v1",
            "request_digest": sha256_json(boundary.request.as_public_dict()),
            "boundary_digest": boundary.digest,
            "identities": list(identities),
            "records": [record_by_id[item].as_dict() for item in identities],
            "dispositions": [outcome_by_id[item].__dict__ for item in identities],
        }
        page["digest"] = sha256_json(page)
        checkpoints.write_page(root, ordinal, page, limits)
        checkpoints.advance(root, boundary.digest, ordinal, limits)


def _page_count(boundary: SealedBoundary) -> int:
    count = len(boundary.entries)
    return (count + boundary.request.page_size - 1) // boundary.request.page_size


def _cancelled(cancelled: Cancelled) -> None:
    if cancelled():
        raise RuntimeError("CRM activities archive cancellation was requested")


def _verify_handler(
    descriptor: AcceptanceDescriptor,
    pointer: PublicationPointer,
    trusted_accepted_outputs: tuple[OutputInventory, ...],
    checkpoint_limits: CheckpointLimits,
    input_limits: ReadLimits,
    output_maximum_bytes: int,
    output_maximum_entries: int,
    run_staging: Path,
    cancelled: Cancelled,
) -> None:
    _cancelled(cancelled)
    verify_accepted(
        run_staging,
        descriptor,
        pointer,
        trusted_accepted_outputs,
        input_limits=input_limits,
        output_maximum_bytes=output_maximum_bytes,
        output_maximum_entries=output_maximum_entries,
        checkpoint_limits=checkpoint_limits,
    )
