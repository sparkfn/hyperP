"""Typed authenticated API models for Person profile analyses."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

type ProfileAnalysisType = Literal["sales", "contact_tracing"]
type ProfileAnalysisStatus = Literal["succeeded", "failed", "obsolete"]
type ProfileAnalysisSlotRefreshState = Literal[
    "disabled", "pending", "running", "retrying", "ready", "failed"
]
type ProfileAnalysisRefreshState = Literal[
    "disabled", "pending", "running", "retrying", "ready", "partial", "failed"
]


class ProfileAnalysisCurrent(BaseModel):
    """One currently published successful analysis."""

    model_config = ConfigDict(extra="forbid")

    analysis_id: str
    person_id: str
    analysis_type: ProfileAnalysisType
    status: Literal["succeeded"]
    content: str
    input_revision: int = Field(ge=0)
    input_fingerprint: str
    prompt_version: str
    provider: str
    model: str
    started_at: str
    completed_at: str
    completed_at_display: str
    attempt_number: int = Field(ge=1)


class ProfileAnalysisSlot(BaseModel):
    """Current output and independent refresh state for one analysis type."""

    model_config = ConfigDict(extra="forbid")

    current: ProfileAnalysisCurrent | None
    stale: bool
    refresh_state: ProfileAnalysisSlotRefreshState
    failure_code: str | None


class PersonProfileAnalyses(BaseModel):
    """Current independent sales and contact-tracing analysis slots."""

    model_config = ConfigDict(extra="forbid")

    input_revision: int = Field(ge=0)
    refresh_state: ProfileAnalysisRefreshState
    sales: ProfileAnalysisSlot
    contact_tracing: ProfileAnalysisSlot


class ProfileAnalysisHistoryItem(BaseModel):
    """One safe terminal profile-analysis history entry."""

    model_config = ConfigDict(extra="forbid")

    analysis_id: str
    person_id: str
    analysis_type: ProfileAnalysisType
    status: ProfileAnalysisStatus
    content: str | None
    input_revision: int = Field(ge=0)
    input_fingerprint: str
    prompt_version: str
    provider: str
    model: str
    started_at: str
    completed_at: str
    completed_at_display: str
    attempt_number: int = Field(ge=1)
    failure_code: str | None
    retryable: bool | None
    next_retry_at: str | None
