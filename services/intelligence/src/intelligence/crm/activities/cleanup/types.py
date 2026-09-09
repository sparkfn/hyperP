"""Strict, graph-agnostic value objects for logical CRM activity cleanup."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, cast

from intelligence.artifacts import canonical_json
from intelligence.crm.activities.models import validate_snapshot_id

ReceiptDisposition = Literal["deleted", "already_absent", "retained", "conflict", "failed"]
RECEIPT_SCHEMA = "crm-activities-cleanup-receipt-v6"
CHECKPOINT_SCHEMA = "crm-activities-cleanup-checkpoint-v2"
RECONCILIATION_SCHEMA = "crm-activities-cleanup-reconciliation-v2"
PROTECTED_PRESERVATION_SCHEMA = "crm-activities-cleanup-protected-preservation-v2"
QUIESCENCE_EVIDENCE_SCHEMA = "crm-activities-cleanup-quiescence-v2"
PROTECTED_PRESERVATION_SCOPE = "exact-selected-from-source-endpoints-and-unowned-relationships"
SHA256_HEX = frozenset("0123456789abcdef")
DISPOSITIONS = frozenset({"deleted", "already_absent", "retained", "conflict", "failed"})


def canonical_digest(value: object) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def require_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or set(value) - SHA256_HEX:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def require_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"{field} is invalid")
    if value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise ValueError(f"{field} is invalid")
    return value


def require_count(value: object, field: str, maximum: int = 1_000_000) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum:
        raise ValueError(f"{field} is invalid")
    return value


def require_true(value: object, field: str) -> bool:
    if value is not True:
        raise ValueError(f"{field} must be true")
    return True


def require_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field} is invalid")
    return value


def exact_keys(value: Mapping[str, object], expected: frozenset[str], field: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{field} schema is invalid")


@dataclass(frozen=True)
class ProtectedPreservationProof:
    """Bounded exact proof; it never claims preservation of a graph-wide class."""

    selected_identity_count: int
    selected_identity_digest: str
    relationship_count: int
    relationship_digest: str
    source_endpoint_count: int
    source_endpoint_digest: str

    def __post_init__(self) -> None:
        require_count(self.selected_identity_count, "protected selected identity count")
        require_count(self.relationship_count, "protected relationship count")
        require_count(self.source_endpoint_count, "protected source endpoint count")
        require_digest(self.selected_identity_digest, "protected selected identity digest")
        require_digest(self.relationship_digest, "protected relationship digest")
        require_digest(self.source_endpoint_digest, "protected source endpoint digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PROTECTED_PRESERVATION_SCHEMA,
            "scope": PROTECTED_PRESERVATION_SCOPE,
            "selected_identity_count": self.selected_identity_count,
            "selected_identity_digest": self.selected_identity_digest,
            "relationship_count": self.relationship_count,
            "relationship_digest": self.relationship_digest,
            "source_endpoint_count": self.source_endpoint_count,
            "source_endpoint_digest": self.source_endpoint_digest,
        }

    @classmethod
    def parse(cls, value: object) -> ProtectedPreservationProof:
        raw = require_mapping(value, "protected preservation proof")
        exact_keys(
            raw,
            frozenset(
                {
                    "schema_version",
                    "scope",
                    "selected_identity_count",
                    "selected_identity_digest",
                    "relationship_count",
                    "relationship_digest",
                    "source_endpoint_count",
                    "source_endpoint_digest",
                }
            ),
            "protected preservation proof",
        )
        if raw["schema_version"] != PROTECTED_PRESERVATION_SCHEMA:
            raise ValueError("protected preservation proof schema is unsupported")
        if raw["scope"] != PROTECTED_PRESERVATION_SCOPE:
            raise ValueError("protected preservation proof scope is unsupported")
        return cls(
            require_count(raw["selected_identity_count"], "protected selected identity count"),
            require_digest(raw["selected_identity_digest"], "protected selected identity digest"),
            require_count(raw["relationship_count"], "protected relationship count"),
            require_digest(raw["relationship_digest"], "protected relationship digest"),
            require_count(raw["source_endpoint_count"], "protected source endpoint count"),
            require_digest(raw["source_endpoint_digest"], "protected source endpoint digest"),
        )


@dataclass(frozen=True)
class AuthorizedCompanionRelationship:
    """Safe exact evidence for one cleanup-authorized call-to-activity parent edge."""

    relationship_element_id: str
    relationship_type: Literal["CHILD_OF", "DETAILS_HISTORY_ITEM"]
    call_source_record_pk: str
    activity_source_record_pk: str
    call_direction: Literal["outbound"]
    activity_direction: Literal["inbound"]

    def __post_init__(self) -> None:
        require_identifier(self.relationship_element_id, "authorized companion relationship")
        require_identifier(self.call_source_record_pk, "authorized companion call")
        require_identifier(self.activity_source_record_pk, "authorized companion activity")
        if self.call_source_record_pk == self.activity_source_record_pk:
            raise ValueError("authorized companion endpoints must differ")
        if self.relationship_type not in {"CHILD_OF", "DETAILS_HISTORY_ITEM"}:
            raise ValueError("authorized companion relationship type is invalid")
        if self.call_direction != "outbound" or self.activity_direction != "inbound":
            raise ValueError("authorized companion relationship direction is invalid")

    def key(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.relationship_element_id,
            self.relationship_type,
            self.call_source_record_pk,
            self.activity_source_record_pk,
            self.call_direction,
            self.activity_direction,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "relationship_element_id": self.relationship_element_id,
            "relationship_type": self.relationship_type,
            "call_source_record_pk": self.call_source_record_pk,
            "activity_source_record_pk": self.activity_source_record_pk,
            "call_direction": self.call_direction,
            "activity_direction": self.activity_direction,
        }

    @classmethod
    def parse(cls, value: object) -> AuthorizedCompanionRelationship:
        raw = require_mapping(value, "authorized companion relationship")
        exact_keys(
            raw,
            frozenset(
                {
                    "relationship_element_id",
                    "relationship_type",
                    "call_source_record_pk",
                    "activity_source_record_pk",
                    "call_direction",
                    "activity_direction",
                }
            ),
            "authorized companion relationship",
        )
        relationship_type = raw["relationship_type"]
        if relationship_type not in {"CHILD_OF", "DETAILS_HISTORY_ITEM"}:
            raise ValueError("authorized companion relationship type is invalid")
        call_direction = raw["call_direction"]
        activity_direction = raw["activity_direction"]
        if call_direction != "outbound" or activity_direction != "inbound":
            raise ValueError("authorized companion relationship direction is invalid")
        return cls(
            require_identifier(raw["relationship_element_id"], "authorized companion relationship"),
            cast(Literal["CHILD_OF", "DETAILS_HISTORY_ITEM"], relationship_type),
            require_identifier(raw["call_source_record_pk"], "authorized companion call"),
            require_identifier(raw["activity_source_record_pk"], "authorized companion activity"),
            call_direction,
            activity_direction,
        )


@dataclass(frozen=True)
class CleanupAuthorization:
    checkpoint_id: str
    accepted_run_id: str
    logical_snapshot_id: str
    manifest_digest: str
    boundary_digest: str
    cleanup_identity_digest: str
    archive_connection_fingerprint: str

    def __post_init__(self) -> None:
        validate_snapshot_id(self.checkpoint_id)
        validate_snapshot_id(self.logical_snapshot_id)
        require_identifier(self.accepted_run_id, "accepted_run_id")
        require_identifier(self.archive_connection_fingerprint, "archive_connection_fingerprint")
        for value, field in (
            (self.manifest_digest, "manifest_digest"),
            (self.boundary_digest, "boundary_digest"),
            (self.cleanup_identity_digest, "cleanup_identity_digest"),
        ):
            require_digest(value, field)

    def as_dict(self) -> dict[str, object]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "accepted_run_id": self.accepted_run_id,
            "logical_snapshot_id": self.logical_snapshot_id,
            "manifest_digest": self.manifest_digest,
            "boundary_digest": self.boundary_digest,
            "cleanup_identity_digest": self.cleanup_identity_digest,
            "archive_connection_fingerprint": self.archive_connection_fingerprint,
        }

    @classmethod
    def parse(cls, value: object) -> CleanupAuthorization:
        raw = require_mapping(value, "cleanup authorization")
        expected = frozenset(
            {
                "checkpoint_id",
                "accepted_run_id",
                "logical_snapshot_id",
                "manifest_digest",
                "boundary_digest",
                "cleanup_identity_digest",
                "archive_connection_fingerprint",
            }
        )
        exact_keys(raw, expected, "cleanup authorization")
        return cls(
            require_identifier(raw["checkpoint_id"], "checkpoint_id"),
            require_identifier(raw["accepted_run_id"], "accepted_run_id"),
            require_identifier(raw["logical_snapshot_id"], "logical_snapshot_id"),
            require_digest(raw["manifest_digest"], "manifest_digest"),
            require_digest(raw["boundary_digest"], "boundary_digest"),
            require_digest(raw["cleanup_identity_digest"], "cleanup_identity_digest"),
            require_identifier(
                raw["archive_connection_fingerprint"], "archive_connection_fingerprint"
            ),
        )


@dataclass(frozen=True)
class CleanupTarget:
    configured_environment_id: str
    supplied_environment_id: str
    supplied_database_identity: str
    observed_database_identity: str

    def __post_init__(self) -> None:
        for value, field in (
            (self.configured_environment_id, "configured_environment_id"),
            (self.supplied_environment_id, "supplied_environment_id"),
            (self.supplied_database_identity, "supplied_database_identity"),
            (self.observed_database_identity, "observed_database_identity"),
        ):
            require_identifier(value, field)
        if self.configured_environment_id != self.supplied_environment_id:
            raise ValueError("configured and supplied environment identities differ")
        if self.supplied_database_identity != self.observed_database_identity:
            raise ValueError("supplied and observed database identities differ")

    def as_dict(self) -> dict[str, object]:
        return {
            "configured_environment_id": self.configured_environment_id,
            "supplied_environment_id": self.supplied_environment_id,
            "supplied_database_identity": self.supplied_database_identity,
            "observed_database_identity": self.observed_database_identity,
        }

    @classmethod
    def parse(cls, value: object) -> CleanupTarget:
        raw = require_mapping(value, "cleanup target")
        expected = frozenset(
            {
                "configured_environment_id",
                "supplied_environment_id",
                "supplied_database_identity",
                "observed_database_identity",
            }
        )
        exact_keys(raw, expected, "cleanup target")
        return cls(
            require_identifier(raw["configured_environment_id"], "configured_environment_id"),
            require_identifier(raw["supplied_environment_id"], "supplied_environment_id"),
            require_identifier(raw["supplied_database_identity"], "supplied_database_identity"),
            require_identifier(raw["observed_database_identity"], "observed_database_identity"),
        )


@dataclass(frozen=True)
class QuiescenceEvidence:
    """One State-published proof that the archive's source writers are retired and quiescent."""

    quiescence_run_id: str
    accepted_run_id: str
    checkpoint_id: str
    logical_snapshot_id: str
    manifest_digest: str
    cleanup_identity_digest: str
    source_key: str
    source_instance_id: str
    environment_id: str
    observed_database_identity: str
    boundary_digest: str
    writer_retired: bool
    writers_quiescent: bool
    evidence_digest: str
    attestation_operator: str
    attestation_reference: str

    def __post_init__(self) -> None:
        for value, field in (
            (self.quiescence_run_id, "quiescence_run_id"),
            (self.accepted_run_id, "quiescence accepted_run_id"),
            (self.checkpoint_id, "quiescence checkpoint_id"),
            (self.logical_snapshot_id, "quiescence logical_snapshot_id"),
            (self.source_key, "quiescence source_key"),
            (self.source_instance_id, "quiescence source_instance_id"),
            (self.environment_id, "quiescence environment_id"),
            (self.observed_database_identity, "quiescence observed_database_identity"),
            (self.attestation_operator, "quiescence attestation_operator"),
            (self.attestation_reference, "quiescence attestation_reference"),
        ):
            require_identifier(value, field)
        for value, field in (
            (self.manifest_digest, "quiescence manifest_digest"),
            (self.cleanup_identity_digest, "quiescence cleanup_identity_digest"),
            (self.boundary_digest, "quiescence boundary_digest"),
            (self.evidence_digest, "quiescence evidence_digest"),
        ):
            require_digest(value, field)
        require_true(self.writer_retired, "quiescence writer_retired")
        require_true(self.writers_quiescent, "quiescence writers_quiescent")
        if self.evidence_digest != canonical_digest(self.unsigned_dict()):
            raise ValueError("quiescence evidence digest is invalid")

    @property
    def identity_digest(self) -> str:
        """Stable authorization identity separately bindable by a cleanup receipt."""
        return canonical_digest(
            {
                "accepted_run_id": self.accepted_run_id,
                "checkpoint_id": self.checkpoint_id,
                "logical_snapshot_id": self.logical_snapshot_id,
                "manifest_digest": self.manifest_digest,
                "cleanup_identity_digest": self.cleanup_identity_digest,
                "source_key": self.source_key,
                "source_instance_id": self.source_instance_id,
                "environment_id": self.environment_id,
                "observed_database_identity": self.observed_database_identity,
                "boundary_digest": self.boundary_digest,
                "writer_retired": self.writer_retired,
                "writers_quiescent": self.writers_quiescent,
                "attestation_operator": self.attestation_operator,
                "attestation_reference": self.attestation_reference,
            }
        )

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_version": QUIESCENCE_EVIDENCE_SCHEMA,
            "quiescence_run_id": self.quiescence_run_id,
            "accepted_run_id": self.accepted_run_id,
            "checkpoint_id": self.checkpoint_id,
            "logical_snapshot_id": self.logical_snapshot_id,
            "manifest_digest": self.manifest_digest,
            "cleanup_identity_digest": self.cleanup_identity_digest,
            "source_key": self.source_key,
            "source_instance_id": self.source_instance_id,
            "environment_id": self.environment_id,
            "observed_database_identity": self.observed_database_identity,
            "boundary_digest": self.boundary_digest,
            "writer_retired": self.writer_retired,
            "writers_quiescent": self.writers_quiescent,
            "attestation_operator": self.attestation_operator,
            "attestation_reference": self.attestation_reference,
        }

    def as_dict(self) -> dict[str, object]:
        value = self.unsigned_dict()
        value["evidence_digest"] = self.evidence_digest
        return value

    def require_matches(self, authorization: CleanupAuthorization, target: CleanupTarget) -> None:
        """Fail closed unless this evidence describes exactly the admitted cleanup target."""
        if (
            self.accepted_run_id != authorization.accepted_run_id
            or self.checkpoint_id != authorization.checkpoint_id
            or self.logical_snapshot_id != authorization.logical_snapshot_id
            or self.manifest_digest != authorization.manifest_digest
            or self.cleanup_identity_digest != authorization.cleanup_identity_digest
            or self.boundary_digest != authorization.boundary_digest
            or self.environment_id != target.configured_environment_id
            or self.observed_database_identity != target.observed_database_identity
        ):
            raise ValueError("quiescence evidence does not bind cleanup authorization and target")

    @classmethod
    def create(
        cls,
        quiescence_run_id: str,
        accepted_run_id: str,
        checkpoint_id: str,
        logical_snapshot_id: str,
        manifest_digest: str,
        cleanup_identity_digest: str,
        source_key: str,
        source_instance_id: str,
        environment_id: str,
        observed_database_identity: str,
        boundary_digest: str,
        attestation_operator: str,
        attestation_reference: str,
    ) -> QuiescenceEvidence:
        unsigned = {
            "schema_version": QUIESCENCE_EVIDENCE_SCHEMA,
            "quiescence_run_id": quiescence_run_id,
            "accepted_run_id": accepted_run_id,
            "checkpoint_id": checkpoint_id,
            "logical_snapshot_id": logical_snapshot_id,
            "manifest_digest": manifest_digest,
            "cleanup_identity_digest": cleanup_identity_digest,
            "source_key": source_key,
            "source_instance_id": source_instance_id,
            "environment_id": environment_id,
            "observed_database_identity": observed_database_identity,
            "boundary_digest": boundary_digest,
            "writer_retired": True,
            "writers_quiescent": True,
            "attestation_operator": attestation_operator,
            "attestation_reference": attestation_reference,
        }
        return cls(
            quiescence_run_id,
            accepted_run_id,
            checkpoint_id,
            logical_snapshot_id,
            manifest_digest,
            cleanup_identity_digest,
            source_key,
            source_instance_id,
            environment_id,
            observed_database_identity,
            boundary_digest,
            True,
            True,
            canonical_digest(unsigned),
            attestation_operator,
            attestation_reference,
        )

    @classmethod
    def parse(cls, value: object) -> QuiescenceEvidence:
        raw = require_mapping(value, "quiescence evidence")
        exact_keys(
            raw,
            frozenset(
                {
                    "schema_version",
                    "quiescence_run_id",
                    "accepted_run_id",
                    "checkpoint_id",
                    "logical_snapshot_id",
                    "manifest_digest",
                    "cleanup_identity_digest",
                    "source_key",
                    "source_instance_id",
                    "environment_id",
                    "observed_database_identity",
                    "boundary_digest",
                    "writer_retired",
                    "writers_quiescent",
                    "evidence_digest",
                    "attestation_operator",
                    "attestation_reference",
                }
            ),
            "quiescence evidence",
        )
        if raw["schema_version"] != QUIESCENCE_EVIDENCE_SCHEMA:
            raise ValueError("quiescence evidence schema is unsupported")
        return cls(
            require_identifier(raw["quiescence_run_id"], "quiescence_run_id"),
            require_identifier(raw["accepted_run_id"], "quiescence accepted_run_id"),
            require_identifier(raw["checkpoint_id"], "quiescence checkpoint_id"),
            require_identifier(raw["logical_snapshot_id"], "quiescence logical_snapshot_id"),
            require_digest(raw["manifest_digest"], "quiescence manifest_digest"),
            require_digest(raw["cleanup_identity_digest"], "quiescence cleanup_identity_digest"),
            require_identifier(raw["source_key"], "quiescence source_key"),
            require_identifier(raw["source_instance_id"], "quiescence source_instance_id"),
            require_identifier(raw["environment_id"], "quiescence environment_id"),
            require_identifier(
                raw["observed_database_identity"], "quiescence observed_database_identity"
            ),
            require_digest(raw["boundary_digest"], "quiescence boundary_digest"),
            require_true(raw["writer_retired"], "quiescence writer_retired"),
            require_true(raw["writers_quiescent"], "quiescence writers_quiescent"),
            require_digest(raw["evidence_digest"], "quiescence evidence_digest"),
            require_identifier(raw["attestation_operator"], "quiescence attestation_operator"),
            require_identifier(raw["attestation_reference"], "quiescence attestation_reference"),
        )


