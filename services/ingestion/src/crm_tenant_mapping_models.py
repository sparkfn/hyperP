"""Typed command and persisted-read models for immutable CRM tenant mappings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.crm_tenant_mapping_contracts import (
    CrmTenantMappingAuthorization,
    CrmTenantMappingEntry,
    CrmTenantMappingEntryTarget,
    CrmTenantMappingExpectedHead,
    CrmTenantMappingManifest,
    CrmTenantMappingRevision,
    CrmTenantMappingRollbackProvenance,
    CrmTenantMappingScope,
)
from src.crm_tenant_mapping_identity import (
    _authorization_payload,
    _boundary_payload,
    _bounded,
    _digest,
    _instant,
    _prepare_payload,
    _rejection_payload,
    _require_sha256,
    _scope_payload,
    _target_keys,
    mapping_head_id,
    mapping_revision_id,  # noqa: F401 - preserved public import for mapping consumers
)
from src.standalone_crm_census_types import _text, _utc

type CrmTenantMappingActionKind = Literal["prepare", "rollback", "reject"]


class CrmTenantMappingError(RuntimeError):
    """Base error for mapping authority operations."""


class CrmTenantMappingConflictError(CrmTenantMappingError):
    """The immutable command or persisted authority boundary conflicts."""


class CrmTenantMappingIntegrityError(CrmTenantMappingError):
    """Persisted mapping rows are malformed, incomplete, or contradictory."""


class CrmTenantMappingAuthorizationError(CrmTenantMappingError):
    """A mapping mutation lacks valid, current authorization."""


@dataclass(frozen=True)
class CrmTenantMappingExpectedHeadBoundary:
    """Full expected active-head snapshot, including generation-zero absence."""

    scope: CrmTenantMappingScope
    head_id: str
    expected_head: CrmTenantMappingExpectedHead | None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, CrmTenantMappingScope):
            raise ValueError("expected head boundary requires a mapping scope")
        if self.head_id != mapping_head_id(self.scope):
            raise ValueError("expected head boundary must use the deterministic scope head_id")
        if self.expected_head is not None:
            if not isinstance(self.expected_head, CrmTenantMappingExpectedHead):
                raise ValueError("expected head boundary must use a mapping expected head")
            if self.expected_head.head_id != self.head_id:
                raise ValueError("expected head boundary head_id conflicts with its predecessor")

    @property
    def is_absent(self) -> bool:
        return self.expected_head is None


@dataclass(frozen=True)
class CrmTenantMappingRejection:
    """Bounded immutable audit metadata for one prepared-revision rejection."""

    actor: str
    rejection_reference: str
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "actor", _bounded(self.actor, "rejection actor", 256))
        object.__setattr__(
            self,
            "rejection_reference",
            _bounded(self.rejection_reference, "rejection reference", 512),
        )
        object.__setattr__(self, "reason", _bounded(self.reason, "rejection reason", 512))


@dataclass(frozen=True)
class CrmTenantMappingPrepareCommand:
    """One requested immutable prepared mapping revision."""

    scope: CrmTenantMappingScope
    preparation_request_id: str
    manifest: CrmTenantMappingManifest
    expected_head_boundary: CrmTenantMappingExpectedHeadBoundary
    authorization: CrmTenantMappingAuthorization
    operation_time: str

    def __post_init__(self) -> None:
        _validate_prepare_common(self)

    @property
    def request_fingerprint(self) -> str:
        return _digest("crm-tenant-mapping-prepare-request-v1", _prepare_payload(self))


@dataclass(frozen=True)
class CrmTenantMappingRollbackCommand:
    """One requested higher prepared revision copying an effective historical mapping."""

    scope: CrmTenantMappingScope
    preparation_request_id: str
    rollback_of_revision_id: str
    rollback_of_manifest_digest: str
    expected_head_boundary: CrmTenantMappingExpectedHeadBoundary
    authorization: CrmTenantMappingAuthorization
    operation_time: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, CrmTenantMappingScope):
            raise ValueError("rollback requires a mapping scope")
        object.__setattr__(
            self,
            "preparation_request_id",
            _text(self.preparation_request_id, "preparation_request_id"),
        )
        object.__setattr__(
            self,
            "rollback_of_revision_id",
            _text(self.rollback_of_revision_id, "rollback_of_revision_id"),
        )
        _require_sha256(self.rollback_of_manifest_digest, "rollback_of_manifest_digest")
        _validate_boundary(self.scope, self.expected_head_boundary)
        if self.expected_head_boundary.expected_head is None:
            raise ValueError("rollback requires a present current active head")
        _validate_authorization_and_time(self.authorization, self.operation_time)

    @property
    def request_fingerprint(self) -> str:
        return _digest(
            "crm-tenant-mapping-rollback-request-v1",
            [
                _scope_payload(self.scope),
                self.preparation_request_id,
                self.rollback_of_revision_id,
                self.rollback_of_manifest_digest,
                _boundary_payload(self.expected_head_boundary),
                _authorization_payload(self.authorization),
            ],
        )


@dataclass(frozen=True)
class CrmTenantMappingRejectCommand:
    """One authorized rejection of exactly one immutable prepared revision."""

    scope: CrmTenantMappingScope
    revision_id: str
    manifest_digest: str
    rejection: CrmTenantMappingRejection
    authorization: CrmTenantMappingAuthorization
    operation_time: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, CrmTenantMappingScope):
            raise ValueError("rejection requires a mapping scope")
        object.__setattr__(self, "revision_id", _text(self.revision_id, "revision_id"))
        _require_sha256(self.manifest_digest, "manifest_digest")
        if not isinstance(self.rejection, CrmTenantMappingRejection):
            raise ValueError("rejection requires bounded rejection metadata")
        _validate_authorization_and_time(self.authorization, self.operation_time)

    @property
    def request_fingerprint(self) -> str:
        return _digest(
            "crm-tenant-mapping-reject-request-v1",
            [
                _scope_payload(self.scope),
                self.revision_id,
                self.manifest_digest,
                _rejection_payload(
                    self.rejection.actor,
                    self.rejection.rejection_reference,
                    self.rejection.reason,
                ),
                _authorization_payload(self.authorization),
            ],
        )


@dataclass(frozen=True)
class CrmTenantMappingRevisionSnapshot:
    """One strict reconstructed immutable revision and its canonical persisted components."""

    revision: CrmTenantMappingRevision
    manifest: CrmTenantMappingManifest
    expected_head_boundary: CrmTenantMappingExpectedHeadBoundary
    entries: tuple[CrmTenantMappingEntry, ...]
    targets: tuple[CrmTenantMappingEntryTarget, ...]
    created_at: str
    request_fingerprint: str
    rejection: CrmTenantMappingRejection | None = None
    rejected_at: str | None = None
    rejection_authorization: CrmTenantMappingAuthorization | None = None
    rejection_request_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.revision, CrmTenantMappingRevision):
            raise ValueError("snapshot requires an immutable mapping revision")
        if not isinstance(self.manifest, CrmTenantMappingManifest):
            raise ValueError("snapshot requires a canonical manifest")
        if self.revision.scope != self.manifest.scope:
            raise ValueError("snapshot revision and manifest scope conflict")
        _validate_boundary(self.revision.scope, self.expected_head_boundary)
        if not isinstance(self.entries, tuple) or not isinstance(self.targets, tuple):
            raise ValueError("snapshot entries and targets must be immutable tuples")
        if any(not isinstance(entry, CrmTenantMappingEntry) for entry in self.entries):
            raise ValueError("snapshot entries are invalid")
        if any(not isinstance(target, CrmTenantMappingEntryTarget) for target in self.targets):
            raise ValueError("snapshot targets are invalid")
        if tuple(entry.company_entry for entry in self.entries) != self.manifest.entries:
            raise ValueError("snapshot entries must exactly reconstruct the manifest")
        expected_targets = tuple(
            CrmTenantMappingEntryTarget(entry, target)
            for entry in self.entries
            for target in entry.company_entry.targets
        )
        if self.targets != expected_targets:
            raise ValueError("snapshot targets must exactly reconstruct the manifest entries")
        if (
            len(self.entries) != self.revision.company_entry_count
            or len(self.targets) != self.revision.target_count
        ):
            raise ValueError("snapshot component counts conflict with revision")
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        _require_sha256(self.request_fingerprint, "request_fingerprint")
        rejection_parts_present = (
            self.rejection,
            self.rejected_at,
            self.rejection_authorization,
            self.rejection_request_fingerprint,
        )
        if any(part is None for part in rejection_parts_present) and any(
            part is not None for part in rejection_parts_present
        ):
            raise ValueError("snapshot rejection metadata must be complete or absent")
        if self.rejected_at is not None:
            object.__setattr__(self, "rejected_at", _utc(self.rejected_at, "rejected_at"))
        if self.rejection_request_fingerprint is not None:
            _require_sha256(self.rejection_request_fingerprint, "rejection_request_fingerprint")
        if self.revision.state == "rejected" and self.rejection is None:
            raise ValueError("rejected snapshot requires rejection metadata")
        if self.revision.state != "rejected" and self.rejection is not None:
            raise ValueError("non-rejected snapshot cannot carry rejection metadata")


@dataclass(frozen=True)
class CrmTenantMappingAuthorizationRequest:
    """Canonical complete action passed to the fail-closed authorization seam."""

    action: CrmTenantMappingActionKind
    scope: CrmTenantMappingScope
    preparation_request_id: str | None
    revision_id: str | None
    manifest_digest: str
    target_entity_keys: tuple[str, ...]
    expected_head_boundary: CrmTenantMappingExpectedHeadBoundary | None
    authorization: CrmTenantMappingAuthorization
    operation_time: str
    rollback_provenance: CrmTenantMappingRollbackProvenance | None = None
    rejection: CrmTenantMappingRejection | None = None

    def __post_init__(self) -> None:
        if self.action not in {"prepare", "rollback", "reject"}:
            raise ValueError("invalid mapping authorization action")
        if not isinstance(self.scope, CrmTenantMappingScope):
            raise ValueError("authorization request requires a mapping scope")
        if self.preparation_request_id is not None:
            object.__setattr__(
                self,
                "preparation_request_id",
                _text(self.preparation_request_id, "preparation_request_id"),
            )
        if self.revision_id is not None:
            object.__setattr__(self, "revision_id", _text(self.revision_id, "revision_id"))
        if (self.action == "reject") != (self.revision_id is not None):
            raise ValueError("only rejection authorization requires an exact revision_id")
        _require_sha256(self.manifest_digest, "manifest_digest")
        if (
            not isinstance(self.target_entity_keys, tuple)
            or tuple(sorted(set(self.target_entity_keys))) != self.target_entity_keys
        ):
            raise ValueError("authorization target entity keys must be unique canonical order")
        if any(_text(key, "target entity_key") != key for key in self.target_entity_keys):
            raise ValueError("authorization target entity keys must be canonical")
        if self.expected_head_boundary is not None:
            _validate_boundary(self.scope, self.expected_head_boundary)
        _validate_authorization_and_time(self.authorization, self.operation_time)
        if self.rollback_provenance is not None and not isinstance(
            self.rollback_provenance, CrmTenantMappingRollbackProvenance
        ):
            raise ValueError("authorization rollback provenance is invalid")
        if self.rejection is not None and not isinstance(self.rejection, CrmTenantMappingRejection):
            raise ValueError("authorization rejection metadata is invalid")


def authorization_request_for_prepare(
    command: CrmTenantMappingPrepareCommand,
) -> CrmTenantMappingAuthorizationRequest:
    return CrmTenantMappingAuthorizationRequest(
        "prepare",
        command.scope,
        command.preparation_request_id,
        None,
        command.manifest.digest,
        _target_keys(command.manifest),
        command.expected_head_boundary,
        command.authorization,
        command.operation_time,
    )


def authorization_request_for_rollback(
    command: CrmTenantMappingRollbackCommand,
    manifest: CrmTenantMappingManifest,
    provenance: CrmTenantMappingRollbackProvenance,
) -> CrmTenantMappingAuthorizationRequest:
    return CrmTenantMappingAuthorizationRequest(
        "rollback",
        command.scope,
        command.preparation_request_id,
        None,
        manifest.digest,
        _target_keys(manifest),
        command.expected_head_boundary,
        command.authorization,
        command.operation_time,
        rollback_provenance=provenance,
    )


def authorization_request_for_rejection(
    command: CrmTenantMappingRejectCommand,
    snapshot: CrmTenantMappingRevisionSnapshot,
) -> CrmTenantMappingAuthorizationRequest:
    if (
        snapshot.revision.scope != command.scope
        or snapshot.revision.revision_id != command.revision_id
        or snapshot.revision.manifest_digest != command.manifest_digest
    ):
        raise ValueError("rejection authorization requires the exact strict mapping revision")
    return CrmTenantMappingAuthorizationRequest(
        "reject",
        command.scope,
        None,
        snapshot.revision.revision_id,
        snapshot.revision.manifest_digest,
        _target_keys(snapshot.manifest),
        None,
        command.authorization,
        command.operation_time,
        rejection=command.rejection,
    )


def authorization_is_current(
    authorization: CrmTenantMappingAuthorization, operation_time: str
) -> bool:
    """Return whether bounded evidence covers the supplied UTC operation instant."""
    instant = _instant(_utc(operation_time, "operation_time"))
    return _instant(authorization.authorized_at) <= instant <= _instant(authorization.expires_at)


def _validate_prepare_common(command: CrmTenantMappingPrepareCommand) -> None:
    if not isinstance(command.scope, CrmTenantMappingScope):
        raise ValueError("preparation requires a mapping scope")
    object.__setattr__(
        command,
        "preparation_request_id",
        _text(command.preparation_request_id, "preparation_request_id"),
    )
    if (
        not isinstance(command.manifest, CrmTenantMappingManifest)
        or command.manifest.scope != command.scope
    ):
        raise ValueError("preparation manifest must use the exact mapping scope")
    _validate_boundary(command.scope, command.expected_head_boundary)
    _validate_authorization_and_time(command.authorization, command.operation_time)


def _validate_boundary(
    scope: CrmTenantMappingScope, boundary: CrmTenantMappingExpectedHeadBoundary
) -> None:
    if not isinstance(boundary, CrmTenantMappingExpectedHeadBoundary) or boundary.scope != scope:
        raise ValueError("expected head boundary must use the exact mapping scope")


def _validate_authorization_and_time(
    authorization: CrmTenantMappingAuthorization, operation_time: str
) -> None:
    if not isinstance(authorization, CrmTenantMappingAuthorization):
        raise ValueError("mapping command requires bounded authorization evidence")
    _utc(operation_time, "operation_time")
