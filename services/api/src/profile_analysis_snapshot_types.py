"""Strict internal and allowlisted output types for profile-analysis snapshots."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, TypedDict

from src.profile_analysis_domain_types import JsonValue, QualityFlag, RecordType
from src.profile_analysis_snapshot_values import CurrencyCode, SafeSnapshotLabel, SnapshotDate

SNAPSHOT_SCHEMA_VERSION: Literal["profile-analysis-snapshot-v1"] = "profile-analysis-snapshot-v1"


class AgeBand(StrEnum):
    """Coarse age groups that cannot reveal an exact date of birth."""

    UNDER_18 = "under_18"
    AGE_18_24 = "18-24"
    AGE_25_34 = "25-34"
    AGE_35_44 = "35-44"
    AGE_45_54 = "45-54"
    AGE_55_64 = "55-64"
    AGE_65_PLUS = "65_plus"


class CompletenessBand(StrEnum):
    UNKNOWN = "unknown"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SourceTrustTier(StrEnum):
    TIER_1 = "tier_1"
    TIER_2 = "tier_2"
    TIER_3 = "tier_3"
    TIER_4 = "tier_4"


class RelationshipDirection(StrEnum):
    OUTGOING = "outgoing"
    INCOMING = "incoming"
    MUTUAL = "mutual"


class SafeVehicleRelationship(StrEnum):
    OWNED = "owned"
    PURCHASED = "purchased"
    INQUIRED = "inquired"
    SERVICED = "serviced"
    OTHER = "other"


class DataQualityArea(StrEnum):
    PROFILE = "profile"
    DEMOGRAPHICS = "demographics"
    SOURCE_RECORDS = "source_records"
    ORDERS = "orders"
    VEHICLES = "vehicles"
    RELATIONSHIPS = "relationships"


class ProfileSignalsPayload(TypedDict):
    age_band: str | None
    completeness_band: str
    completeness_score: float | None


class SourceEvidencePayload(TypedDict):
    evidence_ref: str
    record_type: str
    source_category: str
    observed_date: str | None
    quality_flag: str | None
    trust_tier: str | None
    confidence: float | None


class OrderItemPayload(TypedDict):
    product: str | None
    category: str | None


class OrderEvidencePayload(TypedDict):
    evidence_ref: str
    order_date: str | None
    total: float | None
    currency: str | None
    merchant: str | None
    items: list[OrderItemPayload]


class VehicleEvidencePayload(TypedDict):
    evidence_ref: str
    product: str | None
    manufacturer: str | None
    model: str | None
    relationship_category: str


class RelationshipEvidencePayload(TypedDict):
    evidence_ref: str
    contact_alias: str
    category: str
    direction: str
    event_date: str | None


class DataQualityPayload(TypedDict):
    data_gaps: list[str]
    conflicts: list[str]
    stale_areas: list[str]
    omitted_counts: OmittedCountsPayload


class OmittedCountsPayload(TypedDict):
    sources: int
    orders: int
    order_items: int
    vehicles: int
    relationships: int


class SnapshotPayload(TypedDict):
    schema_version: Literal["profile-analysis-snapshot-v1"]
    profile: ProfileSignalsPayload
    sources: list[SourceEvidencePayload]
    orders: list[OrderEvidencePayload]
    vehicles: list[VehicleEvidencePayload]
    relationships: list[RelationshipEvidencePayload]
    data_quality: DataQualityPayload


@dataclass(frozen=True, slots=True)
class ProfileSignalsInput:
    age_band: AgeBand | None = None
    completeness_band: CompletenessBand = CompletenessBand.UNKNOWN
    completeness_score: float | None = None

    def __post_init__(self) -> None:
        _validate_optional_unit_interval(self.completeness_score, "completeness_score")

    def to_payload(self) -> ProfileSignalsPayload:
        return {
            "age_band": self.age_band.value if self.age_band is not None else None,
            "completeness_band": self.completeness_band.value,
            "completeness_score": self.completeness_score,
        }


@dataclass(frozen=True, slots=True)
class SnapshotSourceRecordInput:
    """Internal Source Record view; direct/raw fields are deliberately discarded."""

    source_record_id: str
    record_type: RecordType
    source_category: SafeSnapshotLabel
    observed_date: SnapshotDate | None
    quality_flag: QualityFlag | None
    trust_tier: SourceTrustTier | None
    confidence: float | None = None
    raw_payload: JsonValue | None = None
    raw_transcript: str | None = None
    normalized_transcript: str | None = None

    def __post_init__(self) -> None:
        _require_safe_label(self.source_category)
        _require_optional_snapshot_date(self.observed_date)
        _validate_optional_unit_interval(self.confidence, "confidence")


@dataclass(frozen=True, slots=True)
class SnapshotOrderItemInput:
    product: SafeSnapshotLabel | None = None
    category: SafeSnapshotLabel | None = None

    def __post_init__(self) -> None:
        _require_optional_safe_label(self.product)
        _require_optional_safe_label(self.category)


@dataclass(frozen=True, slots=True)
class SnapshotOrderInput:
    """Internal order view; ``order_id`` exists only for deterministic ordering."""

    order_id: str
    order_date: SnapshotDate | None = None
    total: float | None = None
    currency: CurrencyCode | None = None
    merchant: SafeSnapshotLabel | None = None
    items: tuple[SnapshotOrderItemInput, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_optional_snapshot_date(self.order_date)
        _require_optional_currency_code(self.currency)
        _require_optional_safe_label(self.merchant)
        if self.total is not None and not math.isfinite(self.total):
            raise ValueError("total must be finite")


@dataclass(frozen=True, slots=True)
class SnapshotVehicleInput:
    """Internal vehicle view; serial and LTA values are never copied out."""

    vehicle_id: str
    product: SafeSnapshotLabel | None = None
    manufacturer: SafeSnapshotLabel | None = None
    model: SafeSnapshotLabel | None = None
    relationship_category: SafeVehicleRelationship = SafeVehicleRelationship.OTHER
    serial_number: str | None = None
    lta_tag: str | None = None

    def __post_init__(self) -> None:
        _require_optional_safe_label(self.product)
        _require_optional_safe_label(self.manufacturer)
        _require_optional_safe_label(self.model)


@dataclass(frozen=True, slots=True)
class RelationshipSnapshotInput:
    """Internal directed relationship used to derive a snapshot-local alias."""

    relationship_id: str
    related_person_id: str
    category: SafeSnapshotLabel
    direction: RelationshipDirection
    event_date: SnapshotDate | None = None
    related_person_name: str | None = None

    def __post_init__(self) -> None:
        _require_safe_label(self.category)
        _require_optional_snapshot_date(self.event_date)


@dataclass(frozen=True, slots=True)
class SnapshotDataQualityInput:
    data_gaps: tuple[DataQualityArea, ...] = field(default_factory=tuple)
    conflicts: tuple[DataQualityArea, ...] = field(default_factory=tuple)
    stale_areas: tuple[DataQualityArea, ...] = field(default_factory=tuple)
    omitted_sources: int = 0
    omitted_orders: int = 0
    omitted_order_items: int = 0
    omitted_vehicles: int = 0
    omitted_relationships: int = 0

    def __post_init__(self) -> None:
        counts = (
            self.omitted_sources,
            self.omitted_orders,
            self.omitted_order_items,
            self.omitted_vehicles,
            self.omitted_relationships,
        )
        if any(isinstance(value, bool) or value < 0 for value in counts):
            raise ValueError("omitted snapshot counts must be non-negative integers")

    def to_payload(self) -> DataQualityPayload:
        return {
            "data_gaps": [area.value for area in self.data_gaps],
            "conflicts": [area.value for area in self.conflicts],
            "stale_areas": [area.value for area in self.stale_areas],
            "omitted_counts": {
                "sources": self.omitted_sources,
                "orders": self.omitted_orders,
                "order_items": self.omitted_order_items,
                "vehicles": self.omitted_vehicles,
                "relationships": self.omitted_relationships,
            },
        }


@dataclass(frozen=True, slots=True)
class ProfileSnapshotInput:
    """Typed internal input; direct identifiers are accepted only to be discarded."""

    person_id: str
    name: str | None = None
    nric: str | None = None
    phone: str | None = None
    email: str | None = None
    exact_dob: str | None = None
    exact_address: str | None = None
    postal_code: str | None = None
    profile: ProfileSignalsInput = field(default_factory=ProfileSignalsInput)
    source_records: tuple[SnapshotSourceRecordInput, ...] = field(default_factory=tuple)
    orders: tuple[SnapshotOrderInput, ...] = field(default_factory=tuple)
    vehicles: tuple[SnapshotVehicleInput, ...] = field(default_factory=tuple)
    relationships: tuple[RelationshipSnapshotInput, ...] = field(default_factory=tuple)
    data_quality: SnapshotDataQualityInput = field(default_factory=SnapshotDataQualityInput)


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    evidence_ref: str
    record_type: RecordType
    source_category: SafeSnapshotLabel
    observed_date: SnapshotDate | None
    quality_flag: QualityFlag | None
    trust_tier: SourceTrustTier | None
    confidence: float | None

    def to_payload(self) -> SourceEvidencePayload:
        return {
            "evidence_ref": self.evidence_ref,
            "record_type": self.record_type.value,
            "source_category": self.source_category.value,
            "observed_date": (self.observed_date.value if self.observed_date is not None else None),
            "quality_flag": (self.quality_flag.value if self.quality_flag is not None else None),
            "trust_tier": self.trust_tier.value if self.trust_tier is not None else None,
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class OrderItemEvidence:
    product: SafeSnapshotLabel | None
    category: SafeSnapshotLabel | None

    def to_payload(self) -> OrderItemPayload:
        return {
            "product": self.product.value if self.product is not None else None,
            "category": self.category.value if self.category is not None else None,
        }


@dataclass(frozen=True, slots=True)
class OrderEvidence:
    evidence_ref: str
    order_date: SnapshotDate | None
    total: float | None
    currency: CurrencyCode | None
    merchant: SafeSnapshotLabel | None
    items: tuple[OrderItemEvidence, ...]

    def to_payload(self) -> OrderEvidencePayload:
        return {
            "evidence_ref": self.evidence_ref,
            "order_date": self.order_date.value if self.order_date is not None else None,
            "total": self.total,
            "currency": self.currency.value if self.currency is not None else None,
            "merchant": self.merchant.value if self.merchant is not None else None,
            "items": [item.to_payload() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class VehicleEvidence:
    evidence_ref: str
    product: SafeSnapshotLabel | None
    manufacturer: SafeSnapshotLabel | None
    model: SafeSnapshotLabel | None
    relationship_category: SafeVehicleRelationship

    def to_payload(self) -> VehicleEvidencePayload:
        return {
            "evidence_ref": self.evidence_ref,
            "product": self.product.value if self.product is not None else None,
            "manufacturer": self.manufacturer.value if self.manufacturer is not None else None,
            "model": self.model.value if self.model is not None else None,
            "relationship_category": self.relationship_category.value,
        }


@dataclass(frozen=True, slots=True)
class RelationshipEvidence:
    evidence_ref: str
    contact_alias: str
    category: SafeSnapshotLabel
    direction: RelationshipDirection
    event_date: SnapshotDate | None

    def to_payload(self) -> RelationshipEvidencePayload:
        return {
            "evidence_ref": self.evidence_ref,
            "contact_alias": self.contact_alias,
            "category": self.category.value,
            "direction": self.direction.value,
            "event_date": self.event_date.value if self.event_date is not None else None,
        }


@dataclass(frozen=True, slots=True)
class ProfileAnalysisSnapshot:
    profile: ProfileSignalsInput
    sources: tuple[SourceEvidence, ...]
    orders: tuple[OrderEvidence, ...]
    vehicles: tuple[VehicleEvidence, ...]
    relationships: tuple[RelationshipEvidence, ...]
    data_quality: SnapshotDataQualityInput

    def to_payload(self) -> SnapshotPayload:
        """Return the sole explicit allowlist used for boundary serialization."""
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "profile": self.profile.to_payload(),
            "sources": [item.to_payload() for item in self.sources],
            "orders": [item.to_payload() for item in self.orders],
            "vehicles": [item.to_payload() for item in self.vehicles],
            "relationships": [item.to_payload() for item in self.relationships],
            "data_quality": self.data_quality.to_payload(),
        }


def _validate_optional_unit_interval(value: float | None, field_name: str) -> None:
    if value is not None and (not math.isfinite(value) or not 0.0 <= value <= 1.0):
        raise ValueError(f"{field_name} must be finite and between 0 and 1")


def _require_safe_label(value: SafeSnapshotLabel) -> None:
    if not isinstance(value, SafeSnapshotLabel):
        raise TypeError("copied snapshot labels require SafeSnapshotLabel")


def _require_optional_safe_label(value: SafeSnapshotLabel | None) -> None:
    if value is not None:
        _require_safe_label(value)


def _require_optional_currency_code(value: CurrencyCode | None) -> None:
    if value is not None and not isinstance(value, CurrencyCode):
        raise TypeError("copied snapshot currencies require CurrencyCode")


def _require_optional_snapshot_date(value: SnapshotDate | None) -> None:
    if value is not None and not isinstance(value, SnapshotDate):
        raise TypeError("copied snapshot dates require SnapshotDate")
