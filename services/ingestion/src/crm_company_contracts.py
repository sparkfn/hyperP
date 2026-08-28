"""Immutable CRM company description and membership source-fact contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.connectors.bitrix_openlines.crm_identity_policy import (
    CRM_COMPANY_REFERENCE_POLICY_VERSION,
)
from src.crm_company_contract_primitives import (
    CrmSourceHeadOrderKey,
    _canonical_text,
    _digest,
    _matching_binding,
    _positive_decimal,
)
from src.crm_identity_associations import (
    CRM_COMPANY_MEMBERSHIP_CONTRACT_VERSION,
    CrmCompanyMembershipSnapshot,
    CrmIdentitySubjectType,
)
from src.models import JsonValue
from src.standalone_crm_census_types import _integer, _utc
from src.standalone_crm_child_contracts import (
    StandaloneCrmSourceAvailability,
    StandaloneCrmSourceChildScope,
)

CRM_COMPANY_DESCRIPTION_CONTRACT_VERSION = "crm-company-description-v1"
CRM_COMPANY_MEMBERSHIP_RECORD_CONTRACT_VERSION = "crm-company-membership-record-v1"
type CrmSourceHeadCasDecision = Literal["advance", "idempotent", "stale_or_conflict"]


@dataclass(frozen=True)
class CrmCompanyReference:
    """Exact non-Person company identity within one source/control scope."""

    scope: StandaloneCrmSourceChildScope
    company_id: str
    source_record_id: str
    identity_policy_version: str = CRM_COMPANY_REFERENCE_POLICY_VERSION
    person_matching_prohibited: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.scope, StandaloneCrmSourceChildScope):
            raise ValueError("company reference requires a canonical source scope")
        _positive_decimal(self.company_id, "company_id")
        _canonical_text(self.source_record_id, "source_record_id")
        if self.identity_policy_version != CRM_COMPANY_REFERENCE_POLICY_VERSION:
            raise ValueError("unsupported CRM company reference policy version")
        if self.person_matching_prohibited is not True:
            raise ValueError("CRM company references must prohibit Person matching")


@dataclass(frozen=True)
class CrmCompanyDescriptionObservation:
    """Immutable TITLE-backed description evidence; null and empty stay distinct."""

    company_reference: CrmCompanyReference
    source_record_pk: str
    source_record_version: int
    source_record_hash: str
    description: str | None
    observed_at: str | None
    availability: StandaloneCrmSourceAvailability
    contract_version: str = CRM_COMPANY_DESCRIPTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.company_reference, CrmCompanyReference):
            raise ValueError("description observation requires a company reference")
        if not isinstance(self.availability, StandaloneCrmSourceAvailability):
            raise ValueError("description observation requires source availability")
        _canonical_text(self.source_record_pk, "source_record_pk")
        _integer(self.source_record_version, "source_record_version", 1)
        _canonical_text(self.source_record_hash, "source_record_hash")
        if self.description is not None and not isinstance(self.description, str):
            raise ValueError("description must be a string or null")
        if self.observed_at is not None:
            object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        if self.contract_version != CRM_COMPANY_DESCRIPTION_CONTRACT_VERSION:
            raise ValueError("unsupported CRM company description contract version")

    @property
    def company_title(self) -> str | None:
        """The existing Bitrix TITLE value carried by the description contract."""
        return self.description

    @property
    def order_key(self) -> CrmSourceHeadOrderKey:
        return CrmSourceHeadOrderKey(
            self.availability.available_at, self.source_record_version, self.source_record_pk
        )

    @property
    def observation_id(self) -> str:
        payload: list[JsonValue] = [
            self.company_reference.scope.source_instance_id,
            self.company_reference.scope.control_instance_id,
            self.company_reference.company_id,
            self.company_reference.source_record_id,
            self.source_record_pk,
            self.source_record_version,
            self.source_record_hash,
            self.description,
            self.observed_at,
            self.availability.available_at,
            self.contract_version,
        ]
        return _digest(
            "crm-company-description-observation-v1",
            payload,
        )

    @property
    def observation_digest(self) -> str:
        return self.observation_id

    @property
    def digest(self) -> str:
        return self.observation_digest


@dataclass(frozen=True)
class CrmCompanyDescriptionHead:
    """One selected immutable description observation for one scoped company."""

    company_reference: CrmCompanyReference
    observation: CrmCompanyDescriptionObservation

    def __post_init__(self) -> None:
        if not isinstance(self.company_reference, CrmCompanyReference):
            raise ValueError("description head requires a company reference")
        if not isinstance(self.observation, CrmCompanyDescriptionObservation):
            raise ValueError("description head requires a description observation")
        if self.observation.company_reference != self.company_reference:
            raise ValueError("description head observation must use its exact company reference")

    @property
    def order_key(self) -> CrmSourceHeadOrderKey:
        return self.observation.order_key


@dataclass(frozen=True)
class CrmCompanyDescriptionHeadCompareAndSet:
    """Expected-head CAS representation; implementations persist it atomically."""

    expected_head: CrmCompanyDescriptionHead | None
    proposed_head: CrmCompanyDescriptionHead

    def __post_init__(self) -> None:
        if self.expected_head is not None and not isinstance(
            self.expected_head,
            CrmCompanyDescriptionHead,
        ):
            raise ValueError("description head CAS requires a description head")
        if not isinstance(self.proposed_head, CrmCompanyDescriptionHead):
            raise ValueError("description head CAS requires a description head")
        if self.expected_head is not None and (
            self.expected_head.company_reference != self.proposed_head.company_reference
        ):
            raise ValueError("description head CAS must use one exact company reference")

    def decision_for(
        self, current_head: CrmCompanyDescriptionHead | None
    ) -> CrmSourceHeadCasDecision:
        if current_head == self.proposed_head:
            return "idempotent"
        if current_head != self.expected_head:
            return "stale_or_conflict"
        if current_head is None or self.proposed_head.order_key > current_head.order_key:
            return "advance"
        return "stale_or_conflict"


@dataclass(frozen=True)
class CrmCompanyMembershipSnapshotRecord:
    """Persistence-facing evidence for one proven-complete membership snapshot."""

    scope: StandaloneCrmSourceChildScope
    membership_snapshot: CrmCompanyMembershipSnapshot
    source_record_id: str
    source_record_pk: str
    source_record_version: int
    source_record_hash: str
    observed_at: str | None
    availability: StandaloneCrmSourceAvailability
    binding_count: int
    contract_version: str = CRM_COMPANY_MEMBERSHIP_RECORD_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.scope, StandaloneCrmSourceChildScope):
            raise ValueError("membership snapshot record requires a canonical source scope")
        if not isinstance(self.membership_snapshot, CrmCompanyMembershipSnapshot):
            raise ValueError("membership snapshot record requires a membership snapshot")
        if not isinstance(self.availability, StandaloneCrmSourceAvailability):
            raise ValueError("membership snapshot record requires source availability")
        _canonical_text(self.source_record_id, "source_record_id")
        _canonical_text(self.source_record_pk, "source_record_pk")
        _integer(self.source_record_version, "source_record_version", 1)
        _canonical_text(self.source_record_hash, "source_record_hash")
        _integer(self.binding_count, "binding_count")
        if self.binding_count != len(self.membership_snapshot.bindings):
            raise ValueError("binding_count must equal the complete snapshot binding count")
        if self.observed_at is not None:
            object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        if self.contract_version != CRM_COMPANY_MEMBERSHIP_RECORD_CONTRACT_VERSION:
            raise ValueError("unsupported CRM company membership record contract version")
        if self.membership_snapshot.contract_version != CRM_COMPANY_MEMBERSHIP_CONTRACT_VERSION:
            raise ValueError("unsupported CRM company membership snapshot contract version")

    @property
    def subject_type(self) -> CrmIdentitySubjectType:
        return self.membership_snapshot.subject_type

    @property
    def subject_kind(self) -> CrmIdentitySubjectType:
        """Schema-facing subject property name for the normalized subject type."""
        return self.subject_type

    @property
    def subject_id(self) -> str:
        return self.membership_snapshot.subject_id

    @property
    def complete_set_digest(self) -> str:
        """Retain the existing normalized complete-set digest exactly."""
        return self.membership_snapshot.digest

    @property
    def snapshot_digest(self) -> str:
        """Schema-facing name for the unchanged complete-set digest."""
        return self.complete_set_digest

    @property
    def snapshot_id(self) -> str:
        payload: list[JsonValue] = [
            self.scope.source_instance_id,
            self.scope.control_instance_id,
            self.subject_type,
            self.subject_id,
            self.snapshot_digest,
            self.source_record_id,
            self.source_record_pk,
            self.source_record_version,
            self.source_record_hash,
            self.observed_at,
            self.availability.available_at,
            self.contract_version,
        ]
        return _digest("crm-company-membership-snapshot-record-v1", payload)

    @property
    def order_key(self) -> CrmSourceHeadOrderKey:
        return CrmSourceHeadOrderKey(
            self.availability.available_at, self.source_record_version, self.source_record_pk
        )


@dataclass(frozen=True)
class CrmCompanyMembershipObservation:
    """A company binding correlated to exactly one complete subject snapshot."""

    snapshot_record: CrmCompanyMembershipSnapshotRecord
    company_reference: CrmCompanyReference
    sort: int | None
    role_id: str | None
    is_primary: bool

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_record, CrmCompanyMembershipSnapshotRecord):
            raise ValueError("membership observation requires a snapshot record")
        if not isinstance(self.company_reference, CrmCompanyReference):
            raise ValueError("membership observation requires a company reference")
        if self.company_reference.scope != self.snapshot_record.scope:
            raise ValueError("membership observation must use the snapshot exact scope")
        binding = _matching_binding(
            self.snapshot_record.membership_snapshot.bindings,
            self.company_reference.company_id,
        )
        if binding is None:
            raise ValueError("membership observation company must exist in the complete snapshot")
        if (self.sort, self.role_id, self.is_primary) != (
            binding.sort,
            binding.role_id,
            binding.is_primary,
        ):
            raise ValueError("membership observation must exactly match its normalized binding")

    @property
    def snapshot_id(self) -> str:
        return self.snapshot_record.snapshot_id

    @property
    def subject_type(self) -> CrmIdentitySubjectType:
        return self.snapshot_record.subject_type

    @property
    def subject_kind(self) -> CrmIdentitySubjectType:
        """Schema-facing subject property name for the normalized subject type."""
        return self.subject_type

    @property
    def subject_id(self) -> str:
        return self.snapshot_record.subject_id

    @property
    def company_id(self) -> str:
        """Schema-facing company identity from the exact company reference."""
        return self.company_reference.company_id

    @property
    def observation_id(self) -> str:
        payload: list[JsonValue] = [
            self.snapshot_id,
            self.company_reference.company_id,
            self.sort,
            self.role_id,
            self.is_primary,
        ]
        return _digest(
            "crm-company-membership-observation-v1",
            payload,
        )


@dataclass(frozen=True)
class CrmCompanyMembershipHead:
    """One selected complete membership snapshot for one scoped CRM subject."""

    scope: StandaloneCrmSourceChildScope
    subject_type: CrmIdentitySubjectType
    subject_id: str
    snapshot_record: CrmCompanyMembershipSnapshotRecord

    def __post_init__(self) -> None:
        if not isinstance(self.scope, StandaloneCrmSourceChildScope):
            raise ValueError("membership head requires a canonical source scope")
        if not isinstance(self.snapshot_record, CrmCompanyMembershipSnapshotRecord):
            raise ValueError("membership head requires a snapshot record")
        if self.subject_type not in {"contact", "lead"}:
            raise ValueError("membership head subject_type must be contact or lead")
        _positive_decimal(self.subject_id, "subject_id")
        if (
            self.scope != self.snapshot_record.scope
            or self.subject_type != self.snapshot_record.subject_type
            or self.subject_id != self.snapshot_record.subject_id
        ):
            raise ValueError("membership head must use its snapshot exact subject scope")

    @property
    def order_key(self) -> CrmSourceHeadOrderKey:
        return self.snapshot_record.order_key

    @property
    def subject_kind(self) -> CrmIdentitySubjectType:
        """Schema-facing subject property name for the normalized subject type."""
        return self.subject_type


@dataclass(frozen=True)
class CrmCompanyMembershipHeadCompareAndSet:
    """Expected-head CAS representation for a forward-only membership head."""

    expected_head: CrmCompanyMembershipHead | None
    proposed_head: CrmCompanyMembershipHead

    def __post_init__(self) -> None:
        if self.expected_head is not None and not isinstance(
            self.expected_head,
            CrmCompanyMembershipHead,
        ):
            raise ValueError("membership head CAS requires a membership head")
        if not isinstance(self.proposed_head, CrmCompanyMembershipHead):
            raise ValueError("membership head CAS requires a membership head")
        if self.expected_head is not None and _head_scope(self.expected_head) != _head_scope(
            self.proposed_head
        ):
            raise ValueError("membership head CAS must use one exact subject scope")

    def decision_for(
        self, current_head: CrmCompanyMembershipHead | None
    ) -> CrmSourceHeadCasDecision:
        if current_head == self.proposed_head:
            if (
                self.expected_head is None
                or self.proposed_head.order_key > self.expected_head.order_key
            ):
                return "idempotent"
            return "stale_or_conflict"
        if current_head != self.expected_head:
            return "stale_or_conflict"
        if current_head is None or self.proposed_head.order_key > current_head.order_key:
            return "advance"
        return "stale_or_conflict"


def _head_scope(
    head: CrmCompanyMembershipHead,
) -> tuple[StandaloneCrmSourceChildScope, CrmIdentitySubjectType, str]:
    return (head.scope, head.subject_type, head.subject_id)