@dataclass(frozen=True)
class ProtectedSourceEndpointEvidence:
    """Exact SourceSystem endpoint intentionally preserved while one FROM_SOURCE edge is removed."""

    selected_source_record_pk: str
    relationship_element_id: str
    relationship_type: Literal["FROM_SOURCE"]
    direction: Literal["outbound"]
    endpoint_element_id: str
    endpoint_labels: tuple[str, ...]
    endpoint_source_key: str

    def __post_init__(self) -> None:
        for value, field in (
            (self.selected_source_record_pk, "protected endpoint selected source_record_pk"),
            (self.relationship_element_id, "protected endpoint relationship_element_id"),
            (self.endpoint_element_id, "protected endpoint element_id"),
            (self.endpoint_source_key, "protected endpoint source_key"),
        ):
            require_identifier(value, field)
        if self.relationship_type != "FROM_SOURCE" or self.direction != "outbound":
            raise ValueError("protected source endpoint relationship is invalid")
        if (
            "SourceSystem" not in self.endpoint_labels
            or tuple(sorted(set(self.endpoint_labels))) != self.endpoint_labels
        ):
            raise ValueError("protected source endpoint labels are invalid")
        for label in self.endpoint_labels:
            require_identifier(label, "protected endpoint label")

    def key(self) -> tuple[str, str, str, str, str, tuple[str, ...], str]:
        return (
            self.selected_source_record_pk,
            self.relationship_element_id,
            self.relationship_type,
            self.direction,
            self.endpoint_element_id,
            self.endpoint_labels,
            self.endpoint_source_key,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "selected_source_record_pk": self.selected_source_record_pk,
            "relationship_element_id": self.relationship_element_id,
            "relationship_type": self.relationship_type,
            "direction": self.direction,
            "endpoint_element_id": self.endpoint_element_id,
            "endpoint_labels": list(self.endpoint_labels),
            "endpoint_source_key": self.endpoint_source_key,
        }

    @classmethod
    def parse(cls, value: object) -> ProtectedSourceEndpointEvidence:
        raw = require_mapping(value, "protected source endpoint")
        exact_keys(
            raw,
            frozenset(
                {
                    "selected_source_record_pk",
                    "relationship_element_id",
                    "relationship_type",
                    "direction",
                    "endpoint_element_id",
                    "endpoint_labels",
                    "endpoint_source_key",
                }
            ),
            "protected source endpoint",
        )
        labels = raw["endpoint_labels"]
        if not isinstance(labels, list) or any(not isinstance(item, str) for item in labels):
            raise ValueError("protected source endpoint labels are invalid")
        if raw["relationship_type"] != "FROM_SOURCE" or raw["direction"] != "outbound":
            raise ValueError("protected source endpoint relationship is invalid")
        return cls(
            require_identifier(
                raw["selected_source_record_pk"], "protected endpoint selected source_record_pk"
            ),
            require_identifier(
                raw["relationship_element_id"], "protected endpoint relationship_element_id"
            ),
            "FROM_SOURCE",
            "outbound",
            require_identifier(raw["endpoint_element_id"], "protected endpoint element_id"),
            tuple(labels),
            require_identifier(raw["endpoint_source_key"], "protected endpoint source_key"),
        )


