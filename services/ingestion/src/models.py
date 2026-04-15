"""Pydantic v2 models for the ingestion pipeline."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator
from pydantic.types import JsonValue

# Re-export pydantic's recursive ``JsonValue`` (str | int | float | bool |
# None | list[JsonValue] | dict[str, JsonValue]) so the rest of the codebase
# can import it from one place. Using pydantic's alias rather than rolling
# our own avoids PEP 695 / pydantic schema-resolution friction with custom
# recursive ``type`` statements.
__all__ = ["JsonValue"]

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


class RecordType(StrEnum):
    """Provenance class of a SourceRecord.

    ``system`` — deterministic extract from another service's system of record.
    ``conversation`` — heuristic extract from chat / voice transcripts.
    Conversation records are never eligible for deterministic auto-merge.
    ``sales`` — order/line-item/product extract from a commerce system. Linked
    to a Person indirectly via FOR_CUSTOMER_RECORD; sales records never force
    identity resolution on their own and never auto-merge.
    """

    SYSTEM = "system"
    CONVERSATION = "conversation"
    SALES = "sales"


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
    record_type: RecordType = RecordType.SYSTEM
    ingest_type: str = "batch"
    observed_at: str  # ISO-8601 datetime string
    record_hash: str
    identifiers: list[RawIdentifier] = Field(default_factory=list)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    raw_payload: dict[str, JsonValue] = Field(default_factory=dict)
    # Conversation-only provenance fields. Required when record_type ==
    # CONVERSATION; ignored otherwise.
    extraction_confidence: float | None = None
    extraction_method: str | None = None
    conversation_ref: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def _check_record_type_invariants(self) -> SourceRecordEnvelope:
        """Conversation records must declare extraction provenance; system records must not.

        - ``conversation`` envelopes require ``extraction_confidence`` (in
          ``[0.0, 1.0]``) and ``extraction_method``.
        - ``system`` envelopes must leave all three conversation-only fields
          unset, so that downstream code can rely on them being ``None``
          whenever ``record_type == SYSTEM``.
        """
        if self.record_type == RecordType.CONVERSATION:
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
    feature_snapshot: dict[str, JsonValue] = Field(default_factory=dict)


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
