"""Typed rows, payloads and state for the Neo4j -> Postgres read projection (#491).

The projection mirrors the API documents verbatim: ``list_payload`` is the API
``ListedPerson`` document and ``detail_payload`` is the API ``Person`` document
returned by ``GET /v1/persons/{id}``. Both are stored as ``jsonb`` and returned
verbatim by the Rust reader, so the field names and null handling follow the
API mappers exactly.

This module holds the types only; the mapping helpers that turn graph rows
into these documents live in :mod:`src.projection_sync_mapper`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict

from src.models import JsonValue


class AddressSummaryPayload(TypedDict):
    """The API ``AddressSummary`` document."""

    address_id: str
    unit_number: str | None
    street_number: str | None
    street_name: str | None
    city: str | None
    postal_code: str | None
    country_code: str | None
    normalized_full: str | None


class PersonEntitySummaryPayload(TypedDict):
    """The API ``PersonEntitySummary`` document."""

    entity_key: str
    display_name: str | None
    entity_type: str | None
    country_code: str | None
    is_active: bool
    source_record_count: int


class PersonPayload(TypedDict):
    """The API ``Person`` document as returned by ``GET /v1/persons/{id}``."""

    person_id: str
    status: str
    is_high_value: bool
    is_high_risk: bool
    preferred_full_name: str | None
    preferred_phone: str | None
    preferred_email: str | None
    preferred_dob: str | None
    preferred_address: AddressSummaryPayload | None
    preferred_nric: str | None
    preferred_race_ethnicity: str | None
    profile_completeness_score: float
    golden_profile_computed_at: str | None
    golden_profile_version: str | None
    source_record_count: int
    connection_count: int
    lifetime_value: float | None
    loyalty: None
    vehicles: None
    created_at: str
    updated_at: str


class ListedPersonPayload(TypedDict):
    """The API ``ListedPerson`` document as returned by ``GET /v1/persons``."""

    person_id: str
    status: str
    is_high_value: bool
    is_high_risk: bool
    preferred_full_name: str | None
    preferred_phone: str | None
    preferred_email: str | None
    preferred_dob: str | None
    preferred_address: AddressSummaryPayload | None
    preferred_nric: str | None
    preferred_race_ethnicity: str | None
    profile_completeness_score: float
    golden_profile_computed_at: str | None
    golden_profile_version: str | None
    source_record_count: int
    connection_count: int
    lifetime_value: None
    loyalty: None
    vehicles: None
    created_at: str
    updated_at: str
    phone_confidence: float | None
    entities: list[PersonEntitySummaryPayload]
    entity_count: int
    identifier_count: int
    possible_match_count: int
    system_match_count: int
    order_count: int
    crm_deal_count: int
    bankruptcy_case_count: int
    preferred_dob_display: str
    preferred_dob_invalid: bool


class IdentityLinkRevisionPayload(TypedDict):
    """The 15 public identity-link revision fields stored in ``revision``."""

    event_id: str
    global_revision: int
    source_system: str
    source_instance_id: str
    source_entity_type: str
    source_entity_id: str
    identity_policy_version: str
    link_status: str
    hyperp_person_id: str | None
    resolution_kind: str
    resolution_revision: int
    effective_at: str
    match_decision_id: str | None
    review_case_id: str | None
    supersedes_event_id: str | None


class SummaryCountersPayload(TypedDict):
    """The six person-list summary counters."""

    all_profiles_count: int
    high_risk_count: int
    high_value_count: int
    no_contact_count: int
    deals_this_month_count: int
    all_deals_count: int


@dataclass(frozen=True)
class PersonProjectionRow:
    """One projected person: typed columns plus the two API payload documents."""

    person_id: str
    canonical_id: str | None
    status: str
    preferred_full_name: str | None
    preferred_phone: str | None
    preferred_email: str | None
    preferred_dob: str | None
    preferred_nric: str | None
    preferred_address_id: str | None
    updated_at: datetime | None
    profile_completeness_score: float | None
    is_high_value: bool
    is_high_risk: bool
    source_record_count: int
    connection_count: int
    entity_count: int
    identifier_count: int
    possible_match_count: int
    system_match_count: int
    order_count: int
    crm_deal_count: int
    bankruptcy_case_count: int
    phone_confidence: float | None
    list_payload: ListedPersonPayload
    detail_payload: PersonPayload


@dataclass(frozen=True)
class PersonIdentifierRow:
    """One projected identifier of a person."""

    person_id: str
    identifier_type: str
    normalized_value: str
    is_active: bool
    is_verified: bool
    payload: dict[str, JsonValue]


@dataclass(frozen=True)
class PersonAddressRow:
    """One projected address of a person."""

    person_id: str
    address_id: str
    is_active: bool
    unit_number: str | None
    street_name: str | None
    city: str | None
    postal_code: str | None
    country_code: str | None
    payload: dict[str, JsonValue]


@dataclass(frozen=True)
class PersonConnectionRow:
    """One projected connection of a person."""

    person_id: str
    connected_person_id: str
    hops: int
    connection_type: str
    payload: dict[str, JsonValue]


@dataclass(frozen=True)
class SourceRecordRow:
    """One projected source record linked to a person."""

    source_record_pk: str
    person_id: str
    entity_key: str | None
    source_key: str | None
    record_type: str | None
    lifecycle_status: str
    is_active: bool
    observed_at: datetime | None
    payload: dict[str, JsonValue]


@dataclass(frozen=True)
class IdentityLinkRevisionRow:
    """One immutable identity-link revision with its public field subset."""

    event_id: str
    global_revision: int
    link_key: str
    revision: IdentityLinkRevisionPayload


@dataclass(frozen=True)
class SummaryCounters:
    """The six person-list summary counters mirrored into ``summary_counter``."""

    all_profiles_count: int
    high_risk_count: int
    high_value_count: int
    no_contact_count: int
    deals_this_month_count: int
    all_deals_count: int

    def as_json(self) -> dict[str, JsonValue]:
        """Return the counters as the JSON document stored in ``summary_counter``."""
        return {
            "all_profiles_count": self.all_profiles_count,
            "high_risk_count": self.high_risk_count,
            "high_value_count": self.high_value_count,
            "no_contact_count": self.no_contact_count,
            "deals_this_month_count": self.deals_this_month_count,
            "all_deals_count": self.all_deals_count,
        }


@dataclass(frozen=True)
class ProjectionState:
    """The durable projection state stored in the ``summary_counter`` row."""

    high_water: datetime | None
    last_success: datetime | None
    current_revision: int
    identity_stream_ready: bool


@dataclass(frozen=True)
class ProjectionRunResult:
    """Outcome of one bounded projection run."""

    persons_scanned: int
    persons_written: int
    identifiers_written: int
    addresses_written: int
    connections_written: int
    source_records_written: int
    revisions_appended: int
    persons_deleted: int
    high_water: datetime | None
    duration_seconds: float


@dataclass(frozen=True)
class ChildRows:
    """The child collections rebuilt for one batch of persons."""

    identifiers: tuple[PersonIdentifierRow, ...]
    addresses: tuple[PersonAddressRow, ...]
    connections: tuple[PersonConnectionRow, ...]
    source_records: tuple[SourceRecordRow, ...]
