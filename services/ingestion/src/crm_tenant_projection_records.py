"""Private shared value contracts for immutable CRM tenant projection releases."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from src.crm_company_contracts import CrmCompanyMembershipHead, CrmCompanyMembershipObservation
from src.crm_tenant_mapping_contracts import (
    CrmTenantMappingEntryTarget,
    CrmTenantMappingScope,
    CrmTenantMappingTarget,
)
from src.models import JsonValue
from src.standalone_crm_census_types import _integer, _text

CRM_TENANT_PROJECTION_CONTRACT_VERSION = "crm-tenant-projection-v1"
type CrmTenantProjectionReleaseState = Literal[
    "building", "completed", "failed", "cancelled", "published"
]
type CrmTenantProjectionDecisionKind = Literal["associated", "zero_target"]
type CrmTenantProjectionZeroTargetReason = Literal["empty_membership", "no_mapped_targets"]


@dataclass(frozen=True)
class CrmTenantProjectionScope:
    """Canonical source/control scope for one projection history."""

    source_key: str
    source_instance_id: str
    control_instance_id: str

    def __post_init__(self) -> None:
        mapping_scope = CrmTenantMappingScope(
            self.source_key, self.source_instance_id, self.control_instance_id
        )
        object.__setattr__(self, "source_key", mapping_scope.source_key)
        object.__setattr__(self, "source_instance_id", mapping_scope.source_instance_id)
        object.__setattr__(self, "control_instance_id", mapping_scope.control_instance_id)

    @property
    def mapping_scope(self) -> CrmTenantMappingScope:
        return CrmTenantMappingScope(
            self.source_key, self.source_instance_id, self.control_instance_id
        )


@dataclass(frozen=True)
class CrmTenantProjectionExpectedHead:
    """Exact predecessor identity used by a head compare-and-swap."""

    head_id: str
    active_release_id: str
    active_release_number: int
    active_release_fingerprint: str

    def __post_init__(self) -> None:
        for field in ("head_id", "active_release_id"):
            object.__setattr__(self, field, _canonical_text(getattr(self, field), field))
        _integer(self.active_release_number, "active_release_number", 1)
        _require_sha256(self.active_release_fingerprint, "active_release_fingerprint")


@dataclass(frozen=True)
class CrmTenantProjectionMembershipHeadBoundary:
    """Frozen eligible membership-head input boundary for one release."""

    scope: CrmTenantProjectionScope
    membership_heads: tuple[CrmCompanyMembershipHead, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.scope, CrmTenantProjectionScope):
            raise ValueError("membership head boundary must use a canonical projection scope")
        if not isinstance(self.membership_heads, tuple):
            raise ValueError("membership_heads must be an immutable tuple")
        if any(not isinstance(head, CrmCompanyMembershipHead) for head in self.membership_heads):
            raise ValueError("membership_heads must contain membership heads")
        if any(not _matches_scope(self.scope, head) for head in self.membership_heads):
            raise ValueError("membership head boundary must use one exact scope")
        if len({_membership_key(head) for head in self.membership_heads}) != len(
            self.membership_heads
        ):
            raise ValueError("membership boundary must contain one head per CRM subject")
        if tuple(sorted(self.membership_heads, key=_membership_key)) != self.membership_heads:
            raise ValueError("membership boundary must use canonical subject order")

    @property
    def digest(self) -> str:
        return _digest(
            "crm-tenant-projection-membership-boundary-v1",
            [
                [head.subject_type, head.subject_id, head.snapshot_record.snapshot_id]
                for head in self.membership_heads
            ],
        )


@dataclass(frozen=True)
class CrmTenantProjectionInput:
    """One release-scoped subject selected from the frozen membership boundary."""

    release_id: str
    membership_head: CrmCompanyMembershipHead

    def __post_init__(self) -> None:
        object.__setattr__(self, "release_id", _canonical_text(self.release_id, "release_id"))
        if not isinstance(self.membership_head, CrmCompanyMembershipHead):
            raise ValueError("projection input must select one membership head")

    @property
    def subject_type(self) -> Literal["contact", "lead"]:
        return self.membership_head.subject_type

    @property
    def subject_id(self) -> str:
        return self.membership_head.subject_id

    @property
    def subject_kind(self) -> Literal["contact", "lead"]:
        """Schema-facing subject property name for the normalized subject type."""
        return self.subject_type

    @property
    def input_id(self) -> str:
        return _digest(
            "crm-tenant-projection-input-id-v1",
            [self.release_id, self.subject_kind, self.subject_id],
        )

    @property
    def input_digest(self) -> str:
        return _digest(
            "crm-tenant-projection-input-v1",
            [
                self.release_id,
                self.input_id,
                self.subject_type,
                self.subject_id,
                self.membership_head.snapshot_record.snapshot_id,
            ],
        )


@dataclass(frozen=True)
class CrmTenantProjectionDecision:
    """One exact outcome for one release-scoped projection input."""

    release_id: str
    input_id: str
    decision: CrmTenantProjectionDecisionKind
    zero_target_reason: CrmTenantProjectionZeroTargetReason | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "release_id", _canonical_text(self.release_id, "release_id"))
        object.__setattr__(self, "input_id", _canonical_text(self.input_id, "input_id"))
        if self.decision not in {"associated", "zero_target"}:
            raise ValueError("invalid projection decision")
        if self.decision == "associated" and self.zero_target_reason is not None:
            raise ValueError("associated decision cannot have a zero-target reason")
        if self.decision == "zero_target" and self.zero_target_reason not in {
            "empty_membership",
            "no_mapped_targets",
        }:
            raise ValueError("zero_target decision must state its reason")

    @property
    def decision_digest(self) -> str:
        return _digest(
            "crm-tenant-projection-decision-v1",
            [self.release_id, self.input_id, self.decision, self.zero_target_reason],
        )


@dataclass(frozen=True)
class CrmTenantProjectionAssociation:
    """Company-free release association identity; evidence belongs to support."""

    release_id: str
    input_id: str
    subject_type: Literal["contact", "lead"]
    subject_id: str
    entity_key: str
    relationship_kind: Literal["tenant_member"] = "tenant_member"

    def __post_init__(self) -> None:
        for field in ("release_id", "input_id", "subject_id", "entity_key"):
            object.__setattr__(self, field, _canonical_text(getattr(self, field), field))
        if self.subject_type not in {"contact", "lead"}:
            raise ValueError("association subject_type must be contact or lead")
        CrmTenantMappingTarget(self.entity_key, self.relationship_kind)

    @property
    def subject_kind(self) -> Literal["contact", "lead"]:
        """Schema-facing subject property name for the normalized subject type."""
        return self.subject_type

    @property
    def association_id(self) -> str:
        return _digest(
            "crm-tenant-projection-association-v1",
            [
                self.release_id,
                self.input_id,
                self.subject_kind,
                self.subject_id,
                self.entity_key,
                self.relationship_kind,
            ],
        )


@dataclass(frozen=True)
class CrmTenantProjectionSupport:
    """One release-bound, correlated membership-observation and mapping-target proof."""

    release_id: str
    association: CrmTenantProjectionAssociation
    membership_observation: CrmCompanyMembershipObservation
    mapping_target: CrmTenantMappingEntryTarget

    def __post_init__(self) -> None:
        object.__setattr__(self, "release_id", _canonical_text(self.release_id, "release_id"))
        if not isinstance(self.association, CrmTenantProjectionAssociation):
            raise ValueError("projection support must use a projection association")
        if not isinstance(self.membership_observation, CrmCompanyMembershipObservation):
            raise ValueError("projection support must use a membership observation")
        if not isinstance(self.mapping_target, CrmTenantMappingEntryTarget):
            raise ValueError("projection support must use a persistence-facing mapping target")
        if self.association.release_id != self.release_id:
            raise ValueError("projection support must use its release exact association")
        if self.membership_observation.company_id != self.mapping_target.entry.company_id:
            raise ValueError(
                "projection support must correlate the same membership and mapping company"
            )

    @property
    def association_id(self) -> str:
        return self.association.association_id

    @property
    def membership_observation_id(self) -> str:
        return self.membership_observation.observation_id

    @property
    def mapping_target_id(self) -> str:
        return self.mapping_target.target_id

    @property
    def support_digest(self) -> str:
        return _digest(
            "crm-tenant-projection-support-v1",
            [
                self.release_id,
                self.association_id,
                self.membership_observation_id,
                self.mapping_target_id,
            ],
        )


def _matches_scope(scope: CrmTenantProjectionScope, head: CrmCompanyMembershipHead) -> bool:
    return (
        head.scope.source_key == scope.source_key
        and head.scope.source_instance_id == scope.source_instance_id
        and head.scope.control_instance_id == scope.control_instance_id
    )


def _membership_key(head: CrmCompanyMembershipHead) -> tuple[int, int]:
    return (0 if head.subject_type == "contact" else 1, int(head.subject_id))


def _canonical_text(value: str, field_name: str) -> str:
    normalized = _text(value, field_name)
    if normalized != value:
        raise ValueError(f"{field_name} must be canonical")
    return normalized


def _require_sha256(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"{field_name} must be a canonical sha256 digest")


def _digest(namespace: str, payload: list[JsonValue]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return (
        "sha256:"
        + hashlib.sha256(namespace.encode("utf-8") + b"\x00" + encoded.encode("utf-8")).hexdigest()
    )
