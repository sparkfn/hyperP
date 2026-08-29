"""Strict A-S2 builders for company descriptions and complete memberships."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.crm_company_contracts import (
    CrmCompanyDescriptionHeadCompareAndSet,
    CrmCompanyDescriptionObservation,
    CrmCompanyMembershipHeadCompareAndSet,
    CrmCompanyMembershipObservation,
    CrmCompanyMembershipSnapshotRecord,
    CrmCompanyReference,
)
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_child_contracts import (
    CompanySourceChildEnvelope,
    ContactSourceChildEnvelope,
    LeadSourceChildEnvelope,
)
from src.standalone_crm_unit_repository import (
    StandaloneCrmAtomicUnitCommit,
    StandaloneCrmUnitAccountingDelta,
)

type CrmCompanyMembershipCommitDecision = Literal[
    "committed",
    "idempotent",
    "stale_or_conflict",
    "attempt_exhausted",
    "occurrence_exhausted",
    "authority_rejected",
]


@dataclass(frozen=True)
class CrmCompanyDescriptionMutation:
    """One immutable TITLE observation and exact description-head CAS."""

    observation: CrmCompanyDescriptionObservation
    compare_and_set: CrmCompanyDescriptionHeadCompareAndSet

    def __post_init__(self) -> None:
        if self.compare_and_set.proposed_head.observation != self.observation:
            raise ValueError("description CAS must select the proposed observation")
        if self.compare_and_set.decision_for(self.compare_and_set.expected_head) != "advance":
            raise ValueError("description CAS proposal must advance its expected head")


@dataclass(frozen=True)
class CrmCompanyMembershipMutation:
    """One complete snapshot and exact membership-head CAS."""

    snapshot_record: CrmCompanyMembershipSnapshotRecord
    observations: tuple[CrmCompanyMembershipObservation, ...]
    compare_and_set: CrmCompanyMembershipHeadCompareAndSet

    def __post_init__(self) -> None:
        if self.compare_and_set.proposed_head.snapshot_record != self.snapshot_record:
            raise ValueError("membership CAS must select the proposed snapshot")
        if self.compare_and_set.decision_for(self.compare_and_set.expected_head) != "advance":
            raise ValueError("membership CAS proposal must advance its expected head")
        bindings = self.snapshot_record.membership_snapshot.bindings
        if len(self.observations) != len(bindings):
            raise ValueError("membership observations must cover all complete bindings")
        for binding, observation in zip(bindings, self.observations, strict=True):
            if observation.snapshot_record != self.snapshot_record:
                raise ValueError("membership observation has a different snapshot")
            if observation.company_id != binding.company_id:
                raise ValueError("membership observation company is not canonical")
            if observation.company_reference != membership_company_reference(
                self.snapshot_record,
                binding.company_id,
            ):
                raise ValueError("membership observation must use the canonical company reference")
            if (observation.sort, observation.role_id, observation.is_primary) != (
                binding.sort,
                binding.role_id,
                binding.is_primary,
            ):
                raise ValueError("membership observation must preserve all binding fields")


type CrmCompanyMembershipUnitMutation = CrmCompanyDescriptionMutation | CrmCompanyMembershipMutation


@dataclass(frozen=True)
class CrmCompanyMembershipCommitResult:
    """Typed outcome of one fenced atomic unit transaction."""

    decision: CrmCompanyMembershipCommitDecision

    @property
    def committed(self) -> bool:
        """Whether a new transition was persisted."""
        return self.decision == "committed"


def membership_company_reference(
    snapshot: CrmCompanyMembershipSnapshotRecord,
    company_id: str,
) -> CrmCompanyReference:
    """Return the reference-only identity authorized by this complete snapshot."""
    if not any(
        binding.company_id == company_id for binding in snapshot.membership_snapshot.bindings
    ):
        raise ValueError("company ID is absent from the complete membership snapshot")
    return CrmCompanyReference(snapshot.scope, company_id, f"bitrix-crm-company-{company_id}")


def build_company_description_commit(
    envelope: CompanySourceChildEnvelope,
    mutation: CrmCompanyDescriptionMutation,
    expected: StandaloneCrmCheckpoint,
    proposed: StandaloneCrmCheckpoint,
    delta: StandaloneCrmUnitAccountingDelta,
) -> StandaloneCrmAtomicUnitCommit[CrmCompanyDescriptionMutation]:
    """Validate a bounded deterministic company-description transaction."""
    _validate_delta(delta)
    observation = mutation.observation
    if observation.company_reference.scope != envelope.scope:
        raise ValueError("description scope must equal the company envelope scope")
    if observation.availability != envelope.availability:
        raise ValueError("description availability must equal the parent census clock")
    expected_source_record_id = f"bitrix-crm-company-{observation.company_reference.company_id}"
    if observation.company_reference.source_record_id != expected_source_record_id:
        raise ValueError("description must use the canonical company source-record identity")
    if int(observation.company_reference.company_id) > envelope.frozen_upper_id:
        raise ValueError("company ID exceeds the frozen upper bound")
    if int(observation.company_reference.company_id) != proposed.last_committed_id:
        raise ValueError("company checkpoint must select the described company")
    return StandaloneCrmAtomicUnitCommit(envelope, mutation, expected, proposed, delta)


def build_company_membership_commit(
    envelope: ContactSourceChildEnvelope | LeadSourceChildEnvelope,
    mutation: CrmCompanyMembershipMutation,
    expected: StandaloneCrmCheckpoint,
    proposed: StandaloneCrmCheckpoint,
    delta: StandaloneCrmUnitAccountingDelta,
) -> StandaloneCrmAtomicUnitCommit[CrmCompanyMembershipMutation]:
    """Validate complete evidence and parent-issued checkpoint authority."""
    _validate_delta(delta)
    snapshot = mutation.snapshot_record
    if snapshot.scope != envelope.scope or snapshot.availability != envelope.availability:
        raise ValueError("snapshot scope and availability must match the envelope")
    if snapshot.subject_type != envelope.unit.stream_kind:
        raise ValueError("snapshot subject type must match the source child stream")
    expected_source_record_id = f"bitrix-crm-{snapshot.subject_type}-{snapshot.subject_id}"
    if snapshot.source_record_id != expected_source_record_id:
        raise ValueError("membership must use the canonical subject source-record identity")
    if int(snapshot.subject_id) > envelope.frozen_upper_id:
        raise ValueError("snapshot subject exceeds the frozen upper bound")
    _validate_contact_position(envelope, snapshot.subject_id, expected, proposed)
    if isinstance(envelope, LeadSourceChildEnvelope) and (
        int(snapshot.subject_id) != proposed.last_committed_id
    ):
        raise ValueError("lead checkpoint must select the membership subject")
    return StandaloneCrmAtomicUnitCommit(envelope, mutation, expected, proposed, delta)


def _validate_delta(delta: StandaloneCrmUnitAccountingDelta) -> None:
    if delta.failed_rows != 0:
        raise ValueError("A-S2 successful commits require failed_rows == 0")


def _validate_contact_position(
    envelope: ContactSourceChildEnvelope | LeadSourceChildEnvelope,
    subject_id: str,
    expected: StandaloneCrmCheckpoint,
    proposed: StandaloneCrmCheckpoint,
) -> None:
    if isinstance(envelope, LeadSourceChildEnvelope):
        if any(
            value is not None
            for value in (
                expected.binding_subject_id,
                expected.binding_offset,
                proposed.binding_subject_id,
                proposed.binding_offset,
            )
        ):
            raise ValueError("lead membership cannot carry contact binding position")
        return
    position = envelope.binding_subposition
    if position is None:
        raise ValueError("contact membership requires a parent-issued binding position")
    if (expected.binding_subject_id, expected.binding_offset) != (
        position.binding_subject_id,
        position.binding_offset,
    ):
        raise ValueError("expected checkpoint must equal the parent-issued position")
    if int(subject_id) != position.binding_subject_id:
        raise ValueError("contact binding position must select the membership subject")
    if proposed.last_committed_id != expected.last_committed_id:
        raise ValueError("membership cannot infer final contact cursor progress")
    if proposed.binding_subject_id != position.binding_subject_id:
        raise ValueError("membership cannot clear, regress, or skip its contact subject")
    if proposed.binding_offset is None or proposed.binding_offset < position.binding_offset:
        raise ValueError("membership binding offset cannot clear or regress")
