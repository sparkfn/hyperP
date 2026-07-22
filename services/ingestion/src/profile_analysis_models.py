"""Strict persistence models for immutable Person profile analyses."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TypedDict
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)


class ProfileAnalysisType(StrEnum):
    """Supported independent Person-analysis outputs."""

    SALES = "sales"
    CONTACT_TRACING = "contact_tracing"


class ProfileAnalysisStatus(StrEnum):
    """Terminal status of an immutable analysis attempt."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OBSOLETE = "obsolete"


class ProfileAnalysisCypherParameters(TypedDict):
    """Neo4j-driver-safe parameters for one persistence attempt."""

    analysis_id: str
    person_id: str
    analysis_type: str
    status: str
    content: str | None
    input_revision: int
    input_fingerprint: str
    prompt_version: str
    provider: str
    model: str
    started_at: datetime
    completed_at: datetime
    failure_code: str | None
    retryable: bool | None
    next_retry_at: datetime | None
    attempt_number: int


class ProfileAnalysisProvenance(BaseModel):
    """Revision, model, and timing provenance captured for an attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_revision: StrictInt = Field(ge=0)
    input_fingerprint: StrictStr = Field(min_length=1)
    prompt_version: StrictStr = Field(min_length=1)
    provider: StrictStr = Field(min_length=1)
    model: StrictStr = Field(min_length=1)
    started_at: AwareDatetime
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_timing(self) -> ProfileAnalysisProvenance:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        return self


class ProfileAnalysisAttempt(ProfileAnalysisProvenance):
    """Validated data persisted on one immutable ProfileAnalysis node."""

    analysis_id: UUID
    person_id: StrictStr = Field(min_length=1)
    analysis_type: ProfileAnalysisType
    status: ProfileAnalysisStatus
    content: StrictStr | None = None
    failure_code: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
    )
    retryable: StrictBool | None = None
    next_retry_at: AwareDatetime | None = None
    attempt_number: StrictInt = Field(ge=1)

    @model_validator(mode="after")
    def validate_status_payload(self) -> ProfileAnalysisAttempt:
        failure_metadata = (
            self.failure_code,
            self.retryable,
            self.next_retry_at,
        )
        if self.status is ProfileAnalysisStatus.SUCCEEDED:
            if self.content is None or not self.content.strip():
                raise ValueError("succeeded attempts require non-empty content")
            if any(value is not None for value in failure_metadata):
                raise ValueError("succeeded attempts cannot carry failure metadata")
            return self

        if self.content is not None:
            raise ValueError("content is only valid for succeeded attempts")
        if self.status is ProfileAnalysisStatus.OBSOLETE:
            if any(value is not None for value in failure_metadata):
                raise ValueError("obsolete attempts cannot carry failure metadata")
            return self

        if self.failure_code is None:
            raise ValueError("failed attempts require failure_code")
        if self.retryable is None:
            raise ValueError("failed attempts require retryable")
        if self.retryable and self.next_retry_at is None:
            raise ValueError("retryable failures require next_retry_at")
        if not self.retryable and self.next_retry_at is not None:
            raise ValueError("non-retryable failures forbid next_retry_at")
        return self

    def to_cypher_parameters(self) -> ProfileAnalysisCypherParameters:
        """Return driver-safe values without JSON-serializing temporal fields."""
        return {
            "analysis_id": str(self.analysis_id),
            "person_id": self.person_id,
            "analysis_type": self.analysis_type.value,
            "status": self.status.value,
            "content": self.content,
            "input_revision": self.input_revision,
            "input_fingerprint": self.input_fingerprint,
            "prompt_version": self.prompt_version,
            "provider": self.provider,
            "model": self.model,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "failure_code": self.failure_code,
            "retryable": self.retryable,
            "next_retry_at": self.next_retry_at,
            "attempt_number": self.attempt_number,
        }
