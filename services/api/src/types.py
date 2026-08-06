"""Enums and Pydantic models for API request/response payloads."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field
from pydantic.types import JsonValue

# Provenance class of a SourceRecord. The first three ("system family") replaced
# the former single "system" value; they share matching behaviour today but are
# distinct so they can diverge later. Mirrors ingestion's ``RecordType`` enum.
SourceRecordTypeLiteral = Literal[
    "identity",
    "bankruptcy",
    "rental_flat",
    "relationship",
    "conversation",
    "sales",
    "crm_deal",
    "crm_history",
    "call",
]
SourceRecordLifecycleStatus = Literal[
    "active", "pending_review", "superseded", "rejected", "link_failed"
]

# --- Enums ---


class PersonStatus(StrEnum):
    ACTIVE = "active"
    MERGED = "merged"
    SUPPRESSED = "suppressed"


class QualityFlag(StrEnum):
    VALID = "valid"
    INVALID_FORMAT = "invalid_format"
    PLACEHOLDER_VALUE = "placeholder_value"
    SHARED_SUSPECTED = "shared_suspected"
    STALE = "stale"
    SOURCE_UNTRUSTED = "source_untrusted"
    PARTIAL_PARSE = "partial_parse"


class IdentifierType(StrEnum):
    PHONE = "phone"
    EMAIL = "email"
    NRIC = "nric"
    EXTERNAL_CUSTOMER_ID = "external_customer_id"
    MEMBERSHIP_ID = "membership_id"
    CRM_CONTACT_ID = "crm_contact_id"
    LOYALTY_ID = "loyalty_id"
    CUSTOM = "custom"


class EngineType(StrEnum):
    DETERMINISTIC = "deterministic"
    HEURISTIC = "heuristic"
    LLM = "llm"
    MANUAL = "manual"


class MatchDecisionOutcome(StrEnum):
    MERGE = "merge"
    REVIEW = "review"
    NO_MATCH = "no_match"


class QueueState(StrEnum):
    OPEN = "open"
    ASSIGNED = "assigned"
    DEFERRED = "deferred"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


class LinkStatus(StrEnum):
    LINKED = "linked"
    PENDING_REVIEW = "pending_review"
    REJECTED = "rejected"
    SUPPRESSED = "suppressed"


class ApiReviewActionType(StrEnum):
    """Action types submittable via the review actions API endpoint."""

    MERGE = "merge"
    REJECT = "reject"
    DEFER = "defer"
    ESCALATE = "escalate"
    MANUAL_NO_MATCH = "manual_no_match"


class ConnectionType(StrEnum):
    IDENTIFIER = "identifier"
    ADDRESS = "address"
    KNOWS = "knows"
    ALL = "all"


class TrustTier(StrEnum):
    TIER_1 = "tier_1"
    TIER_2 = "tier_2"
    TIER_3 = "tier_3"
    TIER_4 = "tier_4"


# --- Response envelope ---


class ResponseMeta(BaseModel):
    request_id: str
    next_cursor: str | None = None
    total_count: int | None = None


class PopoverDisplayItem(BaseModel):
    primary: str
    secondary: str = ""


class ApiResponse[DataT](BaseModel):
    data: DataT
    meta: ResponseMeta
    display_items: list[PopoverDisplayItem] | None = None


class ApiErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, str] | None = None


class ApiError(BaseModel):
    error: ApiErrorBody
    meta: ResponseMeta


# --- Domain models ---


class AddressSummary(BaseModel):
    address_id: str
    unit_number: str | None = None
    street_number: str | None = None
    street_name: str | None = None
    city: str | None = None
    postal_code: str | None = None
    country_code: str | None = None
    normalized_full: str | None = None


class LoyaltySummary(BaseModel):
    """Per-source-system loyalty-points balance, read through from identity SourceRecords."""

    source_system: str
    points: int | None = None
    disable_loyalty: bool | None = None
    current_spend_for_points: float | None = None
    current_sales_for_discount: float | None = None
    observed_at: str | None = None


class VehicleSummary(BaseModel):
    """A Vehicle linked to a person via OWNS_VEHICLE / BOUGHT_VEHICLE."""

    vehicle_id: str
    product: str | None = None
    product_sku: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    lta_tag: str | None = None
    serial_number: str | None = None
    relationship: Literal["OWNS", "BOUGHT"]
    is_active: bool | None = None
    conflict_flag: bool | None = None
    observed_at: str | None = None


class Person(BaseModel):
    person_id: str
    status: PersonStatus
    is_high_value: bool = False
    is_high_risk: bool = False
    preferred_full_name: str | None = None
    preferred_phone: str | None = None
    preferred_email: str | None = None
    preferred_dob: str | None = None
    preferred_address: AddressSummary | None = None
    preferred_nric: str | None = None
    preferred_race_ethnicity: str | None = None
    profile_completeness_score: float = 0.0
    golden_profile_computed_at: str | None = None
    golden_profile_version: str | None = None
    source_record_count: int = 0
    connection_count: int = 0
    lifetime_value: float | None = None
    loyalty: list[LoyaltySummary] | None = None
    vehicles: list[VehicleSummary] | None = None
    created_at: str = ""
    updated_at: str = ""


class PersonIdentifier(BaseModel):
    identifier_type: str
    normalized_value: str
    is_active: bool = True
    is_verified: bool = False
    last_confirmed_at: str | None = None
    source_system_key: str | None = None
    source_record_pks: list[str] = Field(default_factory=list)
    source_record_ids: list[str] = Field(default_factory=list)
    entities: list[PersonEntitySummary] = Field(default_factory=list)
    source_records: list[SourceRecord] = Field(default_factory=list)


class PersonSharedIdentifierCandidate(BaseModel):
    person_id: str
    status: str
    preferred_full_name: str | None = None
    preferred_phone: str | None = None
    preferred_email: str | None = None
    preferred_dob: str | None = None
    profile_completeness_score: float = 0.0
    identifier_strength: Literal["strong", "weak"]
    identifiers: list[SharedIdentifier] = Field(default_factory=list)


class SourceRecord(BaseModel):
    source_record_pk: str
    source_system: str
    source_record_id: str
    source_record_version: str | None = None
    entity_key: str | None = None
    entity_display_name: str | None = None
    record_type: SourceRecordTypeLiteral = "identity"
    lifecycle_status: SourceRecordLifecycleStatus = "active"
    extraction_confidence: float | None = None
    extraction_method: str | None = None
    link_status: str
    linked_person_id: str | None = None
    parent_source_system: str | None = None
    parent_source_record_id: str | None = None
    parent_record_type: SourceRecordTypeLiteral | None = None
    # Optional during the migration from untyped generic CRM activities.
    history_family: Literal["activity", "stage"] | None = None
    history_kind: str | None = None
    history_source: str | None = None
    event_category_id: str | None = None
    event_stage_id: str | None = None
    event_stage_semantic_id: str | None = None
    event_at: str | None = None
    history_projection_version: str | None = None
    history_projection_source: str | None = None
    history_projected_at: str | None = None
    observed_at: str
    ingested_at: str
    conversation_ref: dict[str, JsonValue] | None = None
    raw_payload: dict[str, JsonValue] | None = None
    normalized_payload: dict[str, JsonValue] | None = None


class ChatMessage(BaseModel):
    """One parsed message from a conversation source record's transcript."""

    timestamp: str
    timestamp_display: str
    speaker: str
    phone: str | None = None
    role: str | None = None
    text: str


