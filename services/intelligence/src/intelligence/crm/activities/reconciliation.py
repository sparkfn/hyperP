"""Boundary and partition reconciliation for fail-closed archive publication."""

from __future__ import annotations

from dataclasses import dataclass

from intelligence.crm.activities.models import (
    ArchiveRecord,
    ArchiveRequest,
    BoundaryEntry,
    Disposition,
    SealedBoundary,
)
from intelligence.repositories.protocols.crm_activities import CrmActivitiesRepository


@dataclass(frozen=True)
class CaptureResult:
    """Canonical selected records plus observed grouped duplicate deliveries."""

    records: tuple[ArchiveRecord, ...]
    duplicate_delivery_count: int


def capture(
    repository: CrmActivitiesRepository,
    request: ArchiveRequest,
    max_rows: int,
    max_pages: int,
) -> CaptureResult:
    """Enumerate bounded identity groups whose deliveries cannot cross page boundaries."""
    _assert_structurally_valid(repository, request)
    records: list[ArchiveRecord] = []
    duplicate_delivery_count = 0
    cursor = ""
    for _ in range(max_pages):
        page = repository.page(request, cursor)
        if not page.records:
            break
        if page.records[0].source_record_pk <= cursor or len(page.records) > request.page_size:
            raise RuntimeError("source keyset page violates its cursor contract")
        records.extend(page.records)
        duplicate_delivery_count += page.duplicate_delivery_count
        if len(records) > max_rows:
            raise RuntimeError("CRM activity boundary exceeds configured row ceiling")
        cursor = page.records[-1].source_record_pk
    else:
        raise RuntimeError("CRM activity boundary exceeds configured page ceiling")
    result = tuple(records)
    if len({item.source_record_pk for item in result}) != len(result):
        raise RuntimeError("source boundary contains duplicate identities across keyset pages")
    return CaptureResult(result, duplicate_delivery_count)


def seal(records: tuple[ArchiveRecord, ...], request: ArchiveRequest) -> SealedBoundary:
    return SealedBoundary(request, tuple(BoundaryEntry.from_record(item) for item in records))


def verify_boundary(repository: CrmActivitiesRepository, boundary: SealedBoundary) -> CaptureResult:
    """Re-enumerate grouped selection and compare complete canonical evidence."""
    _assert_structurally_valid(repository, boundary.request)
    current = capture(
        repository,
        boundary.request,
        boundary.request.max_rows,
        boundary.request.max_pages,
    )
    actual = tuple(BoundaryEntry.from_record(item) for item in current.records)
    if actual != boundary.entries:
        raise RuntimeError("CRM activities source boundary drift was detected")
    return current


def _assert_structurally_valid(
    repository: CrmActivitiesRepository, request: ArchiveRequest
) -> None:
    if repository.structural_invalid_count(request) != 0:
        raise RuntimeError("CRM activities boundary has structural-invalid candidate records")
    if repository.reference_fanout_invalid_count(request) != 0:
        raise RuntimeError("CRM activities boundary has reference-fanout candidate records")


def verify_page(
    repository: CrmActivitiesRepository,
    boundary: SealedBoundary,
    identities: tuple[str, ...],
) -> tuple[ArchiveRecord, ...]:
    """Reread one bounded identity page using the repository's grouped records."""
    rows = repository.by_identities(boundary.request, identities).records
    expected = tuple(item for item in boundary.entries if item.source_record_pk in set(identities))
    actual = tuple(BoundaryEntry.from_record(item) for item in rows)
    if tuple(item.source_record_pk for item in actual) != identities or actual != expected:
        raise RuntimeError("sealed page source evidence drifted or was incomplete")
    return rows


def counts(outcomes: tuple[Disposition, ...]) -> dict[str, int]:
    return {
        kind: sum(1 for item in outcomes if item.disposition == kind)
        for kind in ("accepted", "rejected", "quarantined")
    }
