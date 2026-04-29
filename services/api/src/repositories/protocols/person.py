"""Person repository protocol — database-agnostic interface for person data."""

from __future__ import annotations

from typing import Protocol, TypedDict

from src.types import (
    AuditEvent,
    ConnectionType,
    ListedPerson,
    MatchDecision,
    Person,
    PersonConnection,
    PersonEntitySummary,
    PersonGraph,
    PersonIdentifier,
    SourceRecord,
)


class PersonListFilters(TypedDict, total=False):
    q: str | None
    entity_key: str | None
    is_high_value: bool | None
    is_high_risk: bool | None
    has_phone: bool | None
    has_email: bool | None
    has_address: bool | None
    addr_street: str | None
    addr_unit: str | None
    addr_city: str | None
    addr_postal: str | None
    addr_country: str | None
    updated_after: str | None
    updated_before: str | None
    has_dob: bool | None
    dob_from: str | None
    dob_to: str | None
    sort_by: str | None
    sort_order: str | None


class PersonRepository(Protocol):
    async def get_page(
        self, filters: PersonListFilters, skip: int, limit: int
    ) -> tuple[list[ListedPerson], int]:
        """Return a page of persons matching filters, plus the total count."""
        ...

    async def search_by_identifier(self, identifier_type: str, value: str) -> list[Person]: ...

    async def search_by_query(
        self, q: str, status: str | None, skip: int, limit: int
    ) -> tuple[list[Person], bool]:
        """Returns (items, has_more). No count query — has_more via +1 fetch."""
        ...

    async def get_by_id(self, person_id: str) -> Person | None: ...

    async def get_source_records(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[SourceRecord], int]: ...

    async def get_identifiers(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[PersonIdentifier], int]: ...

    async def get_connections(
        self,
        person_id: str,
        connection_type: ConnectionType,
        identifier_type: str | None,
        skip: int,
        limit: int,
    ) -> tuple[list[PersonConnection], int]: ...

    async def get_entities(self, person_id: str) -> list[PersonEntitySummary]: ...

    async def get_graph(self, person_id: str, max_hops: int) -> PersonGraph | None: ...

    async def get_node_graph(self, element_id: str, max_hops: int) -> PersonGraph | None: ...

    async def get_audit(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[AuditEvent], int]: ...

    async def get_matches(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[MatchDecision], bool]:
        """Returns (items, has_more). No count query — has_more via +1 fetch."""
        ...
