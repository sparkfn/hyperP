"""Boundary and partition reconciliation for fail-closed archive publication."""

from __future__ import annotations

from intelligence.crm.activities.models import (
    ArchiveRecord,
    ArchiveRequest,
    BoundaryEntry,
    Disposition,
    SealedBoundary,
)
from intelligence.repositories.protocols.crm_activities import CrmActivitiesRepository


def capture(
    repository: CrmActivitiesRepository,
    request: ArchiveRequest,
    max_rows: int,
    max_pages: int,
) -> tuple[ArchiveRecord, ...]:
    """Keyset enumerate exactly once, enforcing finite no-duplicate source evidence."""
    _assert_structurally_valid(repository, request)
    records: list[ArchiveRecord] = []
    cursor = ""
    for _ in range(max_pages):
        page = repository.page(request, cursor)
        if not page:
            break
        if page[0].source_record_pk <= cursor or len(page) > request.page_size:
            raise RuntimeError("source keyset page violates its cursor contract")
        records.extend(page)
        if len(records) > max_rows:
            raise RuntimeError("CRM activity boundary exceeds configured row ceiling")
        cursor = page[-1].source_record_pk
    else:
        raise RuntimeError("CRM activity boundary exceeds configured page ceiling")
    result = tuple(records)
    if tuple(sorted(result, key=lambda item: item.source_record_pk)) != result:
        raise RuntimeError("source boundary is not ordered")
    if len({item.source_record_pk for item in result}) != len(result):
        raise RuntimeError("source boundary contains duplicate identities")
    return result


def seal(records: tuple[ArchiveRecord, ...], request: ArchiveRequest) -> SealedBoundary:
    return SealedBoundary(request, tuple(BoundaryEntry.from_record(item) for item in records))


def verify_boundary(
    repository: CrmActivitiesRepository, boundary: SealedBoundary
) -> tuple[ArchiveRecord, ...]:
    """Independently re-enumerate selection and compare complete identity/fingerprint sets."""
    _assert_structurally_valid(repository, boundary.request)
    current = capture(
        repository,
        boundary.request,
        boundary.request.max_rows,
        boundary.request.max_pages,
    )
    actual = tuple(BoundaryEntry.from_record(item) for item in current)
    if actual != boundary.entries:
        raise RuntimeError("CRM activities source boundary drift was detected")
    return current


def _assert_structurally_valid(
    repository: CrmActivitiesRepository, request: ArchiveRequest
) -> None:
    if repository.structural_invalid_count(request) != 0:
        raise RuntimeError("CRM activities boundary has structural-invalid candidate records")


def verify_page(
    repository: CrmActivitiesRepository,
    boundary: SealedBoundary,
    identities: tuple[str, ...],
) -> tuple[ArchiveRecord, ...]:
    rows = repository.by_identities(boundary.request, identities)
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
