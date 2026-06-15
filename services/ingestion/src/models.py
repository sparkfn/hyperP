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

    The "system family" — ``identity``, ``bankruptcy``, ``relationship`` —
    are all deterministic extracts from a system of record that share the same
    generic matching behaviour (see :data:`SYSTEM_FAMILY`), and are kept as
    distinct values so they can carry their own criteria: ``bankruptcy`` gates
    its exact-NRIC merge on a partial name match, and ``relationship`` adds a
    phone + partial-name auto-merge promotion. Per-type detail:

    ``identity`` — first-party identity from a transactional system of record
    (Fundbox users/legacy/merged, Eko, SpeedZone).
    ``bankruptcy`` — government register about a person (SG Bankruptcy Register);
    carries a verified NRIC + name, runs the person pipeline, member of the
    system family.
    ``rental_flat`` — government register about a place (SG Rental Flats); address
    attributes only, no person identifier; routed address-only by
    ``source_system`` so it never reaches the match engine; NOT in the system
    family.
    ``relationship`` — a record whose subject is a different person, e.g. a
    Fundbox emergency contact that feeds ``KNOWS``.
    ``conversation`` — heuristic extract from chat / voice transcripts.
    Conversation records are never eligible for deterministic auto-merge.
    ``sales`` — order/line-item/product extract from a commerce system. Linked
    to a Person indirectly via FOR_CUSTOMER_RECORD; sales records never force
    identity resolution on their own and never auto-merge.
    """

    IDENTITY = "identity"
    BANKRUPTCY = "bankruptcy"
    RENTAL_FLAT = "rental_flat"
    RELATIONSHIP = "relationship"
    CONVERSATION = "conversation"
    SALES = "sales"


#: Record types that descend from the former ``system`` provenance class. Every
#: matching branch that historically tested ``record_type == SYSTEM`` (or its
#: negation) now tests membership here, so these three behave identically until
#: deliberately diverged. ``rental_flat`` is deliberately excluded — it is a
#: place register routed address-only, never reaching the person match engine.
SYSTEM_FAMILY: frozenset[RecordType] = frozenset(
    {RecordType.IDENTITY, RecordType.BANKRUPTCY, RecordType.RELATIONSHIP}
)


# ---------------------------------------------------------------------------
# Source record envelope (common contract from architecture doc)
# ---------------------------------------------------------------------------


class RawIdentifier(BaseModel):
    """A single identifier as it arrives from the source system."""

    type: str
    value: str
    is_verified: bool = False


class RawAddress(BaseModel):
    """Structured address evidence as it arrives from the source system."""

    raw: str | None = None
    unit_number: str | None = None
    street_number: str | None = None
    street_name: str | None = None
    building_name: str | None = None
    city: str | None = None
    state_province: str | None = None
    postal_code: str | None = None
    country_code: str | None = None


class SourceRecordEnvelope(BaseModel):
    """Common envelope for raw source records.

    Every connector must translate upstream data into this shape before
    handing it to the pipeline.
    """

    source_system: str
    source_record_id: str
    source_record_version: str | None = None
    record_type: RecordType = RecordType.IDENTITY
    ingest_type: str = "batch"
    observed_at: str  # ISO-8601 datetime string
    record_hash: str
    identifiers: list[RawIdentifier] = Field(default_factory=list)
    addresses: list[RawAddress] = Field(default_factory=list)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    raw_payload: dict[str, JsonValue] = Field(default_factory=dict)
    # Conversation-only provenance fields. Required when record_type ==
    # CONVERSATION; ignored otherwise.
    extraction_confidence: float | None = None
    extraction_method: str | None = None
    conversation_ref: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def _check_record_type_invariants(self) -> SourceRecordEnvelope:
        """Conversation records must declare extraction provenance; others must not.

        - ``conversation`` envelopes require ``extraction_confidence`` (in
          ``[0.0, 1.0]``) and ``extraction_method``.
        - Every non-conversation envelope must leave all three conversation-only
          fields unset, so that downstream code can rely on them being ``None``
          whenever ``record_type != CONVERSATION``.
        """
        if self.record_type == RecordType.CONVERSATION:
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
    # When an incoming record independently matches (MERGE band) more than one
    # distinct active person, the record and its extracted evidence are linked to
    # ALL of them — ``matched_person_id`` (the primary) plus every id here — but
    # the persons are NOT merged (they may legitimately share an identifier).
    # Empty in the common single-match case.
    additional_linked_person_ids: list[str] = Field(default_factory=list)
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