class SourceRecordView(SourceRecord):
    """v2 presentation model: SourceRecord plus API-formatted display strings.

    Kept separate from SourceRecord so the public person-page contract (which
    returns SourceRecord) is unaffected.
    """

    observed_at_display: str
    ingested_at_display: str
    extraction_confidence_display: str | None = None
    chat_transcript: list[ChatMessage] | None = None


class SourceRecordEntityFacet(BaseModel):
    """Per-entity source-record count for a person, for filter chips."""

    source_system: str
    entity_key: str | None = None
    entity_display_name: str | None = None
    count: int


GoldenFieldName = Literal[
    "preferred_full_name",
    "preferred_dob",
    "preferred_phone",
    "preferred_email",
    "preferred_nric",
    "preferred_address",
    "preferred_race_ethnicity",
]

GoldenSourceKind = Literal["source_record_fact", "identifier", "address"]


class FieldOption(BaseModel):
    """One candidate value for an editable golden-profile field.

    Computed server-side from the appropriate graph relationship (HAS_FACT,
    IDENTIFIED_BY, or LIVES_AT) so the frontend never has to map field names
    onto the source record's normalized payload itself.
    """

    source_record_pk: str
    source_kind: GoldenSourceKind
    identifier_type: str | None = None
    value: str
    value_display: str
    source_system: str
    entity_display_name: str | None = None
    observed_at_display: str | None = None
    is_current: bool = False


