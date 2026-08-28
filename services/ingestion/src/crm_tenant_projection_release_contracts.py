"""Release and active-head contracts for immutable CRM tenant projections."""

from __future__ import annotations

from dataclasses import dataclass

from src.crm_tenant_mapping_contracts import CrmTenantMappingRevision
from src.crm_tenant_projection_records import (
    CRM_TENANT_PROJECTION_CONTRACT_VERSION,
    CrmTenantProjectionAssociation,
    CrmTenantProjectionDecision,
    CrmTenantProjectionExpectedHead,
    CrmTenantProjectionInput,
    CrmTenantProjectionMembershipHeadBoundary,
    CrmTenantProjectionReleaseState,
    CrmTenantProjectionScope,
    CrmTenantProjectionSupport,
    _canonical_text,
    _require_sha256,
)
from src.standalone_crm_census_types import _integer, _utc

_RELEASE_STATES = frozenset({"building", "completed", "failed", "cancelled", "published"})


@dataclass(frozen=True)
class CrmTenantProjectionRelease:
    """Immutable release; validation deliberately has no persistence behavior."""

    scope: CrmTenantProjectionScope
    release_id: str
    release_number: int
    request_id: str
    release_fingerprint: str
    source_census_id: str
    source_census_fingerprint: str
    membership_head_boundary: CrmTenantProjectionMembershipHeadBoundary
    mapping_revision: CrmTenantMappingRevision
    expected_prior_head: CrmTenantProjectionExpectedHead | None
    state: CrmTenantProjectionReleaseState
    inputs: tuple[CrmTenantProjectionInput, ...]
    decisions: tuple[CrmTenantProjectionDecision, ...]
    associations: tuple[CrmTenantProjectionAssociation, ...]
    supports: tuple[CrmTenantProjectionSupport, ...]
    contract_version: str = CRM_TENANT_PROJECTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.scope, CrmTenantProjectionScope):
            raise ValueError("release requires a canonical projection scope")
        for field in ("release_id", "request_id", "source_census_id"):
            object.__setattr__(self, field, _canonical_text(getattr(self, field), field))
        _integer(self.release_number, "release_number", 1)
        _require_sha256(self.release_fingerprint, "release_fingerprint")
        _require_sha256(self.source_census_fingerprint, "source_census_fingerprint")
        if self.state not in _RELEASE_STATES:
            raise ValueError("invalid projection release state")
        if self.contract_version != CRM_TENANT_PROJECTION_CONTRACT_VERSION:
            raise ValueError("unsupported tenant projection contract version")
        if not isinstance(self.membership_head_boundary, CrmTenantProjectionMembershipHeadBoundary):
            raise ValueError("release requires a frozen membership-head boundary")
        if not isinstance(self.mapping_revision, CrmTenantMappingRevision):
            raise ValueError("release requires an immutable mapping revision")
        if self.expected_prior_head is not None and not isinstance(
            self.expected_prior_head,
            CrmTenantProjectionExpectedHead,
        ):
            raise ValueError("expected_prior_head must be a projection head identity")
        _validate_release_collections(self)
        if self.membership_head_boundary.scope != self.scope:
            raise ValueError("release membership boundary must use its exact scope")
        if (
            self.mapping_revision.scope != self.scope.mapping_scope
            or self.mapping_revision.state != "active"
        ):
            raise ValueError("release requires its exact active mapping revision")
        if (
            self.expected_prior_head is not None
            and self.release_number <= self.expected_prior_head.active_release_number
        ):
            raise ValueError("release_number must advance the expected prior head")
        _validate_release_records(self)

    @property
    def mapping_revision_id(self) -> str:
        return self.mapping_revision.revision_id

    @property
    def mapping_revision_number(self) -> int:
        return self.mapping_revision.revision_number

    @property
    def mapping_manifest_digest(self) -> str:
        return self.mapping_revision.manifest_digest


@dataclass(frozen=True)
class CrmTenantProjectionActiveHead:
    """Visible projection head; #307 alone may create it from a completed release."""

    scope: CrmTenantProjectionScope
    head_id: str
    active_release: CrmTenantProjectionRelease
    published_at: str
    expected_head: CrmTenantProjectionExpectedHead | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "head_id", _canonical_text(self.head_id, "head_id"))
        object.__setattr__(self, "published_at", _utc(self.published_at, "published_at"))
        if not isinstance(self.scope, CrmTenantProjectionScope):
            raise ValueError("active projection head must use a canonical projection scope")
        if not isinstance(self.active_release, CrmTenantProjectionRelease):
            raise ValueError("active projection head requires a projection release")
        if self.expected_head is not None and not isinstance(
            self.expected_head,
            CrmTenantProjectionExpectedHead,
        ):
            raise ValueError("expected_head must be a projection head identity")
        if self.active_release.scope != self.scope or self.active_release.state != "published":
            raise ValueError("active projection head requires its exact published release")
        if self.expected_head is not None and (
            self.active_release.release_number <= self.expected_head.active_release_number
        ):
            raise ValueError("active release_number must advance expected head")

    @property
    def active_release_id(self) -> str:
        return self.active_release.release_id

    @property
    def active_release_number(self) -> int:
        return self.active_release.release_number

    @property
    def active_release_fingerprint(self) -> str:
        return self.active_release.release_fingerprint


