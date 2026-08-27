"""CRM metrics domain models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CrmActivityKindCount(BaseModel):
    """Count of CRM activities grouped by normalized history kind."""

    history_kind: str
    count: int
    last_event_at: str | None = None
    last_event_at_display: str | None = None


class CrmDealStageCount(BaseModel):
    """Count of CRM deals grouped by current stage identifier."""

    stage_id: str | None = None
    count: int


class CrmEntityBreakdown(BaseModel):
    """Per-entity CRM record counts for a person."""

    entity_key: str
    entity_display_name: str | None = None
    deal_count: int = 0
    activity_count: int = 0
    conversation_count: int = 0


class PersonCrmMetrics(BaseModel):
    """Aggregate CRM engagement metrics for one person."""

    deal_count: int = 0
    deal_stage_breakdown: list[CrmDealStageCount] = Field(default_factory=list)
    first_deal_at: str | None = None
    first_deal_at_display: str | None = None
    last_deal_at: str | None = None
    last_deal_at_display: str | None = None
    activity_count: int = 0
    call_count: int = 0
    conversation_count: int = 0
    activity_kind_breakdown: list[CrmActivityKindCount] = Field(default_factory=list)
    first_activity_at: str | None = None
    first_activity_at_display: str | None = None
    last_activity_at: str | None = None
    last_activity_at_display: str | None = None
    entity_breakdown: list[CrmEntityBreakdown] = Field(default_factory=list)
    recent_30d_deal_count: int = 0
    recent_30d_activity_count: int = 0
    recent_30d_call_count: int = 0
    recent_30d_conversation_count: int = 0
    last_crm_touch_at: str | None = None
    last_crm_touch_at_display: str | None = None
    days_since_last_crm_touch: int | None = None
    days_since_last_deal: int | None = None
    days_since_last_activity: int | None = None
    # 30-day daily trend series (oldest → newest, UTC midnight buckets). Each
    # list always has length 30; days with no events are 0.
    recent_30d_daily_deal_counts: list[int] = Field(default_factory=lambda: [0] * 30)
    recent_30d_daily_activity_counts: list[int] = Field(default_factory=lambda: [0] * 30)
    recent_30d_daily_call_counts: list[int] = Field(default_factory=lambda: [0] * 30)
    recent_30d_daily_conversation_counts: list[int] = Field(default_factory=lambda: [0] * 30)
    # Percentage change vs the prior 30-day window, rounded to int. None when
    # the prior window has no events (division-by-zero guard for the UI).
    recent_30d_deal_change_pct: int | None = None
    recent_30d_activity_change_pct: int | None = None
    recent_30d_call_change_pct: int | None = None
    recent_30d_conversation_change_pct: int | None = None