class EditableFieldOptions(BaseModel):
    """All candidate values for one editable golden-profile field."""

    field_name: GoldenFieldName
    label: str
    source_kind: GoldenSourceKind
    current_value_display: str | None = None
    is_overridden: bool = False
    options: list[FieldOption] = Field(default_factory=list)


class PersonFieldOptions(BaseModel):
    """Editable golden-profile fields and their selectable source values."""

    person_id: str
    fields: list[EditableFieldOptions] = Field(default_factory=list)


class SharedIdentifierGroup(BaseModel):
    identifier_type: str
    normalized_value: str
    candidate_source_records: list[SourceRecord]
    current_person_source_records: list[SourceRecord]


class PossibleMatchDetail(BaseModel):
    candidate_person_id: str
    candidate_name: str | None = None
    shared_identifier_groups: list[SharedIdentifierGroup]


class BankruptcyCase(BaseModel):
    bankruptcy_case_id: str
    source_system_key: str
    source_case_id: str
    case_number: str | None = None
    document_type: str | None = None
    document_date: str | None = None
    event_type: str | None = None
    event_date: str | None = None
    trustee_name: str | None = None
    trustee_firm: str | None = None
    source_url: str | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


TimelineTimestampKind = Literal["source", "fallback"]
TimelineFactCategory = Literal[
    "identity",
    "contact",
    "address",
    "sale",
    "relationship",
    "conversation",
    "source",
    "bankruptcy",
]


class TimelineFact(BaseModel):
    fact_id: str
    category: TimelineFactCategory
    label: str
    value: str
    detail: str | None = None


class PersonTimelineGroup(BaseModel):
    source_record_pk: str
    source_system: str
    source_record_id: str
    source_record_version: str | None = None
    record_type: SourceRecordTypeLiteral = "identity"
    extraction_confidence: float | None = None
    link_status: str
    linked_person_id: str | None = None
    occurred_at: str
    timestamp_kind: TimelineTimestampKind
    ingested_at: str
    facts: list[TimelineFact] = Field(default_factory=list)


class MatchDecisionSummary(BaseModel):
    match_decision_id: str
    engine_type: str
    decision: str
    confidence: float


class MatchDecision(BaseModel):
    match_decision_id: str
    engine_type: str
    engine_version: str
    policy_version: str
    decision: str
    confidence: float
    reasons: list[str] = Field(default_factory=list)
    blocking_conflicts: list[str] = Field(default_factory=list)
    created_at: str = ""
    left_person_id: str | None = None
    right_person_id: str | None = None
    review_case_id: str | None = None
    review_case_queue_state: str | None = None
    review_case_assigned_to: str | None = None


class ReviewCaseSummary(BaseModel):
    review_case_id: str
    queue_state: str
    priority: int
    assigned_to: str | None = None
    follow_up_at: str | None = None
    sla_due_at: str | None = None
    resolution: str | None = None
    resolved_at: str | None = None
    left_person_id: str | None = None
    left_person_name: str | None = None
    left_person_status: str | None = None
    right_person_id: str | None = None
    right_person_name: str | None = None
    right_person_status: str | None = None
    match_decision: MatchDecisionSummary


class SalesVehicleSummary(BaseModel):
    vehicle_id: str
    product: str | None = None
    product_sku: str | None = None
    normalized_lta_tag: str | None = None
    normalized_serial_number: str | None = None
    conflict_flag: bool = False


class NonVehicleLine(BaseModel):
    """One non-vehicle line item denormalized onto an Order."""

    product_sku: str | None = None
    product: str | None = None
    merchant: str | None = None
    manufacturer: str | None = None
    serial_number: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    total_amount: float | None = None
    currency: str | None = None
    category: str | None = None
    raw: dict[str, JsonValue] = Field(default_factory=dict)


class SalesOrderSummary(BaseModel):
    order_id: str
    order_no: str | None = None
    total_amount: float | None = None
    currency: str | None = None
    ordered_at: str | None = None
    vehicles: list[SalesVehicleSummary] = Field(default_factory=list)
    non_vehicle_lines: list[NonVehicleLine] = Field(default_factory=list)