def _validate_release_collections(release: CrmTenantProjectionRelease) -> None:
    if not isinstance(release.inputs, tuple):
        raise ValueError("release inputs must be an immutable tuple")
    if any(not isinstance(item, CrmTenantProjectionInput) for item in release.inputs):
        raise ValueError("release inputs must contain projection inputs")
    if not isinstance(release.decisions, tuple):
        raise ValueError("release decisions must be an immutable tuple")
    if any(not isinstance(item, CrmTenantProjectionDecision) for item in release.decisions):
        raise ValueError("release decisions must contain projection decisions")
    if not isinstance(release.associations, tuple):
        raise ValueError("release associations must be an immutable tuple")
    if any(not isinstance(item, CrmTenantProjectionAssociation) for item in release.associations):
        raise ValueError("release associations must contain projection associations")
    if not isinstance(release.supports, tuple):
        raise ValueError("release supports must be an immutable tuple")
    if any(not isinstance(item, CrmTenantProjectionSupport) for item in release.supports):
        raise ValueError("release supports must contain projection supports")


def _validate_release_records(release: CrmTenantProjectionRelease) -> None:
    inputs = {item.input_id: item for item in release.inputs}
    if len(inputs) != len(release.inputs):
        raise ValueError("release inputs must have unique input_id values")
    if any(item.release_id != release.release_id for item in release.inputs):
        raise ValueError("release inputs must use this release identity")
    if any(
        item.membership_head not in release.membership_head_boundary.membership_heads
        for item in release.inputs
    ):
        raise ValueError("release inputs must use frozen boundary membership heads")
    if release.state in {"completed", "published"}:
        boundary_heads = release.membership_head_boundary.membership_heads
        input_heads = tuple(item.membership_head for item in release.inputs)
        if len(input_heads) != len(boundary_heads) or set(input_heads) != set(boundary_heads):
            raise ValueError("completed release inputs must exactly cover the frozen boundary")
    decisions = {item.input_id: item for item in release.decisions}
    if len(decisions) != len(release.decisions) or set(decisions) != set(inputs):
        raise ValueError("release must have exactly one decision per input")
    if any(item.release_id != release.release_id for item in release.decisions):
        raise ValueError("release decisions must use this release identity")
    _validate_associations_and_supports(release, inputs, decisions)


def _validate_associations_and_supports(
    release: CrmTenantProjectionRelease,
    inputs: dict[str, CrmTenantProjectionInput],
    decisions: dict[str, CrmTenantProjectionDecision],
) -> None:
    associations = {item.association_id: item for item in release.associations}
    if len(associations) != len(release.associations):
        raise ValueError("release associations must have unique association_id values")
    if any(item.release_id != release.release_id for item in release.associations):
        raise ValueError("release associations must use this release identity")
    association_keys = {
        (item.input_id, item.subject_type, item.subject_id, item.entity_key, item.relationship_kind)
        for item in release.associations
    }
    if len(association_keys) != len(release.associations):
        raise ValueError("associations must deduplicate without company_id")
    support_keys = {
        (item.association_id, item.membership_observation_id, item.mapping_target_id)
        for item in release.supports
    }
    if len(support_keys) != len(release.supports):
        raise ValueError("release supports must have unique exact support identities")
    if any(item.release_id != release.release_id for item in release.supports):
        raise ValueError("release supports must use this release identity")
    counts = {item.input_id: 0 for item in release.inputs}
    supported = {item.association_id: 0 for item in release.associations}
    for association in release.associations:
        input_item = inputs.get(association.input_id)
        if input_item is None or (association.subject_type, association.subject_id) != (
            input_item.subject_type,
            input_item.subject_id,
        ):
            raise ValueError("association must use its input exact subject")
        counts[association.input_id] += 1
    for support in release.supports:
        association = associations.get(support.association_id)
        input_item = inputs.get(association.input_id) if association is not None else None
        if association is None or input_item is None:
            raise ValueError("support must reference this release association and input")
        if (
            support.membership_observation.snapshot_id
            != input_item.membership_head.snapshot_record.snapshot_id
        ):
            raise ValueError("support must use its frozen membership head")
        if support.mapping_target.entry.revision_id != release.mapping_revision_id:
            raise ValueError("support must use this release mapping revision")
        if (support.mapping_target.entity_key, support.mapping_target.relationship_kind) != (
            association.entity_key,
            association.relationship_kind,
        ):
            raise ValueError("support mapping target must prove its association")
        supported[association.association_id] += 1
    if any(count == 0 for count in supported.values()):
        raise ValueError("every association requires correlated support")
    _validate_decisions(inputs, decisions, counts)


def _validate_decisions(
    inputs: dict[str, CrmTenantProjectionInput],
    decisions: dict[str, CrmTenantProjectionDecision],
    counts: dict[str, int],
) -> None:
    for input_id, input_item in inputs.items():
        decision = decisions[input_id]
        bindings = input_item.membership_head.snapshot_record.membership_snapshot.bindings
        if decision.decision == "associated" and counts[input_id] == 0:
            raise ValueError("associated decision requires associations")
        if decision.decision == "zero_target" and counts[input_id] != 0:
            raise ValueError("zero_target decision cannot have associations")
        if not bindings and decision.zero_target_reason != "empty_membership":
            raise ValueError("empty membership requires empty_membership")
        if (
            bindings
            and decision.decision == "zero_target"
            and decision.zero_target_reason != "no_mapped_targets"
        ):
            raise ValueError("non-empty zero target requires no_mapped_targets")
