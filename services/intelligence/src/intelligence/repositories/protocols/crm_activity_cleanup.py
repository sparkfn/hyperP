"""Typed exact-identity contract for manifest-gated CRM activity cleanup.

The graph boundary deliberately exposes no generic Cypher or traversal escape hatch.
Every input is finite, canonically ordered, and revalidated immediately before a
write transaction can remove an explicitly enumerated relationship or node.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

CleanupClassification = Literal["deleted", "already_absent", "retained", "conflict", "failed"]
RelationshipDirection = Literal["inbound", "outbound", "self"]
RecordType = Literal["crm_history", "call"]

MAX_BATCH_IDENTITIES = 1_000
MAX_AUTHORIZED_IDENTITIES = 100_000
MAX_INCIDENT_RELATIONSHIPS = 1_000


class GraphRelationshipRow(TypedDict):
    """The closed, scalar/map shape returned by the inspection query."""

    relationship_element_id: str
    direction: str
    relationship_type: str
    other_element_id: str
    other_labels: list[str]
    other_source_record_pk: str | None
    other_person_id: str | None
    other_identifier_type: str | None
    other_identifier_comparison_token: str | None
    other_source_key: str | None
    other_review_case_id: str | None
    other_match_decision_id: str | None


def _text(value: str, field: str, *, optional: bool = False) -> None:
    if optional and value == "":
        return
    if not value or len(value) > 1_024 or "\x00" in value:
        raise ValueError(f"{field} must be bounded non-empty text")


def _optional_text(value: str | None, field: str) -> None:
    if value is not None:
        _text(value, field)


def _canonical_strings(values: tuple[str, ...], field: str, maximum: int) -> None:
    if not values or len(values) > maximum or tuple(sorted(set(values))) != values:
        raise ValueError(f"{field} must be finite, unique, and canonically ordered")
    for value in values:
        _text(value, field)


def _canonical_labels(values: tuple[str, ...], field: str) -> None:
    if len(values) > 32 or tuple(sorted(set(values))) != values:
        raise ValueError(f"{field} must be finite, unique, and canonically ordered")
    for value in values:
        _text(value, field)


@dataclass(frozen=True)
class ParentIdentity:
    """Immutable stored-parent fields from the accepted activity snapshot."""

    source_record_pk: str | None
    source_instance_id: str | None
    source_record_id: str | None
    record_type: str | None
    source_system: str | None

    def __post_init__(self) -> None:
        for value, field in (
            (self.source_record_pk, "parent source_record_pk"),
            (self.source_instance_id, "parent source_instance_id"),
            (self.source_record_id, "parent source_record_id"),
            (self.record_type, "parent record_type"),
            (self.source_system, "parent source_system"),
        ):
            _optional_text(value, field)


@dataclass(frozen=True)
class LiveTargetIdentity:
    """One immutable manifest-authorized source record identity.

    ``source_record_id``, ``source_version_key``, and projection fields are
    accepted-manifest facts. ``None`` is an exact expected absence, not a
    wildcard, preserving fail-closed compatibility for legacy constructors.
    """

    source_record_pk: str
    record_type: RecordType
    source_instance_id: str
    source_record_version: str
    record_hash: str
    lifecycle_status: str | None
    history_family: str | None
    stored_parent: ParentIdentity
    source_key: str
    child_parent_source_record_pks: tuple[str, ...] = ()
    details_parent_source_record_pks: tuple[str, ...] = ()
    source_record_id: str | None = None
    source_version_key: str | None = None
    history_source: str | None = None
    projection_source: str | None = None
    projection_version: str | None = None

    def __post_init__(self) -> None:
        for value, field in (
            (self.source_record_pk, "source_record_pk"),
            (self.source_instance_id, "source_instance_id"),
            (self.source_record_version, "source_record_version"),
            (self.record_hash, "record_hash"),
            (self.source_key, "source_key"),
        ):
            _text(value, field)
        if self.record_type not in {"crm_history", "call"}:
            raise ValueError("cleanup record type is unsupported")
        _optional_text(self.lifecycle_status, "lifecycle_status")
        _optional_text(self.history_family, "history_family")
        for optional_value, field in (
            (self.source_record_id, "source_record_id"),
            (self.source_version_key, "source_version_key"),
            (self.history_source, "history_source"),
            (self.projection_source, "projection_source"),
            (self.projection_version, "projection_version"),
        ):
            _optional_text(optional_value, field)
        for values, field in (
            (self.child_parent_source_record_pks, "CHILD_OF parent identities"),
            (self.details_parent_source_record_pks, "DETAILS_HISTORY_ITEM parent identities"),
        ):
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{field} must be unique and canonically ordered")
            for value in values:
                _text(value, field)


@dataclass(frozen=True)
class EndpointIdentity:
    """An exact bounded endpoint description for an incident relationship."""

    element_id: str
    labels: tuple[str, ...]
    source_record_pk: str | None
    person_id: str | None
    identifier_type: str | None
    identifier_comparison_token: str | None
    source_key: str | None
    review_case_id: str | None
    match_decision_id: str | None

    def __post_init__(self) -> None:
        _text(self.element_id, "endpoint element id")
        _canonical_labels(self.labels, "endpoint labels")
        for value, field in (
            (self.source_record_pk, "endpoint source_record_pk"),
            (self.person_id, "endpoint person_id"),
            (self.identifier_type, "endpoint identifier_type"),
            (self.identifier_comparison_token, "endpoint identifier comparison token"),
            (self.source_key, "endpoint source_key"),
            (self.review_case_id, "endpoint review_case_id"),
            (self.match_decision_id, "endpoint match_decision_id"),
        ):
            _optional_text(value, field)

    def key(self) -> tuple[str, tuple[str, ...], str, str, str, str, str, str, str]:
        return (
            self.element_id,
            self.labels,
            self.source_record_pk or "",
            self.person_id or "",
            self.identifier_type or "",
            self.identifier_comparison_token or "",
            self.source_key or "",
            self.review_case_id or "",
            self.match_decision_id or "",
        )

    @property
    def identifier_value(self) -> str | None:
        """Compatibility view of the opaque token; it is never an Identifier value."""
        return self.identifier_comparison_token


@dataclass(frozen=True)
class IncidentRelationship:
    """All-direction relationship evidence; element IDs are opaque Neo4j IDs."""

    relationship_element_id: str
    direction: RelationshipDirection
    relationship_type: str
    other_endpoint: EndpointIdentity

    def __post_init__(self) -> None:
        _text(self.relationship_element_id, "relationship element id")
        _text(self.relationship_type, "relationship type")
        if self.direction not in {"inbound", "outbound", "self"}:
            raise ValueError("relationship direction is invalid")

    def key(
        self,
    ) -> tuple[str, str, str, tuple[str, tuple[str, ...], str, str, str, str, str, str, str]]:
        return (
            self.relationship_type,
            self.direction,
            self.relationship_element_id,
            self.other_endpoint.key(),
        )


@dataclass(frozen=True)
class ObservedRecord:
    """The immutable fields read from one exactly matched graph node."""

    element_id: str
    labels: tuple[str, ...]
    record_type: str | None
    source_instance_id: str | None
    source_record_version: str | None
    record_hash: str | None
    lifecycle_status: str | None
    history_family: str | None
    stored_parent: ParentIdentity
    source_record_id: str | None = None
    source_version_key: str | None = None
    history_source: str | None = None
    projection_source: str | None = None
    projection_version: str | None = None

    def __post_init__(self) -> None:
        _text(self.element_id, "record element id")
        _canonical_labels(self.labels, "record labels")
        for value, field in (
            (self.record_type, "record_type"),
            (self.source_instance_id, "source_instance_id"),
            (self.source_record_version, "source_record_version"),
            (self.record_hash, "record_hash"),
            (self.lifecycle_status, "lifecycle_status"),
            (self.history_family, "history_family"),
            (self.source_record_id, "source_record_id"),
            (self.source_version_key, "source_version_key"),
            (self.history_source, "history_source"),
            (self.projection_source, "projection_source"),
            (self.projection_version, "projection_version"),
        ):
            _optional_text(value, field)


@dataclass(frozen=True)
class ExactRecordInspection:
    """One result for one requested PK, including absence and duplicate delivery."""

    source_record_pk: str
    matching_node_count: int
    record: ObservedRecord | None
    incident_relationships: tuple[IncidentRelationship, ...]
    incident_relationship_count: int
    relationship_inventory_truncated: bool

    def __post_init__(self) -> None:
        _text(self.source_record_pk, "source_record_pk")
        if (
            not isinstance(self.matching_node_count, int)
            or isinstance(self.matching_node_count, bool)
            or self.matching_node_count < 0
        ):
            raise ValueError("matching node count is invalid")
        if self.matching_node_count == 0 and self.record is not None:
            raise ValueError("absent identity cannot have a node")
        if self.matching_node_count == 1 and self.record is None:
            raise ValueError("present identity lacks a node")
        if (
            not isinstance(self.incident_relationship_count, int)
            or isinstance(self.incident_relationship_count, bool)
            or self.incident_relationship_count < len(self.incident_relationships)
        ):
            raise ValueError("incident relationship count is invalid")
        if len(self.incident_relationships) > MAX_INCIDENT_RELATIONSHIPS + 1:
            raise ValueError("incident relationship inventory exceeds its ceiling")
        if self.relationship_inventory_truncated != (
            self.incident_relationship_count > len(self.incident_relationships)
        ):
            raise ValueError("incident relationship truncation is inconsistent")
        if (
            tuple(sorted(self.incident_relationships, key=IncidentRelationship.key))
            != self.incident_relationships
        ):
            raise ValueError("incident relationship inventory is not canonical")


@dataclass(frozen=True)
class ProtectedEvidence:
    """A protected dependency that must survive because its ownership is unproven."""

    selected_source_record_pk: str
    relationship: IncidentRelationship

    def __post_init__(self) -> None:
        _text(self.selected_source_record_pk, "protected selected source_record_pk")

    def key(
        self,
    ) -> tuple[
        str, tuple[str, str, str, tuple[str, tuple[str, ...], str, str, str, str, str, str, str]]
    ]:
        return (self.selected_source_record_pk, self.relationship.key())


@dataclass(frozen=True)
class ExpectedDeletionFact:
    """Exactly what a write transaction must observe again before it mutates."""

    target: LiveTargetIdentity
    observed: ObservedRecord
    incident_relationships: tuple[IncidentRelationship, ...]
    owned_relationship_element_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            tuple(sorted(self.incident_relationships, key=IncidentRelationship.key))
            != self.incident_relationships
        ):
            raise ValueError("expected relationships are not canonical")
        _canonical_strings(
            self.owned_relationship_element_ids,
            "owned relationship element ids",
            MAX_INCIDENT_RELATIONSHIPS,
        )
        actual = {item.relationship_element_id for item in self.incident_relationships}
        if not set(self.owned_relationship_element_ids).issubset(actual):
            raise ValueError("expected deletion refers to an unobserved relationship")


@dataclass(frozen=True)
class RecordOutcome:
    source_record_pk: str
    classification: CleanupClassification
    reason_code: str

    def __post_init__(self) -> None:
        _text(self.source_record_pk, "outcome source_record_pk")
        _text(self.reason_code, "outcome reason_code")


@dataclass(frozen=True)
class BatchOutcome:
    """Deterministic partition for exact deletions and required absences."""

    database_identity: str
    outcomes: tuple[RecordOutcome, ...]
    mutation_applied: bool

    def __post_init__(self) -> None:
        _text(self.database_identity, "database identity")
        identities = tuple(item.source_record_pk for item in self.outcomes)
        if identities:
            _canonical_strings(identities, "batch outcome identities", MAX_AUTHORIZED_IDENTITIES)

    def for_classification(
        self, classification: CleanupClassification
    ) -> tuple[RecordOutcome, ...]:
        return tuple(item for item in self.outcomes if item.classification == classification)


@dataclass(frozen=True)
class CleanupPlan:
    expected_deletions: tuple[ExpectedDeletionFact, ...]
    protected_evidence: tuple[ProtectedEvidence, ...]
    outcomes: tuple[RecordOutcome, ...]

    def __post_init__(self) -> None:
        expected = tuple(item.target.source_record_pk for item in self.expected_deletions)
        if expected:
            _canonical_strings(expected, "expected deletion identities", MAX_BATCH_IDENTITIES)
        protected = tuple(item.key() for item in self.protected_evidence)
        if protected != tuple(sorted(set(protected))):
            raise ValueError("protected evidence is not canonical")
        outcome_ids = tuple(item.source_record_pk for item in self.outcomes)
        if outcome_ids:
            _canonical_strings(outcome_ids, "plan outcome identities", MAX_BATCH_IDENTITIES)


class CrmActivityCleanupRepository(Protocol):
    """Narrow graph capability; no graph-wide scan or generic write method exists."""

    def database_identity(self) -> str: ...

    def inspect(
        self,
        source_record_pks: tuple[str, ...],
        relationship_limit: int = MAX_INCIDENT_RELATIONSHIPS,
    ) -> tuple[ExactRecordInspection, ...]: ...

    def plan(
        self,
        identities: tuple[LiveTargetIdentity, ...],
        inspections: tuple[ExactRecordInspection, ...],
    ) -> CleanupPlan: ...

    def delete_batch(
        self,
        database_identity: str,
        expected: tuple[ExpectedDeletionFact, ...],
    ) -> BatchOutcome: ...

    def verify_protected(
        self, protected: tuple[ProtectedEvidence, ...]
    ) -> tuple[ProtectedEvidence, ...]: ...

    def close(self) -> None: ...


class RequiredAbsenceCrmActivityCleanupRepository(CrmActivityCleanupRepository, Protocol):
    """Optional transactional extension for revalidating selected absences."""

    def delete_batch_with_required_absences(
        self,
        database_identity: str,
        expected: tuple[ExpectedDeletionFact, ...],
        required_absent: tuple[LiveTargetIdentity, ...],
    ) -> BatchOutcome: ...
