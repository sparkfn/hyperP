"""Pydantic v2 models for the ingestion pipeline."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class QualityFlag(StrEnum):
    """Canonical quality flags — closed enum per graph schema."""

    VALID = "valid"
    INVALID_FORMAT = "invalid_format"
    PLACEHOLDER_VALUE = "placeholder_value"
    SHARED_SUSPECTED = "shared_suspected"
    STALE = "stale"
    SOURCE_UNTRUSTED = "source_untrusted"
    PARTIAL_PARSE = "partial_parse"


class MatchDecision(StrEnum):
    MERGE = "merge"
    REVIEW = "review"
    NO_MATCH = "no_match"


class EngineType(StrEnum):
    DETERMINISTIC = "deterministic"
    HEURISTIC = "heuristic"
    LLM = "llm"
    MANUAL = "manual"


# ---------------------------------------------------------------------------
# Source record envelope (common contract from architecture doc)
# ---------------------------------------------------------------------------

class RawIdentifier(BaseModel):
    """A single identifier as it arrives from the source system."""

    type: str
    value: str
    is_verified: bool = False


class SourceRecordEnvelope(BaseModel):
    """Common envelope for raw source records.

    Every connector must translate upstream data into this shape before
    handing it to the pipeline.
    """

    source_system: str
    source_record_id: str
    source_record_version: str | None = None
    ingest_type: str = "batch"
    observed_at: str  # ISO-8601 datetime string
    record_hash: str
    identifiers: list[RawIdentifier] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    raw_payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Normalized intermediates
# ---------------------------------------------------------------------------

class NormalizedIdentifier(BaseModel):
    """An identifier after normalization."""

    identifier_type: str
    normalized_value: str
    is_verified: bool = False
    quality_flag: QualityFlag = QualityFlag.VALID


class NormalizedAddress(BaseModel):
    """An address after normalization, ready for graph storage."""

    unit_number: str | None = None
    street_number: str = ""
    street_name: str = ""
    building_name: str | None = None
    city: str = ""
    state_province: str | None = None
    postal_code: str = ""
    country_code: str = "SG"
    normalized_full: str = ""
    quality_flag: QualityFlag = QualityFlag.VALID


class NormalizedAttribute(BaseModel):
    """A non-identifier, non-address attribute after normalization."""

    attribute_name: str
    attribute_value: str
    quality_flag: QualityFlag = QualityFlag.VALID


# ---------------------------------------------------------------------------
# Matching results
# ---------------------------------------------------------------------------

class CandidateResult(BaseModel):
    """A candidate person discovered during graph traversal."""

    person_id: str
    source: str = "identifier"  # "identifier" or "address"


class MatchResult(BaseModel):
    """Output of the match engine chain."""

    decision: MatchDecision
    confidence: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    engine_type: EngineType = EngineType.DETERMINISTIC
    engine_version: str = "v0.1.0"
    matched_person_id: str | None = None
    is_new_person: bool = False
    feature_snapshot: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pipeline output
# ---------------------------------------------------------------------------

class IngestResult(BaseModel):
    """Summary returned after processing a single source record."""

    source_record_id: str
    source_record_pk: str | None = None
    person_id: str | None = None
    is_new_person: bool = False
    candidate_count: int = 0
    match_decision: MatchDecision | None = None
    ingest_run_id: str | None = None
    match_decision_id: str | None = None
    review_case_id: str | None = None
    errors: list[str] = Field(default_factory=list)
    skipped_duplicate: bool = False
