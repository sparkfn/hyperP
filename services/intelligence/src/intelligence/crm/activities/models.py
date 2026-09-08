"""Strict safe value objects for CRM activity archive evidence."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from intelligence.artifacts import canonical_json

DispositionKind = Literal["accepted", "rejected", "quarantined"]
RecordKind = Literal["crm_history", "call"]
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PATH_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
KNOWN_LIFECYCLES = frozenset(
    {"active", "pending_review", "superseded", "rejected", "link_failed", "retired"}
)
PROVENANCE: dict[str, object] = {
    "source_population": "neo4j_existing_records",
    "completeness": "legacy_partial_snapshot",
    "bitrix_completeness_asserted": False,
}


def _id(value: str, field: str) -> str:
    if not _ID.fullmatch(value):
        raise ValueError(f"{field} must be a safe canonical identifier")
    return value


def _text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 1_024:
        raise ValueError(f"{field} must be null or bounded non-empty text")
    return value


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_snapshot_id(value: str) -> str:
    """Validate a snapshot component before using it in any filesystem path."""
    if not _PATH_ID.fullmatch(value):
        raise ValueError("snapshot_id must be a Windows-safe path component")
    return value


@dataclass(frozen=True)
class ArchiveRequest:
    snapshot_id: str
    source_instance_id: str
    source_key: str = "bitrix_chat"
    page_size: int = 100
    max_rows: int = 10_000
    max_pages: int = 200
    database_identity: str = "default"
    selection_contract_version: str = "crm-activities-selection-v2"
    max_references_per_record: int = 100

    def __post_init__(self) -> None:
        validate_snapshot_id(self.snapshot_id)
        _id(self.source_instance_id, "source_instance_id")
        _id(self.source_key, "source_key")
        _id(self.database_identity, "database_identity")
        _id(self.selection_contract_version, "selection_contract_version")
        if not 1 <= self.max_references_per_record <= 10_000:
            raise ValueError("max_references_per_record is outside approved bounds")
        if not 1 <= self.page_size <= 1_000:
            raise ValueError("page_size must be between 1 and 1000")
        if not 1 <= self.max_rows <= 100_000 or not 1 <= self.max_pages <= 10_000:
            raise ValueError("archive limits are outside approved bounds")

    def as_public_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "source_instance_id": self.source_instance_id,
            "source_key": self.source_key,
            "page_size": self.page_size,
            "max_rows": self.max_rows,
            "max_pages": self.max_pages,
            "database_identity": self.database_identity,
            "selection_contract_version": self.selection_contract_version,
            "max_references_per_record": self.max_references_per_record,
        }


@dataclass(frozen=True)
class ParentReference:
    source_record_pk: str | None
    source_instance_id: str | None
    source_record_id: str | None
    record_type: str | None
    relationship: str
    source_system: str | None = None

    def __post_init__(self) -> None:
        optional_values: tuple[tuple[str | None, str], ...] = (
            (self.source_record_pk, "parent source_record_pk"),
            (self.source_instance_id, "parent source_instance_id"),
            (self.source_record_id, "parent source_record_id"),
            (self.record_type, "parent record_type"),
            (self.source_system, "parent source_system"),
        )
        for value, field in optional_values:
            if value is not None:
                _id(value, field)
        _id(self.relationship, "parent relationship")

    def key(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.relationship,
            self.source_record_pk or "",
            self.source_instance_id or "",
            self.source_record_id or "",
            self.record_type or "",
            self.source_system or "",
        )


@dataclass(frozen=True)
class PersonReference:
    person_id: str
    status: str | None
    revision: str | None
    association_source_record_pk: str | None

    def __post_init__(self) -> None:
        _id(self.person_id, "person_id")
        _text(self.status, "person status")
        _text(self.revision, "person revision")
        if self.association_source_record_pk is not None:
            _id(self.association_source_record_pk, "person association source_record_pk")

    def key(self) -> tuple[str, str, str, str]:
        return (
            self.person_id,
            self.status or "",
            self.revision or "",
            self.association_source_record_pk or "",
        )


@dataclass(frozen=True)
class ArchiveRecord:
    """One allowlisted source version; raw payload and graph topology are absent."""

    source_record_pk: str
    source_record_id: str
    source_record_version: str
    source_version_key: str
    record_hash: str
    source_instance_id: str
    source_key: str
    record_type: RecordKind
    lifecycle_status: str | None
    history_family: str | None
    history_kind: str | None
    history_source: str | None
    projection_version: str | None
    projection_source: str | None
    event_at: str | None
    observed_at: str | None
    available_at: str | None
    stored_parent: ParentReference
    child_parents: tuple[ParentReference, ...]
    details_parents: tuple[ParentReference, ...]
    people: tuple[PersonReference, ...]
    user_capabilities: tuple[str, ...]
    ingested_at: str | None = None
    link_status: str | None = None

    def __post_init__(self) -> None:
        for value, field in (
            (self.source_record_pk, "source_record_pk"),
            (self.source_record_id, "source_record_id"),
            (self.source_record_version, "source_record_version"),
            (self.source_version_key, "source_version_key"),
            (self.source_instance_id, "source_instance_id"),
            (self.source_key, "source_key"),
        ):
            _id(value, field)
        if (
            self.record_type not in {"crm_history", "call"}
            or not self.record_hash
            or len(self.record_hash) > 512
        ):
            raise ValueError("archive record is unsupported or missing its source hash")
        optional_values: tuple[tuple[str | None, str], ...] = (
            (self.lifecycle_status, "lifecycle_status"),
            (self.history_family, "history_family"),
            (self.history_kind, "history_kind"),
            (self.history_source, "history_source"),
            (self.projection_version, "projection_version"),
            (self.projection_source, "projection_source"),
            (self.event_at, "event_at"),
            (self.observed_at, "observed_at"),
            (self.available_at, "available_at"),
            (self.ingested_at, "ingested_at"),
            (self.link_status, "link_status"),
        )
        for value, field in optional_values:
            _text(value, field)
        if tuple(sorted(self.child_parents, key=ParentReference.key)) != self.child_parents:
            raise ValueError("child parents must be canonical")
        if tuple(sorted(self.details_parents, key=ParentReference.key)) != self.details_parents:
            raise ValueError("details parents must be canonical")
        if tuple(sorted(self.people, key=PersonReference.key)) != self.people:
            raise ValueError("Person references must be canonical")
        if tuple(sorted(set(self.user_capabilities))) != self.user_capabilities:
            raise ValueError("user capability evidence must be canonical")

    @property
    def known_lifecycle(self) -> bool:
        return self.lifecycle_status in KNOWN_LIFECYCLES

    def reference_fingerprint(self) -> str:
        return sha256_json(
            {
                "stored_parent": _parent_dict(self.stored_parent),
                "child_parents": [_parent_dict(item) for item in self.child_parents],
                "details_parents": [_parent_dict(item) for item in self.details_parents],
                "people": [_person_dict(item) for item in self.people],
                "user_capabilities": list(self.user_capabilities),
            }
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "source_record_pk": self.source_record_pk,
            "source_record_id": self.source_record_id,
            "source_record_version": self.source_record_version,
            "source_version_key": self.source_version_key,
            "record_hash": self.record_hash,
            "source_instance_id": self.source_instance_id,
            "source_key": self.source_key,
            "record_type": self.record_type,
            "lifecycle_status": self.lifecycle_status,
            "history_family": self.history_family,
            "history_kind": self.history_kind,
            "history_source": self.history_source,
            "projection_version": self.projection_version,
            "projection_source": self.projection_source,
            "event_at": self.event_at,
            "observed_at": self.observed_at,
            "available_at": self.available_at,
            "stored_parent": _parent_dict(self.stored_parent),
            "child_parents": [_parent_dict(item) for item in self.child_parents],
            "details_parents": [_parent_dict(item) for item in self.details_parents],
            "people": [_person_dict(item) for item in self.people],
            "user_capabilities": list(self.user_capabilities),
            "ingested_at": self.ingested_at,
            "link_status": self.link_status,
            "reference_fingerprint": self.reference_fingerprint(),
        }

    def digest(self) -> str:
        return sha256_json(self.as_dict())


@dataclass(frozen=True)
class BoundaryEntry:
    source_record_pk: str
    record_type: RecordKind
    record_digest: str
    reference_fingerprint: str

    def __post_init__(self) -> None:
        _id(self.source_record_pk, "boundary source_record_pk")
        if self.record_type not in {"crm_history", "call"}:
            raise ValueError("boundary record type is invalid")
        for value in (self.record_digest, self.reference_fingerprint):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError("boundary digest is invalid")

    @classmethod
    def from_record(cls, record: ArchiveRecord) -> BoundaryEntry:
        return cls(
            record.source_record_pk,
            record.record_type,
            record.digest(),
            record.reference_fingerprint(),
        )


@dataclass(frozen=True)
class SealedBoundary:
    request: ArchiveRequest
    entries: tuple[BoundaryEntry, ...]
    schema_version: str = "crm-activities-boundary-v1"

    def __post_init__(self) -> None:
        if tuple(sorted(self.entries, key=lambda item: item.source_record_pk)) != self.entries:
            raise ValueError("boundary is not ordered by source_record_pk")
        if len({item.source_record_pk for item in self.entries}) != len(self.entries):
            raise ValueError("boundary contains duplicate identities")

    @property
    def digest(self) -> str:
        return sha256_json(
            {
                "schema_version": self.schema_version,
                "source_instance_id": self.request.source_instance_id,
                "source_key": self.request.source_key,
                "database_identity": self.request.database_identity,
                "selection_contract_version": self.request.selection_contract_version,
                "max_references_per_record": self.request.max_references_per_record,
                "entries": [_boundary_entry_dict(item) for item in self.entries],
            }
        )

    @property
    def logical_snapshot_id(self) -> str:
        return f"crm-activities-{self.digest[:24]}"

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "request": self.request.as_public_dict(),
            "entries": [_boundary_entry_dict(item) for item in self.entries],
            "digest": self.digest,
        }


@dataclass(frozen=True)
class Disposition:
    source_record_pk: str
    disposition: DispositionKind
    reason_code: str | None

    def __post_init__(self) -> None:
        _id(self.source_record_pk, "disposition source_record_pk")
        if self.disposition not in {"accepted", "rejected", "quarantined"}:
            raise ValueError("unknown disposition")
        if self.disposition == "accepted" and self.reason_code is not None:
            raise ValueError("accepted disposition cannot carry a reason")
        if self.disposition != "accepted":
            if self.reason_code is None:
                raise ValueError("non-accepted disposition requires a reason")
            _id(self.reason_code, "disposition reason")


def _parent_dict(reference: ParentReference) -> dict[str, str | None]:
    return {
        "source_record_pk": reference.source_record_pk,
        "source_instance_id": reference.source_instance_id,
        "source_record_id": reference.source_record_id,
        "record_type": reference.record_type,
        "relationship": reference.relationship,
        "source_system": reference.source_system,
    }


def _person_dict(reference: PersonReference) -> dict[str, str | None]:
    return {
        "person_id": reference.person_id,
        "status": reference.status,
        "revision": reference.revision,
        "association_source_record_pk": reference.association_source_record_pk,
    }


def _boundary_entry_dict(entry: BoundaryEntry) -> dict[str, str]:
    return {
        "source_record_pk": entry.source_record_pk,
        "record_type": entry.record_type,
        "record_digest": entry.record_digest,
        "reference_fingerprint": entry.reference_fingerprint,
    }
