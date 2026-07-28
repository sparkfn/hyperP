"""Person repository protocol — database-agnostic interface for person data."""

from __future__ import annotations

from typing import Protocol, TypedDict

from src.types import (
    AuditEvent,
    BankruptcyCase,
    ConnectionType,
    ListedPerson,
    MatchDecision,
    Person,
    PersonConnection,
    PersonEntitySummary,
    PersonGraph,
    PersonIdentifier,
    PersonListSummary,
    PersonSharedIdentifierCandidate,
    PersonTimelineGroup,
    PossibleMatchDetail,
    SourceRecord,
    SourceRecordEntityFacet,
)
from src.types_profile_analysis import (
    PersonProfileAnalyses,
    ProfileAnalysisHistoryItem,
    ProfileAnalysisRequestRequeueResult,
    ProfileAnalysisRequestResult,
    ProfileAnalysisType,
)


class PersonListFilters(TypedDict, total=False):
    q: str | None
    entity_keys: list[str] | None
    source_keys: list[str] | None
    source_record_type: str | None
    is_high_value: bool | None
    is_high_risk: bool | None
    has_phone: bool | None
    has_email: bool | None
    has_any_contact: bool | None
    has_address: bool | None
    has_bankruptcy_case: bool | None
    has_any_match: bool | None
    has_possible_match: bool | None
    has_system_match: bool | None
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
    dob_year: str | None
    dob_month: str | None
    dob_day: str | None
    entity_key_mode: str | None
    source_key_mode: str | None
    sort_by: str | None
    sort_order: str | None


class PersonRepository(Protocol):
    async def get_page(
        self, filters: PersonListFilters, skip: int, limit: int
    ) -> tuple[list[ListedPerson], int]:
        """Return a page of persons matching filters, plus the total count."""
        ...

    async def get_list_summary(self) -> PersonListSummary:
        """Return the aggregate counts shown above the person listing."""
        ...

    async def search_by_identifier(self, identifier_type: str, value: str) -> list[Person]: ...

    async def search_by_query(
        self, q: str, status: str | None, skip: int, limit: int
    ) -> tuple[list[Person], bool]:
        """Returns (items, has_more). No count query — has_more via +1 fetch."""
        ...

    async def get_by_id(self, person_id: str) -> Person | None: ...

    async def get_profile_analyses(self, person_id: str) -> PersonProfileAnalyses | None: ...

    async def request_profile_analysis(
        self,
        person_id: str,
        analysis_type: ProfileAnalysisType,
        force: bool,
    ) -> ProfileAnalysisRequestResult | None: ...

    async def mark_profile_analysis_request_dispatch_failed(self, request_id: str) -> None: ...

    async def requeue_failed_profile_analysis_request(
        self,
        person_id: str,
        request_id: str,
        max_attempts: int,
    ) -> ProfileAnalysisRequestRequeueResult | None: ...

    async def get_profile_analysis_history(
        self,
        person_id: str,
        analysis_type: ProfileAnalysisType | None,
        skip: int,
        limit: int,
    ) -> tuple[list[ProfileAnalysisHistoryItem], int] | None: ...

    async def get_source_records(
        self,
        person_id: str,
        skip: int,
        limit: int,
        entity_key: str | None = None,
        record_type: str | None = None,
    ) -> tuple[list[SourceRecord], int]: ...

    async def get_source_record_entity_facets(
        self, person_id: str
    ) -> list[SourceRecordEntityFacet]: ...

    async def get_bankruptcy_cases(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[BankruptcyCase], int]: ...

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

    async def get_shared_identifier_candidates(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[PersonSharedIdentifierCandidate], int]: ...

    async def get_possible_match_detail(
        self, person_id: str, candidate_person_id: str
    ) -> PossibleMatchDetail | None: ...

    async def get_entities(self, person_id: str) -> list[PersonEntitySummary]: ...

    async def get_graph(self, person_id: str, max_hops: int) -> PersonGraph | None: ...

    async def get_node_graph(self, element_id: str, max_hops: int) -> PersonGraph | None: ...

    async def get_audit(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[AuditEvent], int]: ...

    async def get_matches(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[MatchDecision], int]: ...

    async def get_timeline(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[PersonTimelineGroup], int]: ...

    async def get_timeline_target(
        self, person_id: str, source_record_pk: str
    ) -> PersonTimelineGroup | None: ...
