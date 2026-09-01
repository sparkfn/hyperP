"""Stable #307 activation boundaries shared by census integration and Neo4j CAS work.

This module intentionally contains no Neo4j implementation.  The activation
worker owns persistence behind these contracts; the census lead owns request
construction, child execution, and post-commit settlement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from src.crm_tenant_mapping_contracts import CrmTenantMappingExpectedHead, CrmTenantMappingScope
from src.crm_tenant_projection_records import (
    CrmTenantProjectionExpectedHead,
    CrmTenantProjectionScope,
)
from src.standalone_crm_census_types import _integer, _text, _utc


@dataclass(frozen=True)
class CrmTenantActivationCandidate:
    """One exact prepared mapping candidate; rollback provenance is not a candidate."""

    revision_id: str
    manifest_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "revision_id", _text(self.revision_id, "revision_id"))
        _require_sha256(self.manifest_digest, "manifest_digest")


@dataclass(frozen=True)
class CrmTenantActivationRelease:
    """One exact completed, unpublished projection release."""

    release_id: str
    release_fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "release_id", _text(self.release_id, "release_id"))
        _require_sha256(self.release_fingerprint, "release_fingerprint")


@dataclass(frozen=True)
class CrmTenantActivationCommand:
    """Complete immutable CAS input, including both full predecessor identities."""

    mapping_scope: CrmTenantMappingScope
    projection_scope: CrmTenantProjectionScope
    candidate: CrmTenantActivationCandidate
    release: CrmTenantActivationRelease
    expected_mapping_head: CrmTenantMappingExpectedHead | None
    expected_projection_head: CrmTenantProjectionExpectedHead | None
    census_id: str
    generation: int
    task_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.mapping_scope, CrmTenantMappingScope):
            raise ValueError("activation requires a mapping scope")
        if not isinstance(self.projection_scope, CrmTenantProjectionScope):
            raise ValueError("activation requires a projection scope")
        if self.projection_scope.mapping_scope != self.mapping_scope:
            raise ValueError("activation scopes must match")
        if not isinstance(self.candidate, CrmTenantActivationCandidate):
            raise ValueError("activation requires an exact candidate")
        if not isinstance(self.release, CrmTenantActivationRelease):
            raise ValueError("activation requires an exact release")
        if self.expected_mapping_head is not None and not isinstance(
            self.expected_mapping_head, CrmTenantMappingExpectedHead
        ):
            raise ValueError("activation mapping predecessor is invalid")
        if self.expected_projection_head is not None and not isinstance(
            self.expected_projection_head, CrmTenantProjectionExpectedHead
        ):
            raise ValueError("activation projection predecessor is invalid")
        object.__setattr__(self, "census_id", _text(self.census_id, "census_id"))
        _integer(self.generation, "generation", 1)
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id"))


@dataclass(frozen=True)
class CrmTenantActivationReceipt:
    """Durable release-bound receipt used by post-CAS settlement and replay."""

    release_id: str
    census_id: str
    generation: int
    task_id: str
    candidate_revision_id: str
    activated_at: str
    prior_mapping_head: CrmTenantMappingExpectedHead | None
    prior_projection_head: CrmTenantProjectionExpectedHead | None

    def __post_init__(self) -> None:
        for field in ("release_id", "census_id", "task_id", "candidate_revision_id"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        _integer(self.generation, "generation", 1)
        object.__setattr__(self, "activated_at", _utc(self.activated_at, "activated_at"))


@dataclass(frozen=True)
class CrmTenantActivationResult:
    """CAS outcome.  Successful exact replay returns the original receipt unchanged."""

    receipt: CrmTenantActivationReceipt
    replayed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, CrmTenantActivationReceipt):
            raise ValueError("activation result requires a durable receipt")
        if not isinstance(self.replayed, bool):
            raise ValueError("activation replayed must be boolean")


class CrmTenantActivationRepository(Protocol):
    """Single transaction CAS and strict receipt-read seam."""

    def activate(self, command: CrmTenantActivationCommand) -> CrmTenantActivationResult: ...

    def read_receipt(
        self,
        scope: CrmTenantProjectionScope,
        release: CrmTenantActivationRelease,
        census_id: str,
        generation: int,
        task_id: str,
    ) -> CrmTenantActivationReceipt | None: ...


def _require_sha256(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"{field} must be a canonical sha256 digest")
