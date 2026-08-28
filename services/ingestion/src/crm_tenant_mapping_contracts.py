"""Immutable tenant mapping contracts for standalone Bitrix CRM projections."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from src.models import JsonValue
from src.source_instances import effective_control_instance_id, effective_source_instance_id
from src.standalone_crm_census_types import _integer, _text, _utc

CRM_TENANT_MAPPING_MANIFEST_VERSION = "crm-tenant-mapping-manifest-v1"
CRM_TENANT_MAPPING_OMISSION_POLICY = "omitted_company_maps_to_empty_v1"
CRM_TENANT_MAPPING_RELATIONSHIP_KIND: Literal["tenant_member"] = "tenant_member"

type CrmTenantRelationshipKind = Literal["tenant_member"]
type CrmTenantMappingRevisionState = Literal[
    "prepared", "active", "superseded", "rejected", "activation_failed"
]

_REVISION_STATES = frozenset({"prepared", "active", "superseded", "rejected", "activation_failed"})
_MAX_AUTHORIZATION_ACTOR_LENGTH = 256
_MAX_AUTHORIZATION_REFERENCE_LENGTH = 512


@dataclass(frozen=True)
class CrmTenantMappingScope:
    """Canonical source/control scope for one independent mapping history."""

    source_key: str
    source_instance_id: str
    control_instance_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_key", _text(self.source_key, "source_key"))
        if self.source_key != "bitrix_chat":
            raise ValueError("source_key must be bitrix_chat")
        if effective_source_instance_id(self.source_instance_id) != self.source_instance_id:
            raise ValueError("source_instance_id must be canonical")
        if effective_control_instance_id(self.control_instance_id) != self.control_instance_id:
            raise ValueError("control_instance_id must be canonical")


@dataclass(frozen=True, order=True)
class CrmTenantMappingTarget:
    """An existing Entity reference; it cannot create or rename Entity or Person."""

    entity_key: str
    relationship_kind: CrmTenantRelationshipKind = CRM_TENANT_MAPPING_RELATIONSHIP_KIND

    def __post_init__(self) -> None:
        object.__setattr__(self, "entity_key", _text(self.entity_key, "entity_key"))
        if self.relationship_kind != CRM_TENANT_MAPPING_RELATIONSHIP_KIND:
            raise ValueError("relationship_kind must be tenant_member")


@dataclass(frozen=True)
class CrmTenantMappingCompanyEntry:
    """One auditable company mapping entry, including an explicit empty target tuple."""

    company_id: str
    targets: tuple[CrmTenantMappingTarget, ...]

    def __post_init__(self) -> None:
        company_id = _positive_decimal(self.company_id, "company_id")
        if company_id != self.company_id:
            raise ValueError("company_id must be canonical positive decimal")
        if not isinstance(self.targets, tuple):
            raise ValueError("targets must be an immutable tuple")
        if any(not isinstance(target, CrmTenantMappingTarget) for target in self.targets):
            raise ValueError("targets must contain mapping targets")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("targets must be unique")
        if tuple(sorted(self.targets)) != self.targets:
            raise ValueError("targets must use canonical order")


@dataclass(frozen=True)
class CrmTenantMappingEntry:
    """Persistence-facing mapping entry identity under one immutable revision."""

    revision_id: str
    company_entry: CrmTenantMappingCompanyEntry

    def __post_init__(self) -> None:
        object.__setattr__(self, "revision_id", _text(self.revision_id, "revision_id"))
        if not isinstance(self.company_entry, CrmTenantMappingCompanyEntry):
            raise ValueError("company_entry must be a canonical mapping company entry")

    @property
    def company_id(self) -> str:
        return self.company_entry.company_id

    @property
    def entry_id(self) -> str:
        return _mapping_digest("crm-tenant-mapping-entry-v1", [self.revision_id, self.company_id])


@dataclass(frozen=True)
class CrmTenantMappingEntryTarget:
    """Persistence-facing mapping target identity under one mapping entry."""

    entry: CrmTenantMappingEntry
    target: CrmTenantMappingTarget

    def __post_init__(self) -> None:
        if not isinstance(self.entry, CrmTenantMappingEntry):
            raise ValueError("entry must be a persistence-facing mapping entry")
        if not isinstance(self.target, CrmTenantMappingTarget):
            raise ValueError("target must be a canonical mapping target")
        if self.target not in self.entry.company_entry.targets:
            raise ValueError("target must belong to its mapping entry company entry")

    @property
    def entry_id(self) -> str:
        return self.entry.entry_id

    @property
    def entity_key(self) -> str:
        return self.target.entity_key

    @property
    def relationship_kind(self) -> CrmTenantRelationshipKind:
        return self.target.relationship_kind

    @property
    def target_id(self) -> str:
        return _mapping_digest(
            "crm-tenant-mapping-entry-target-v1",
            [self.entry_id, self.entity_key, self.relationship_kind],
        )


@dataclass(frozen=True)
class CrmTenantMappingManifest:
    """Complete manifest: omission maps to empty; explicit emptiness stays auditable."""

    scope: CrmTenantMappingScope
    entries: tuple[CrmTenantMappingCompanyEntry, ...]
    contract_version: str = CRM_TENANT_MAPPING_MANIFEST_VERSION
    omission_policy: str = CRM_TENANT_MAPPING_OMISSION_POLICY

    def __post_init__(self) -> None:
        if not isinstance(self.scope, CrmTenantMappingScope):
            raise ValueError("scope must be a canonical mapping scope")
        if self.contract_version != CRM_TENANT_MAPPING_MANIFEST_VERSION:
            raise ValueError("unsupported mapping manifest contract_version")
        if self.omission_policy != CRM_TENANT_MAPPING_OMISSION_POLICY:
            raise ValueError("unsupported mapping omission_policy")
        if not isinstance(self.entries, tuple):
            raise ValueError("entries must be an immutable tuple")
        if any(not isinstance(entry, CrmTenantMappingCompanyEntry) for entry in self.entries):
            raise ValueError("entries must contain mapping company entries")
        if len({entry.company_id for entry in self.entries}) != len(self.entries):
            raise ValueError("mapping company entries must have unique company_id values")
        if tuple(sorted(self.entries, key=lambda entry: int(entry.company_id))) != self.entries:
            raise ValueError("mapping company entries must use canonical order")

    @property
    def digest(self) -> str:
        """Return a domain-separated digest of the complete canonical manifest."""
        payload = {
            "contract_version": self.contract_version,
            "omission_policy": self.omission_policy,
            "scope": {
                "source_key": self.scope.source_key,
                "source_instance_id": self.scope.source_instance_id,
                "control_instance_id": self.scope.control_instance_id,
            },
            "entries": [
                {
                    "company_id": entry.company_id,
                    "targets": [
                        {
                            "entity_key": target.entity_key,
                            "relationship_kind": target.relationship_kind,
                        }
                        for target in entry.targets
                    ],
                }
                for entry in self.entries
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        domain = CRM_TENANT_MAPPING_MANIFEST_VERSION.encode("ascii") + bytes((0,))
        return "sha256:" + hashlib.sha256(domain + encoded.encode()).hexdigest()

    def targets_for(self, company_id: str) -> tuple[CrmTenantMappingTarget, ...]:
        """Look up targets; an omitted company is defined to map to an empty tuple."""
        canonical_company_id = _positive_decimal(company_id, "company_id")
        for entry in self.entries:
            if entry.company_id == canonical_company_id:
                return entry.targets
        return ()


@dataclass(frozen=True)
class CrmTenantMappingAuthorization:
    """Bounded non-secret preparation authority safe for child-safe payloads."""

    actor: str
    authorization_reference: str
    authorization_digest: str
    authorized_at: str
    expires_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "actor", _bounded_text(self.actor, "actor", _MAX_AUTHORIZATION_ACTOR_LENGTH)
        )
        object.__setattr__(
            self,
            "authorization_reference",
            _bounded_text(
                self.authorization_reference,
                "authorization_reference",
                _MAX_AUTHORIZATION_REFERENCE_LENGTH,
            ),
        )
        _require_sha256(self.authorization_digest, "authorization_digest")
        object.__setattr__(self, "authorized_at", _utc(self.authorized_at, "authorized_at"))
        object.__setattr__(self, "expires_at", _utc(self.expires_at, "expires_at"))
        if _instant(self.authorized_at) > _instant(self.expires_at):
            raise ValueError("authorized_at cannot be after expires_at")


@dataclass(frozen=True)
class CrmTenantMappingRollbackProvenance:
    """Optional immutable provenance identifying the prior mapping revision."""

    rollback_of_revision_id: str
    rollback_of_revision_number: int
    rollback_of_manifest_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "rollback_of_revision_id",
            _text(self.rollback_of_revision_id, "rollback_of_revision_id"),
        )
        _integer(self.rollback_of_revision_number, "rollback_of_revision_number", 1)
        _require_sha256(self.rollback_of_manifest_digest, "rollback_of_manifest_digest")


@dataclass(frozen=True)
class CrmTenantMappingRevision:
    """Immutable revision record; this contract does not implement transitions."""

    scope: CrmTenantMappingScope
    revision_id: str
    revision_number: int
    manifest_digest: str
    company_entry_count: int
    target_count: int
    preparation_request_id: str
    authorization: CrmTenantMappingAuthorization
    state: CrmTenantMappingRevisionState
    rollback_provenance: CrmTenantMappingRollbackProvenance | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, CrmTenantMappingScope):
            raise ValueError("scope must be a canonical mapping scope")
        object.__setattr__(self, "revision_id", _text(self.revision_id, "revision_id"))
        object.__setattr__(
            self,
            "preparation_request_id",
            _text(self.preparation_request_id, "preparation_request_id"),
        )
        _integer(self.revision_number, "revision_number", 1)
        _require_sha256(self.manifest_digest, "manifest_digest")
        _integer(self.company_entry_count, "company_entry_count")
        _integer(self.target_count, "target_count")
        if not isinstance(self.authorization, CrmTenantMappingAuthorization):
            raise ValueError("authorization must be bounded mapping authorization")
        if self.state not in _REVISION_STATES:
            raise ValueError("invalid mapping revision state")
        if self.rollback_provenance is not None and not isinstance(
            self.rollback_provenance, CrmTenantMappingRollbackProvenance
        ):
            raise ValueError("rollback_provenance must be mapping rollback provenance or None")


@dataclass(frozen=True)
class CrmTenantMappingExpectedHead:
    """Exact optional predecessor identity for an active-head compare-and-swap."""

    head_id: str
    active_revision_id: str
    active_revision_number: int
    active_manifest_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "head_id", _text(self.head_id, "expected head_id"))
        object.__setattr__(
            self,
            "active_revision_id",
            _text(self.active_revision_id, "expected active_revision_id"),
        )
        _integer(self.active_revision_number, "expected active_revision_number", 1)
        _require_sha256(self.active_manifest_digest, "expected active_manifest_digest")


@dataclass(frozen=True)
class CrmTenantActiveMappingHead:
    """Forward-only mapping-head CAS contract ordered by revision number."""

    scope: CrmTenantMappingScope
    head_id: str
    active_revision_id: str
    active_revision_number: int
    active_manifest_digest: str
    effective_at: str
    expected_head: CrmTenantMappingExpectedHead | None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, CrmTenantMappingScope):
            raise ValueError("scope must be a canonical mapping scope")
        object.__setattr__(self, "head_id", _text(self.head_id, "head_id"))
        object.__setattr__(
            self,
            "active_revision_id",
            _text(self.active_revision_id, "active_revision_id"),
        )
        _integer(self.active_revision_number, "active_revision_number", 1)
        _require_sha256(self.active_manifest_digest, "active_manifest_digest")
        object.__setattr__(self, "effective_at", _utc(self.effective_at, "effective_at"))
        if self.expected_head is not None and not isinstance(
            self.expected_head, CrmTenantMappingExpectedHead
        ):
            raise ValueError("expected_head must be a mapping head CAS identity")
        prior_number = (
            self.expected_head.active_revision_number if self.expected_head is not None else None
        )
        if prior_number is not None and self.active_revision_number <= prior_number:
            raise ValueError("active mapping revision_number must advance expected head")


type CrmTenantMappingActiveHead = CrmTenantActiveMappingHead


def _positive_decimal(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip().isdigit():
        raise ValueError(f"{field} must be a positive decimal")
    parsed = int(value.strip())
    if parsed < 1:
        raise ValueError(f"{field} must be a positive decimal")
    return str(parsed)


def _bounded_text(value: str, field: str, maximum_length: int) -> str:
    text = _text(value, field)
    if len(text) > maximum_length:
        raise ValueError(f"{field} exceeds its bounded maximum length")
    return text


def _require_sha256(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"{field} must be a canonical sha256 digest")


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _mapping_digest(namespace: str, payload: list[JsonValue]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return (
        "sha256:"
        + hashlib.sha256(namespace.encode("utf-8") + b"\x00" + encoded.encode("utf-8")).hexdigest()
    )
