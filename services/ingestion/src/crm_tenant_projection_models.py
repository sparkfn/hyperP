"""Typed application models for bounded immutable CRM tenant projection materialization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.crm_tenant_mapping_models import CrmTenantMappingExpectedHeadBoundary
from src.crm_tenant_projection_identity import (
    command_fingerprint,
    empty_capture_boundary_digest,
    projection_head_id,
)
from src.crm_tenant_projection_records import (
    CRM_TENANT_PROJECTION_CONTRACT_VERSION,
    CrmTenantProjectionExpectedHead,
    CrmTenantProjectionScope,
    _canonical_text,
    _require_sha256,
)
from src.standalone_crm_census_types import _integer

type CrmTenantProjectionPhase = Literal["capture", "projection", "complete"]
type CrmTenantProjectionCursorKind = Literal["contact", "lead"]
type CrmTenantProjectionFailureCode = Literal[
    "materialization_error", "boundary_conflict", "integrity_error"
]


class CrmTenantProjectionError(RuntimeError):
    """Base materialization error."""


class CrmTenantProjectionConflictError(CrmTenantProjectionError):
    """An immutable command, replay, or authority boundary conflicts."""


class CrmTenantProjectionIntegrityError(CrmTenantProjectionError):
    """Persisted projection topology or immutable values are malformed."""


class CrmTenantProjectionCancelledError(CrmTenantProjectionError):
    """The release was cancelled before it could complete."""


@dataclass(frozen=True)
class CrmTenantProjectionCursor:
    """Exclusive canonical contact-then-lead keyset cursor."""

    subject_kind: CrmTenantProjectionCursorKind
    subject_id: int

    def __post_init__(self) -> None:
        if self.subject_kind not in {"contact", "lead"}:
            raise ValueError("cursor subject_kind must be contact or lead")
        _integer(self.subject_id, "cursor subject_id", 1)


@dataclass(frozen=True)
class CrmTenantProjectionMaterializationCommand:
    """All immutable inputs needed to allocate or replay one projection release."""

    scope: CrmTenantProjectionScope
    request_id: str
    source_census_id: str
    source_census_fingerprint: str
    mapping_revision_id: str
    mapping_manifest_digest: str
    expected_mapping_head_boundary: CrmTenantMappingExpectedHeadBoundary
    expected_prior_head: CrmTenantProjectionExpectedHead | None
    page_limit: int = 100
    contract_version: str = CRM_TENANT_PROJECTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.scope, CrmTenantProjectionScope):
            raise ValueError("materialization requires a projection scope")
        for field in ("request_id", "source_census_id", "mapping_revision_id"):
            object.__setattr__(self, field, _canonical_text(getattr(self, field), field))
        _require_sha256(self.source_census_fingerprint, "source_census_fingerprint")
        _require_sha256(self.mapping_manifest_digest, "mapping_manifest_digest")
        if self.expected_mapping_head_boundary.scope != self.scope.mapping_scope:
            raise ValueError("mapping expected-head boundary must use the exact projection scope")
        if self.expected_prior_head is not None and not isinstance(
            self.expected_prior_head, CrmTenantProjectionExpectedHead
        ):
            raise ValueError("expected_prior_head must be a projection head identity")
        if not 1 <= self.page_limit <= 500:
            raise ValueError("page_limit must be between 1 and 500")
        if self.contract_version != CRM_TENANT_PROJECTION_CONTRACT_VERSION:
            raise ValueError("unsupported projection contract version")

    @property
    def expected_mapping_head_id(self) -> str:
        return self.expected_mapping_head_boundary.head_id

    @property
    def expected_mapping_head_digest(self) -> str:
        head = self.expected_mapping_head_boundary.expected_head
        return "absent" if head is None else head.active_manifest_digest

    @property
    def release_fingerprint(self) -> str:
        """Return the request replay fingerprint before immutable boundaries are resolved."""
        return command_fingerprint(self)

    @property
    def request_fingerprint(self) -> str:
        """Return the immutable request replay fingerprint."""
        return command_fingerprint(self)

    @property
    def projection_head_id(self) -> str:
        return projection_head_id(self.scope)


@dataclass(frozen=True)
class CrmTenantProjectionReleaseSummary:
    """Strict persisted-release summary used for resumable bounded execution."""

    scope: CrmTenantProjectionScope
    release_id: str
    release_number: int
    request_id: str
    release_fingerprint: str
    source_census_id: str
    mapping_revision_id: str
    mapping_manifest_digest: str
    state: Literal["building", "completed", "failed", "cancelled"]
    phase: CrmTenantProjectionPhase
    capture_cursor: CrmTenantProjectionCursor | None
    projection_cursor: CrmTenantProjectionCursor | None
    input_count: int
    decision_count: int
    association_count: int
    support_count: int
    capture_boundary_digest: str = empty_capture_boundary_digest()
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, CrmTenantProjectionScope):
            raise ValueError("release summary requires a projection scope")
        for field in ("release_id", "request_id", "source_census_id", "mapping_revision_id"):
            object.__setattr__(self, field, _canonical_text(getattr(self, field), field))
        _integer(self.release_number, "release_number", 1)
        _require_sha256(self.release_fingerprint, "release_fingerprint")
        _require_sha256(self.mapping_manifest_digest, "mapping_manifest_digest")
        _require_sha256(self.capture_boundary_digest, "capture_boundary_digest")
        if self.state not in {"building", "completed", "failed", "cancelled"}:
            raise ValueError("invalid materialization release state")
        if self.phase not in {"capture", "projection", "complete"}:
            raise ValueError("invalid materialization release phase")
        for field in ("input_count", "decision_count", "association_count", "support_count"):
            _integer(getattr(self, field), field)
        if self.state == "completed" and self.phase != "complete":
            raise ValueError("completed release must have complete phase")
        if self.state == "failed":
            if self.failure_code is None:
                raise ValueError("failed release requires a failure_code")
            object.__setattr__(
                self,
                "failure_code",
                validate_failure_code(_canonical_text(self.failure_code, "failure_code")),
            )
        elif self.failure_code is not None:
            raise ValueError("non-failed release must not have a failure_code")

    @property
    def terminal(self) -> bool:
        return self.state in {"completed", "failed", "cancelled"}


def validate_failure_code(value: str) -> CrmTenantProjectionFailureCode:
    """Allow only bounded non-secret operational failure classifications."""
    if value == "materialization_error":
        return "materialization_error"
    if value == "boundary_conflict":
        return "boundary_conflict"
    if value == "integrity_error":
        return "integrity_error"
    raise ValueError("unsupported projection failure code")
