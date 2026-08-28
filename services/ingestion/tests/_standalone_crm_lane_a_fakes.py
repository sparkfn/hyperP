"""Typed contract fixtures and fake atomic repositories for #301 ownership tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

from src.connectors.bitrix_openlines.models import CrmCompanyBindingPayload
from src.crm_company_contracts import (
    CrmCompanyDescriptionHead,
    CrmCompanyDescriptionObservation,
    CrmCompanyMembershipHead,
    CrmCompanyMembershipObservation,
    CrmCompanyMembershipSnapshotRecord,
    CrmCompanyReference,
)
from src.crm_identity_associations import normalize_company_membership_snapshot
from src.crm_tenant_mapping_contracts import (
    CrmTenantActiveMappingHead,
    CrmTenantMappingAuthorization,
    CrmTenantMappingCompanyEntry,
    CrmTenantMappingEntry,
    CrmTenantMappingEntryTarget,
    CrmTenantMappingManifest,
    CrmTenantMappingRevision,
    CrmTenantMappingScope,
    CrmTenantMappingTarget,
)
from src.crm_tenant_projection_contracts import (
    CrmTenantProjectionActiveHead,
    CrmTenantProjectionAssociation,
    CrmTenantProjectionDecision,
    CrmTenantProjectionInput,
    CrmTenantProjectionMembershipHeadBoundary,
    CrmTenantProjectionRelease,
    CrmTenantProjectionScope,
    CrmTenantProjectionSupport,
)
from src.standalone_crm_census_types import StandaloneCrmStreamKind
from src.standalone_crm_child_contracts import (
    CompanySourceChildEnvelope,
    ContactBindingSubposition,
    ContactSourceChildEnvelope,
    LeadSourceChildEnvelope,
    StandaloneCrmSourceAvailability,
    StandaloneCrmSourceChildBudgetAuthorization,
    StandaloneCrmSourceChildScope,
    StandaloneCrmSourceChildUnitAuthority,
)
from src.standalone_crm_unit_repository import (
    StandaloneCrmAtomicUnitCommit,
    StandaloneCrmAtomicUnitRepository,
)

_AVAILABLE_AT = "2026-08-28T00:00:00Z"
_OBSERVED_AT = "2026-08-27T00:00:00Z"
_DIGEST = "sha256:" + "a" * 64

MutationT = TypeVar("MutationT")
ResultT = TypeVar("ResultT")


def source_scope() -> StandaloneCrmSourceChildScope:
    return StandaloneCrmSourceChildScope("bitrix_chat", "portal-a", "control-a")


def source_availability() -> StandaloneCrmSourceAvailability:
    return StandaloneCrmSourceAvailability(_AVAILABLE_AT)


def source_budget(
    unit: StandaloneCrmSourceChildUnitAuthority,
) -> StandaloneCrmSourceChildBudgetAuthorization:
    return StandaloneCrmSourceChildBudgetAuthorization(
        "authorization-a",
        _DIGEST,
        unit.census_id,
        unit.stream_kind,
        unit.generation,
        unit.fence_token,
        unit.fence_owner_id,
        unit.task_name,
        unit.task_id,
        unit.payload_digest,
        2,
        10,
        4,
        20,
        "2026-08-28T12:00:00Z",
        "2026-08-29T00:00:00Z",
    )


def _source_unit(stream_kind: StandaloneCrmStreamKind) -> StandaloneCrmSourceChildUnitAuthority:
    return StandaloneCrmSourceChildUnitAuthority(
        "census-a",
        stream_kind,
        1,
        2,
        "worker-a",
        "source.child",
        f"{stream_kind}-task",
        _DIGEST,
    )


def contact_envelope() -> ContactSourceChildEnvelope:
    unit = _source_unit("contact")
    return ContactSourceChildEnvelope(
        source_scope(),
        unit,
        10,
        5,
        source_availability(),
        source_budget(unit),
        ContactBindingSubposition(5, 0),
    )


def lead_envelope() -> LeadSourceChildEnvelope:
    unit = _source_unit("lead")
    return LeadSourceChildEnvelope(
        source_scope(),
        unit,
        10,
        5,
        source_availability(),
        source_budget(unit),
    )


def company_envelope() -> CompanySourceChildEnvelope:
    unit = _source_unit("company")
    return CompanySourceChildEnvelope(
        source_scope(),
        unit,
        10,
        5,
        source_availability(),
        source_budget(unit),
    )


def company_reference() -> CrmCompanyReference:
    return CrmCompanyReference(source_scope(), "303", "bitrix-crm-company-303")


def company_description() -> CrmCompanyDescriptionObservation:
    return CrmCompanyDescriptionObservation(
        company_reference(),
        "company-record-303",
        1,
        "company-hash-303",
        "Northwind",
        _OBSERVED_AT,
        source_availability(),
    )


def company_description_head() -> CrmCompanyDescriptionHead:
    observation = company_description()
    return CrmCompanyDescriptionHead(observation.company_reference, observation)


def empty_membership_snapshot_record() -> CrmCompanyMembershipSnapshotRecord:
    return _membership_snapshot_record(())


def membership_snapshot_record() -> CrmCompanyMembershipSnapshotRecord:
    bindings = (CrmCompanyBindingPayload("303", 0, "7", "Y"),)
    return _membership_snapshot_record(bindings)


def _membership_snapshot_record(
    payloads: tuple[CrmCompanyBindingPayload, ...],
) -> CrmCompanyMembershipSnapshotRecord:
    snapshot = normalize_company_membership_snapshot(
        subject_type="contact",
        subject_id="101",
        payloads=payloads,
    )
    return CrmCompanyMembershipSnapshotRecord(
        source_scope(),
        snapshot,
        "bitrix-crm-contact-101",
        "contact-record-101",
        1,
        "contact-hash-101",
        _OBSERVED_AT,
        source_availability(),
        len(snapshot.bindings),
    )


def membership_observation() -> CrmCompanyMembershipObservation:
    return CrmCompanyMembershipObservation(
        membership_snapshot_record(), company_reference(), 0, "7", True
    )


def membership_head() -> CrmCompanyMembershipHead:
    snapshot_record = membership_snapshot_record()
    return CrmCompanyMembershipHead(
        source_scope(),
        snapshot_record.subject_type,
        snapshot_record.subject_id,
        snapshot_record,
    )


def mapping_scope() -> CrmTenantMappingScope:
    return CrmTenantMappingScope("bitrix_chat", "portal-a", "control-a")


def mapping_manifest() -> CrmTenantMappingManifest:
    return CrmTenantMappingManifest(
        mapping_scope(),
        (CrmTenantMappingCompanyEntry("303", (CrmTenantMappingTarget("entity-a"),)),),
    )


def active_mapping_revision() -> CrmTenantMappingRevision:
    manifest = mapping_manifest()
    authorization = CrmTenantMappingAuthorization(
        "reviewer-a",
        "approval-301",
        _DIGEST,
        _AVAILABLE_AT,
        "2026-08-29T00:00:00Z",
    )
    return CrmTenantMappingRevision(
        mapping_scope(),
        "mapping-revision-1",
        1,
        manifest.digest,
        1,
        1,
        "mapping-prepare-1",
        authorization,
        "active",
    )


def active_mapping_head() -> CrmTenantActiveMappingHead:
    revision = active_mapping_revision()
    return CrmTenantActiveMappingHead(
        mapping_scope(),
        "mapping-head-1",
        revision.revision_id,
        revision.revision_number,
        revision.manifest_digest,
        _AVAILABLE_AT,
        None,
    )


def projection_scope() -> CrmTenantProjectionScope:
    return CrmTenantProjectionScope("bitrix_chat", "portal-a", "control-a")


def projection_release() -> CrmTenantProjectionRelease:
    release_id = "projection-release-1"
    head = membership_head()
    boundary = CrmTenantProjectionMembershipHeadBoundary(projection_scope(), (head,))
    input_item = CrmTenantProjectionInput(release_id, head)
    association = CrmTenantProjectionAssociation(
        release_id,
        input_item.input_id,
        input_item.subject_type,
        input_item.subject_id,
        "entity-a",
    )
    manifest_entry = mapping_manifest().entries[0]
    persisted_entry = CrmTenantMappingEntry("mapping-revision-1", manifest_entry)
    support = CrmTenantProjectionSupport(
        release_id,
        association,
        membership_observation(),
        CrmTenantMappingEntryTarget(persisted_entry, manifest_entry.targets[0]),
    )
    return CrmTenantProjectionRelease(
        projection_scope(),
        release_id,
        1,
        "projection-request-1",
        _DIGEST,
        "census-a",
        _DIGEST,
        boundary,
        active_mapping_revision(),
        None,
        "published",
        (input_item,),
        (CrmTenantProjectionDecision(release_id, input_item.input_id, "associated"),),
        (association,),
        (support,),
    )


def active_projection_head() -> CrmTenantProjectionActiveHead:
    return CrmTenantProjectionActiveHead(
        projection_scope(),
        "projection-head-1",
        projection_release(),
        _AVAILABLE_AT,
        None,
    )


@dataclass(frozen=True)
class ContactMutation:
    source_record_id: str


@dataclass(frozen=True)
class LeadMutation:
    source_record_id: str


@dataclass(frozen=True)
class CompanyMutation:
    source_record_id: str


@dataclass(frozen=True)
class ContactResult:
    committed: bool


@dataclass(frozen=True)
class LeadResult:
    committed: bool


@dataclass(frozen=True)
class CompanyResult:
    committed: bool


@dataclass
class FakeAtomicUnitRepository(StandaloneCrmAtomicUnitRepository[MutationT, ResultT]):
    result_for: Callable[[StandaloneCrmAtomicUnitCommit[MutationT]], ResultT]
    commits: list[StandaloneCrmAtomicUnitCommit[MutationT]] = field(default_factory=list)

    def commit_unit(self, request: StandaloneCrmAtomicUnitCommit[MutationT]) -> ResultT:
        self.commits.append(request)
        return self.result_for(request)


def contact_repository() -> FakeAtomicUnitRepository[ContactMutation, ContactResult]:
    return FakeAtomicUnitRepository(lambda _request: ContactResult(True))


def lead_repository() -> FakeAtomicUnitRepository[LeadMutation, LeadResult]:
    return FakeAtomicUnitRepository(lambda _request: LeadResult(True))


def company_repository() -> FakeAtomicUnitRepository[CompanyMutation, CompanyResult]:
    return FakeAtomicUnitRepository(lambda _request: CompanyResult(True))
