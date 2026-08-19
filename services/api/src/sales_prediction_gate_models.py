"""Typed private evidence and privacy-safe output for CRM-WON Gate 1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

LabelStatus = Literal["positive", "negative", "censored", "unknown", "ineligible"]
GateDecision = Literal["go", "rules_only", "collect_more_data", "stop"]
MappedState = Literal["open", "won", "lost"]


@dataclass(frozen=True, slots=True)
class GateRelease:
    enabled: bool
    mapping_version: str
    policy_version: str
    accepted_at: datetime
    evidence_cutoff_at: datetime
    source_accounting_complete: bool
    analytical_release_consistent: bool
    restated_event_count: int


@dataclass(frozen=True, slots=True)
class StageEvent:
    event_identity: str
    parent_key: tuple[str, str]
    mapped_state: MappedState
    event_at: datetime
    available_at: datetime
    authority_head_version: int


@dataclass(frozen=True, slots=True)
class DealVersion:
    parent_key: tuple[str, str]
    version_key: str
    source_record_version: int
    entity_key: str | None
    observed_at: datetime | None
    ingested_at: datetime | None
    activated_at: datetime | None
    superseded_at: datetime | None
    rejected_at: datetime | None
    link_failed_at: datetime | None
    linked_person_count: int
    active_person_count: int
    latest_linked_at: datetime | None
    timestamps_valid: bool
    amount_state: str
    currency_status: str
    lifecycle_status: str = "active"
    linked_person_ids: tuple[str, ...] = ()
    active_person_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LabelEvidence:
    private_parent_key: tuple[str, str]
    snapshot_at: datetime
    month: str
    entity_key: str
    status: LabelStatus
    reason: str
    mature: bool
    person_linked: bool
    timestamp_valid: bool
    history_determinate: bool
    amount_state: str
    currency_status: str
    amount_reconstructable: bool = False


@dataclass(frozen=True, slots=True)
class PopulationMetrics:
    entity_key: str
    snapshot_count: int
    unique_deal_count: int
    matured_eligible_deals: int
    positive_deals: int
    negative_deals: int
    unknown_snapshots: int
    censored_snapshots: int
    ineligible_snapshots: int
    selected_parent_ambiguity_snapshots: int
    missing_parent_snapshots: int
    usable_months: int
    rolling_temporal_folds: int
    positive_rate: float | None
    analytically_determinate_rate: float
    valid_timestamp_rate: float
    deterministic_person_linkage_rate: float
    data_quality_unknown_censored_rate: float
    amount_known_rate: float
    amount_zero_rate: float
    amount_reconstructable_rate: float = 0.0
    amount_revision_availability: str = "snapshot_versioned"
    optional_interaction_coverage: str = "not_evaluated_non_blocking"


@dataclass(frozen=True, slots=True)
class ThresholdResult:
    name: str
    required: str
    observed: str
    passed: bool


@dataclass(frozen=True, slots=True)
class PopulationDecision:
    entity_key: str
    recommendation: GateDecision
    metrics: PopulationMetrics
    thresholds: tuple[ThresholdResult, ...]


@dataclass(frozen=True, slots=True)
class GateMetadata:
    report_schema_version: str
    selector_version: str
    mapping_version: str
    policy_version: str
    eligibility_version: str
    evidence_cutoff_status: str
    accepted_source_boundary_status: str
    restatement_version: str
    restatement_status: str
    availability_semantics: str = "operational_as_known"


@dataclass(frozen=True, slots=True)
class GateReport:
    generated_at: str
    metadata: GateMetadata
    populations: tuple[PopulationDecision, ...]
    monthly_counts: tuple[dict[str, str | int | float | None], ...]