class PersonComparisonEntity(BaseModel):
    entity_kind: Literal["person", "source_record"] = "person"
    person_id: str | None = None
    source_record_pk: str | None = None
    source_record_id: str | None = None
    source_system_key: str | None = None
    observed_at: str | None = None
    record_type: str | None = None
    linked_person_id: str | None = None
    status: str | None = None
    preferred_full_name: str | None = None
    preferred_phone: str | None = None
    preferred_email: str | None = None
    preferred_dob: str | None = None
    preferred_address: AddressSummary | None = None
    sales_summary: SalesOrderSummary | None = None


class ReviewCaseDetail(BaseModel):
    review_case_id: str
    queue_state: str
    priority: int
    assigned_to: str | None = None
    follow_up_at: str | None = None
    sla_due_at: str | None = None
    resolution: str | None = None
    resolved_at: str | None = None
    actions: list[dict[str, str | None]] = Field(default_factory=list)
    match_decision: MatchDecision
    comparison_left: PersonComparisonEntity | None = None
    comparison_right: PersonComparisonEntity | None = None
    shared_identifier_groups: list[SharedIdentifierGroup] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


class SharedIdentifier(BaseModel):
    identifier_type: str
    normalized_value: str


class SharedAddress(BaseModel):
    address_id: str
    normalized_full: str | None = None
    source_system_key: str | None = None


class KnowsRelationship(BaseModel):
    relationship_label: str | None = None
    relationship_category: str
    source_system_key: str | None = None


class ConnectionSource(BaseModel):
    """Source system + entity that established a particular connection."""

    source_system_key: str
    entity_display_name: str | None = None


class PersonConnection(BaseModel):
    person_id: str
    status: str
    preferred_full_name: str | None = None
    hops: int
    shared_identifiers: list[SharedIdentifier] = Field(default_factory=list)
    shared_addresses: list[SharedAddress] = Field(default_factory=list)
    knows_relationships: list[KnowsRelationship] = Field(default_factory=list)
    connection_sources: list[ConnectionSource] = Field(default_factory=list)


class AuditEvent(BaseModel):
    merge_event_id: str
    event_type: str
    actor_type: str
    actor_id: str
    reason: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    created_at: str = ""
    absorbed_person_id: str | None = None
    survivor_person_id: str | None = None
    triggered_by_decision_id: str | None = None


class GraphNode(BaseModel):
    id: str
    label: str
    properties: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    type: str
    properties: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class PersonGraph(BaseModel):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


class DownstreamEvent(BaseModel):
    event_id: str
    event_type: str
    affected_person_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    created_at: str


class EntitySummary(BaseModel):
    entity_key: str
    display_name: str | None = None
    entity_type: str | None = None
    country_code: str | None = None
    is_active: bool = True
    person_count: int = 0
    source_record_count: int = 0
    last_ingested_at: str | None = None
    active_review_cases: int = 0


class SourceSystemSummary(BaseModel):
    source_key: str
    display_name: str | None = None
    system_type: str | None = None
    is_active: bool = True
    source_record_count: int = 0
    last_ingested_at: str | None = None


class PersonEntitySummary(BaseModel):
    """Entity a person is linked to, with that person's source-record count."""

    entity_key: str
    display_name: str | None = None
    entity_type: str | None = None
    country_code: str | None = None
    is_active: bool = True
    source_record_count: int = 0


class PersonListSummary(BaseModel):
    """Counts displayed above the authenticated person listing."""

    all_profiles_count: int = 0
    high_risk_count: int = 0
    high_value_count: int = 0
    no_contact_count: int = 0


class EntityPerson(Person):
    phone_confidence: float | None = None


class ListedPerson(EntityPerson):
    """Person row in the /v1/persons listing, with inline entity memberships."""

    entities: list[PersonEntitySummary] = Field(default_factory=list)
    entity_count: int = 0
    identifier_count: int = 0
    possible_match_count: int = 0
    system_match_count: int = 0
    order_count: int = 0
    bankruptcy_case_count: int = 0
    # Server-computed DOB presentation: ready-to-render string + validity flag,
    # so the frontend does no date parsing/formatting. See format_display_dob.
    preferred_dob_display: str = "—"
    preferred_dob_invalid: bool = False
