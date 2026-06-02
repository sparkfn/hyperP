"""Request body Pydantic models for API endpoints."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from src.repositories.protocols.merge import GoldenProfileSelection
from src.types import ApiReviewActionType, GoldenFieldName, TrustTier


class GoldenProfileSelectionRequest(BaseModel):
    field_name: Literal[
        "preferred_full_name",
        "preferred_dob",
        "preferred_phone",
        "preferred_email",
        "preferred_address",
        "preferred_nric",
    ]
    source_kind: Literal["source_record_fact", "identifier", "address"]
    selected_value: str
    source_record_pk: str | None = None
    identifier_type: str | None = None

    def to_selection(self) -> GoldenProfileSelection:
        return {
            "field_name": self.field_name,
            "source_kind": self.source_kind,
            "selected_value": self.selected_value,
            "source_record_pk": self.source_record_pk,
            "identifier_type": self.identifier_type,
        }


class AssignReviewRequest(BaseModel):
    assigned_to: str


class ReviewActionMetadata(BaseModel):
    create_manual_lock: bool = False
    follow_up_at: str | None = None
    escalation_reason: str | None = None
    survivor_person_id: str | None = None
    golden_profile_selections: list[GoldenProfileSelectionRequest] = Field(default_factory=list)


class ReviewActionRequest(BaseModel):
    action_type: ApiReviewActionType
    notes: str | None = None
    metadata: ReviewActionMetadata = Field(default_factory=ReviewActionMetadata)


class ManualMergeRequest(BaseModel):
    from_person_id: str
    to_person_id: str
    reason: str
    recompute_golden_profile: bool = True
    golden_profile_selections: list[GoldenProfileSelectionRequest] = Field(default_factory=list)


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
    field_name: GoldenFieldName
    source_record_pk: str
    reason: str


class SurvivorshipOverrideBatchItem(BaseModel):
    field_name: GoldenFieldName
    source_record_pk: str


class SurvivorshipOverrideBatchRequest(BaseModel):
    overrides: list[SurvivorshipOverrideBatchItem]
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
                    f"extraction_confidence must be in [0.0, 1.0], got {self.extraction_confidence}"
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


IngestRunMode = Literal["batch", "dump"]


class IngestRunCreateRequest(BaseModel):
    run_type: str
    mode: IngestRunMode = "batch"
    dump_path: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_dump_path(self) -> IngestRunCreateRequest:
        if self.mode == "batch":
            if self.dump_path is not None:
                raise ValueError("dump_path is only valid when mode='dump'")
            return self
        if self.dump_path is None or self.dump_path.strip() == "":
            raise ValueError("dump_path is required when mode='dump'")
        normalized = self.dump_path.replace("\\", "/").strip()
        path = PurePosixPath(normalized)
        has_windows_anchor = len(path.parts) > 0 and path.parts[0].endswith(":")
        if path.is_absolute() or has_windows_anchor:
            raise ValueError("dump_path must be relative to the dumps root")
        if ".." in path.parts:
            raise ValueError("dump_path must not contain parent traversal")
        self.dump_path = path.as_posix()
        return self


class IngestRunUpdateRequest(BaseModel):
    status: str
    finished_at: str | None = None
    metadata: dict[str, str] | None = None


class FieldTrustUpdateRequest(BaseModel):
    updates: dict[str, TrustTier]
