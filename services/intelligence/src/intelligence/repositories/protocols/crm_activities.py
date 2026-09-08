"""Read-only boundary for safe CRM activity projections."""

from __future__ import annotations

from typing import Protocol

from intelligence.crm.activities.models import ArchiveRecord, ArchiveRequest


class CrmActivitiesRepository(Protocol):
    """No graph mutation, schema initialisation, raw payload, or traversal escape hatch."""

    def page(
        self, request: ArchiveRequest, after_source_record_pk: str
    ) -> tuple[ArchiveRecord, ...]: ...

    def by_identities(
        self, request: ArchiveRequest, identities: tuple[str, ...]
    ) -> tuple[ArchiveRecord, ...]: ...

    def structural_invalid_count(self, request: ArchiveRequest) -> int: ...

    def close(self) -> None: ...
