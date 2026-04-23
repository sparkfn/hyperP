"""Enums and Pydantic models for API request/response payloads."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

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

class ApiResponse[DataT](BaseModel):
    data: DataT
    meta: ResponseMeta

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
    profile_completeness_score: float = 0.0
    golden_profile_computed_at: str | None = None
    golden_profile_version: str | None = None
    source_record_count: int = 0
    connection_count: int = 0
    created_at: str = ""
    updated_at: str = ""


class PersonIdentifier(BaseModel):
    identifier_type: str
    normalized_value: str
    is_active: bool = True
    is_verified: bool = False
    last_confirmed_at: str | None = None
    source_system_key: str | None = None


class SourceRecord(BaseModel):
    source_record_pk: str
    source_system: str
    source_record_id: str
    source_record_version: str | None = None
    record_type: Literal["system", "conversation"] = "system"
    extraction_confidence: float | None = None
    link_status: str
    linked_person_id: str | None = None
    observed_at: str
    ingested_at: str


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


class ReviewCaseSummary(BaseModel):
    review_case_id: str
    queue_state: str
    priority: int
    assigned_to: str | None = None
    follow_up_at: str | None = None
    sla_due_at: str | None = None
    match_decision: MatchDecisionSummary


class PersonComparisonEntity(BaseModel):
    entity_kind: Literal["person", "source_record"] = "person"
    person_id: str | None = None
    source_record_pk: str | None = None
    source_record_id: str | None = None
    status: str | None = None
    preferred_full_name: str | None = None
    preferred_phone: str | None = None
    preferred_email: str | None = None
    preferred_dob: str | None = None
    preferred_address: AddressSummary | None = None


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
    created_at: str = ""
    updated_at: str = ""


class SharedIdentifier(BaseModel):
    identifier_type: str
    normalized_value: str


class SharedAddress(BaseModel):
    address_id: str
    normalized_full: str | None = None


class KnowsRelationship(BaseModel):
    relationship_label: str | None = None
    relationship_category: str


class PersonConnection(BaseModel):
    person_id: str
    status: str
    preferred_full_name: str | None = None
    hops: int
    shared_identifiers: list[SharedIdentifier] = Field(default_factory=list)
    shared_addresses: list[SharedAddress] = Field(default_factory=list)
    knows_relationships: list[KnowsRelationship] = Field(default_factory=list)


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


class PersonEntitySummary(BaseModel):
    """Entity a person is linked to, with that person's source-record count."""
    entity_key: str
    display_name: str | None = None
    entity_type: str | None = None
    country_code: str | None = None
    is_active: bool = True
    source_record_count: int = 0


class EntityPerson(Person):
    phone_confidence: float | None = None


class ListedPerson(EntityPerson):
    """Person row in the /v1/persons listing, with inline entity memberships."""
    entities: list[PersonEntitySummary] = Field(default_factory=list)
    entity_count: int = 0
    identifier_count: int = 0


class SalesProduct(BaseModel):
    display_name: str | None = None
    sku: str | None = None

class SalesLineItem(BaseModel):
    line_no: int | None = None
    quantity: float | None = None
    unit_price: float | None = None
    subtotal: float | None = None
    product: SalesProduct | None = None

class SalesOrder(BaseModel):
    order_no: str | None = None
    source_order_id: str | None = None
    order_date: str | None = None
    total_amount: float | None = None
    currency: str | None = None
    source_system: str | None = None
    entity_name: str | None = None
    line_items: list[SalesLineItem] = Field(default_factory=list)

# --- Request bodies ---

class AssignReviewRequest(BaseModel):
    assigned_to: str

class ReviewActionMetadata(BaseModel):
    create_manual_lock: bool = False
    follow_up_at: str | None = None
    escalation_reason: str | None = None
    survivor_person_id: str | None = None

class ReviewActionRequest(BaseModel):
    action_type: ApiReviewActionType
    notes: str | None = None
    metadata: ReviewActionMetadata = Field(default_factory=ReviewActionMetadata)

class ManualMergeRequest(BaseModel):
    from_person_id: str
    to_person_id: str
    reason: str
    recompute_golden_profile: bool = True

class UnmergeRequest(BaseModel):
    merge_event_id: str
    reason: str

class LockRequest(BaseModel):
    left_person_id: str
    right_person_id: str
    lock_type: str
    reason: str
    expires_at: str | None = None

class SurvivorshipOverrideRequest(BaseModel):
    attribute_name: str
    selected_source_record_pk: str
    reason: str

class IngestIdentifier(BaseModel):
    type: str
    value: str
    is_verified: bool = False

class IngestRecord(BaseModel):
    source_record_id: str
    source_record_version: str | None = None
    record_type: Literal["system", "conversation"] = "system"
    extraction_confidence: float | None = None
    extraction_method: str | None = None
    conversation_ref: dict[str, str | int | float | bool | None] | None = None
    observed_at: str
    record_hash: str
    identifiers: list[IngestIdentifier] = Field(default_factory=list)
    attributes: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    raw_payload: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_record_type_invariants(self) -> IngestRecord:
        """Validate conversation vs system record field constraints."""
        if self.record_type == "conversation":
            if self.extraction_confidence is None or self.extraction_method is None:
                raise ValueError(
                    "conversation source records require extraction_confidence "
                    "and extraction_method"
                )
            if not 0.0 <= self.extraction_confidence <= 1.0:
                raise ValueError(
                    f"extraction_confidence must be in [0.0, 1.0], "
                    f"got {self.extraction_confidence}"
                )
        else:
            if (
                self.extraction_confidence is not None
                or self.extraction_method is not None
                or self.conversation_ref is not None
            ):
                raise ValueError(
                    "extraction_confidence / extraction_method / conversation_ref "
                    "are only valid on record_type='conversation'"
                )
        return self

class IngestRecordsRequest(BaseModel):
    ingest_type: str
    ingest_run_id: str | None = None
    records: list[IngestRecord]

class IngestRunCreateRequest(BaseModel):
    run_type: str
    metadata: dict[str, str] = Field(default_factory=dict)

class IngestRunUpdateRequest(BaseModel):
    status: str
    finished_at: str | None = None
    metadata: dict[str, str] | None = None

class FieldTrustUpdateRequest(BaseModel):
    updates: dict[str, TrustTier]