@dataclass(frozen=True)
class ResourceCeilings:
    max_checkpoint_bytes: int
    max_checkpoint_entries: int
    max_batches: int
    max_batch_size: int

    def __post_init__(self) -> None:
        for value, field in (
            (self.max_checkpoint_bytes, "max_checkpoint_bytes"),
            (self.max_checkpoint_entries, "max_checkpoint_entries"),
            (self.max_batches, "max_batches"),
            (self.max_batch_size, "max_batch_size"),
        ):
            if require_count(value, field, 1_000_000_000) < 1:
                raise ValueError(f"{field} must be positive")

    def as_dict(self) -> dict[str, object]:
        return {
            "max_checkpoint_bytes": self.max_checkpoint_bytes,
            "max_checkpoint_entries": self.max_checkpoint_entries,
            "max_batches": self.max_batches,
            "max_batch_size": self.max_batch_size,
        }

    @classmethod
    def parse(cls, value: object) -> ResourceCeilings:
        raw = require_mapping(value, "resource ceilings")
        expected = frozenset(
            {"max_checkpoint_bytes", "max_checkpoint_entries", "max_batches", "max_batch_size"}
        )
        exact_keys(raw, expected, "resource ceilings")
        return cls(
            require_count(raw["max_checkpoint_bytes"], "max_checkpoint_bytes", 1_000_000_000),
            require_count(raw["max_checkpoint_entries"], "max_checkpoint_entries", 1_000_000_000),
            require_count(raw["max_batches"], "max_batches", 1_000_000_000),
            require_count(raw["max_batch_size"], "max_batch_size", 1_000_000_000),
        )


