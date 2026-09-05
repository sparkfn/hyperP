"""Typed, privacy-safe CRM metric response models."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class CrmDealStageCount(BaseModel):
    stage_id: str | None = None
    count: int


class CrmDealEntityBreakdown(BaseModel):
    entity_key: str
    entity_display_name: str | None = None
    deal_count: int = 0
    conversation_count: int = 0


class PersonCrmDealMetrics(BaseModel):
    """Neo4j-backed deals and Open Lines conversations only."""

    deal_count: int = 0
    deal_stage_breakdown: list[CrmDealStageCount] = Field(default_factory=list)
    first_deal_at: str | None = None
    first_deal_at_display: str | None = None
    last_deal_at: str | None = None
    last_deal_at_display: str | None = None
    conversation_count: int = 0
    last_conversation_at: str | None = None
    last_conversation_at_display: str | None = None
    recent_30d_deal_count: int = 0
    recent_30d_conversation_count: int = 0
    recent_30d_daily_deal_counts: list[int] = Field(default_factory=lambda: [0] * 30)
    recent_30d_daily_conversation_counts: list[int] = Field(default_factory=lambda: [0] * 30)
    recent_30d_deal_change_pct: int | None = None
    recent_30d_conversation_change_pct: int | None = None
    last_graph_crm_touch_at: str | None = None
    last_graph_crm_touch_at_display: str | None = None
    days_since_last_deal: int | None = None
    entity_breakdown: list[CrmDealEntityBreakdown] = Field(default_factory=list)


class CrmActivityKindCount(BaseModel):
    history_kind: str
    count: int
    last_event_at: str | None = None
    last_event_at_display: str | None = None


class CrmCallClassificationCount(BaseModel):
    classification: str
    count: int


CrmActivityFailureReason = Literal[
    "not_configured",
    "source_unavailable",
    "deal_limit",
    "request_limit",
    "page_limit",
    "row_limit",
    "elapsed_limit",
    "non_advancing_pagination",
    "rate_limited",
    "timeout",
    "upstream_error",
    "malformed_response",
]


class CrmActivityMetricsBase(BaseModel):
    source: Literal["bitrix_crm_activity"] = "bitrix_crm_activity"
    source_instance: str
    fetched_at: str
    fetched_at_display: str | None = None
    cache_disposition: Literal["miss", "hit", "coalesced", "disabled"]
    completeness: Literal["complete", "partial", "unavailable"]
    truncated: bool
    queried_deal_count: int
    resolved_deal_count: int
    request_count: int
    page_count: int
    row_count: int
    failure_reason: CrmActivityFailureReason | None = None


class CrmActivityAggregate(CrmActivityMetricsBase):
    activity_count: int
    call_count: int
    activity_kind_breakdown: list[CrmActivityKindCount]
    call_classification_breakdown: list[CrmCallClassificationCount]
    first_activity_at: str | None = None
    first_activity_at_display: str | None = None
    last_activity_at: str | None = None
    last_activity_at_display: str | None = None
    recent_30d_activity_count: int
    recent_30d_call_count: int
    recent_30d_daily_activity_counts: list[int]
    recent_30d_daily_call_counts: list[int]
    recent_30d_activity_change_pct: int | None = None
    recent_30d_call_change_pct: int | None = None


class PersonCrmActivityMetricsComplete(CrmActivityAggregate):
    status: Literal["complete"] = "complete"
    completeness: Literal["complete"] = "complete"
    truncated: Literal[False] = False
    failure_reason: None = None


class PersonCrmActivityMetricsPartial(CrmActivityAggregate):
    status: Literal["partial"] = "partial"
    completeness: Literal["partial"] = "partial"
    truncated: bool = True
    failure_reason: CrmActivityFailureReason


class PersonCrmActivityMetricsUnavailable(CrmActivityMetricsBase):
    status: Literal["unavailable"] = "unavailable"
    completeness: Literal["unavailable"] = "unavailable"
    truncated: Literal[False] = False
    failure_reason: CrmActivityFailureReason


PersonCrmActivityMetrics = Annotated[
    PersonCrmActivityMetricsComplete
    | PersonCrmActivityMetricsPartial
    | PersonCrmActivityMetricsUnavailable,
    Field(discriminator="status"),
]


class BitrixDealScope(BaseModel):
    canonical_person_id: str
    deal_ids: tuple[str, ...]
    resolved_deal_count: int
    deal_limit_exhausted: bool
    source_authorized: bool = True
    scope_valid: bool = True