@dataclass(frozen=True)
class CleanupIdentity:
    source_record_pk: str
    record_type: Literal["crm_history", "call"]
    source_instance_id: str
    source_record_version: str
    record_hash: str
    accepted_record_digest: str
    reference_fingerprint: str
    incident_relationship_count: int
    incident_relationship_digest: str
    dependency_count: int
    dependency_digest: str

    def __post_init__(self) -> None:
        for value, field in (
            (self.source_record_pk, "source_record_pk"),
            (self.source_instance_id, "source_instance_id"),
            (self.source_record_version, "source_record_version"),
            (self.record_hash, "record_hash"),
        ):
            require_identifier(value, field)
        if self.record_type not in {"crm_history", "call"}:
            raise ValueError("record_type is invalid")
        for value, field in (
            (self.accepted_record_digest, "accepted_record_digest"),
            (self.reference_fingerprint, "reference_fingerprint"),
            (self.incident_relationship_digest, "incident_relationship_digest"),
            (self.dependency_digest, "dependency_digest"),
        ):
            require_digest(value, field)
        require_count(self.incident_relationship_count, "incident_relationship_count")
        require_count(self.dependency_count, "dependency_count")

    def as_dict(self) -> dict[str, object]:
        return {
            "source_record_pk": self.source_record_pk,
            "record_type": self.record_type,
            "source_instance_id": self.source_instance_id,
            "source_record_version": self.source_record_version,
            "record_hash": self.record_hash,
            "accepted_record_digest": self.accepted_record_digest,
            "reference_fingerprint": self.reference_fingerprint,
            "incident_relationship_count": self.incident_relationship_count,
            "incident_relationship_digest": self.incident_relationship_digest,
            "dependency_count": self.dependency_count,
            "dependency_digest": self.dependency_digest,
        }

    @classmethod
    def parse(cls, value: object) -> CleanupIdentity:
        raw = require_mapping(value, "cleanup identity")
        expected = frozenset(
            {
                "source_record_pk",
                "record_type",
                "source_instance_id",
                "source_record_version",
                "record_hash",
                "accepted_record_digest",
                "reference_fingerprint",
                "incident_relationship_count",
                "incident_relationship_digest",
                "dependency_count",
                "dependency_digest",
            }
        )
        exact_keys(raw, expected, "cleanup identity")
        record_type = raw["record_type"]
        if record_type not in {"crm_history", "call"}:
            raise ValueError("record_type is invalid")
        return cls(
            require_identifier(raw["source_record_pk"], "source_record_pk"),
            cast(Literal["crm_history", "call"], record_type),
            require_identifier(raw["source_instance_id"], "source_instance_id"),
            require_identifier(raw["source_record_version"], "source_record_version"),
            require_identifier(raw["record_hash"], "record_hash"),
            require_digest(raw["accepted_record_digest"], "accepted_record_digest"),
            require_digest(raw["reference_fingerprint"], "reference_fingerprint"),
            require_count(raw["incident_relationship_count"], "incident_relationship_count"),
            require_digest(raw["incident_relationship_digest"], "incident_relationship_digest"),
            require_count(raw["dependency_count"], "dependency_count"),
            require_digest(raw["dependency_digest"], "dependency_digest"),
        )
