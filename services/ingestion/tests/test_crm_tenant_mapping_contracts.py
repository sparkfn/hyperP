"""Contract tests for immutable standalone CRM tenant mappings."""

from __future__ import annotations

from dataclasses import replace

import pytest
from src.crm_tenant_mapping_contracts import (
    CRM_TENANT_MAPPING_OMISSION_POLICY,
    CRM_TENANT_MAPPING_RELATIONSHIP_KIND,
    CrmTenantActiveMappingHead,
    CrmTenantMappingAuthorization,
    CrmTenantMappingCompanyEntry,
    CrmTenantMappingEntry,
    CrmTenantMappingEntryTarget,
    CrmTenantMappingExpectedHead,
    CrmTenantMappingManifest,
    CrmTenantMappingRevision,
    CrmTenantMappingRollbackProvenance,
    CrmTenantMappingScope,
    CrmTenantMappingTarget,
)

_DIGEST = "sha256:" + "a" * 64


def _scope() -> CrmTenantMappingScope:
    return CrmTenantMappingScope("bitrix_chat", "portal-a", "control-a")


def _manifest() -> CrmTenantMappingManifest:
    return CrmTenantMappingManifest(
        _scope(),
        (
            CrmTenantMappingCompanyEntry("10", (CrmTenantMappingTarget("entity-a"),)),
            CrmTenantMappingCompanyEntry("20", ()),
        ),
    )


def _authorization() -> CrmTenantMappingAuthorization:
    return CrmTenantMappingAuthorization(
        "reviewer-a",
        "approval-301",
        _DIGEST,
        "2026-08-28T00:00:00Z",
        "2026-08-29T00:00:00Z",
    )


def test_manifest_is_canonical_complete_and_preserves_empty_entry_auditability() -> None:
    manifest = _manifest()

    assert manifest.targets_for("10") == (CrmTenantMappingTarget("entity-a"),)
    assert manifest.targets_for("20") == ()
    assert manifest.targets_for("999") == ()
    assert manifest.omission_policy == CRM_TENANT_MAPPING_OMISSION_POLICY
    assert manifest.entries[1].targets == ()
    assert manifest.digest.startswith("sha256:")
    assert manifest.digest == _manifest().digest


def test_manifest_digest_changes_with_explicit_empty_entry() -> None:
    omitted = CrmTenantMappingManifest(
        _scope(),
        (CrmTenantMappingCompanyEntry("10", (CrmTenantMappingTarget("entity-a"),)),),
    )
    explicit_empty = _manifest()

    assert omitted.targets_for("20") == explicit_empty.targets_for("20") == ()
    assert omitted.digest != explicit_empty.digest


def test_mapping_rejects_noncanonical_scope_targets_and_manifest_order() -> None:
    with pytest.raises(ValueError, match="source_key"):
        CrmTenantMappingScope("another_source", "portal-a", "control-a")
    with pytest.raises(ValueError, match="canonical"):
        CrmTenantMappingScope("bitrix_chat", "Portal-A", "control-a")
    with pytest.raises(ValueError, match="relationship_kind"):
        CrmTenantMappingTarget("entity-a", relationship_kind="member")
    with pytest.raises(ValueError, match="canonical order"):
        CrmTenantMappingManifest(
            _scope(),
            (CrmTenantMappingCompanyEntry("20", ()), CrmTenantMappingCompanyEntry("10", ())),
        )


def test_mapping_target_is_reference_only_and_uses_closed_tenant_member_kind() -> None:
    target = CrmTenantMappingTarget("entity-a")

    assert target.entity_key == "entity-a"
    assert target.relationship_kind == CRM_TENANT_MAPPING_RELATIONSHIP_KIND
    assert set(target.__dataclass_fields__) == {"entity_key", "relationship_kind"}


def test_persistence_entry_and_target_identities_match_the_schema_properties() -> None:
    company_entry = _manifest().entries[0]
    entry = CrmTenantMappingEntry("revision-1", company_entry)
    target = CrmTenantMappingEntryTarget(entry, company_entry.targets[0])

    assert entry.revision_id == "revision-1"
    assert entry.company_id == "10"
    assert entry.entry_id.startswith("sha256:")
    assert target.entry_id == entry.entry_id
    assert target.entity_key == "entity-a"
    assert target.relationship_kind == "tenant_member"
    assert target.target_id.startswith("sha256:")


def test_authorization_is_bounded_nonsecret_and_time_ordered() -> None:
    authorization = _authorization()

    assert set(authorization.__dataclass_fields__) == {
        "actor",
        "authorization_reference",
        "authorization_digest",
        "authorized_at",
        "expires_at",
    }
    with pytest.raises(ValueError, match="bounded"):
        replace(authorization, actor="a" * 257)
    with pytest.raises(ValueError, match="canonical sha256"):
        replace(authorization, authorization_digest="sha256:short")
    with pytest.raises(ValueError, match="cannot be after"):
        replace(authorization, expires_at="2026-08-27T00:00:00Z")
    with pytest.raises(ValueError, match="cannot be after"):
        replace(
            authorization,
            authorized_at="2026-08-28T00:00:00.100000Z",
            expires_at="2026-08-28T00:00:00Z",
        )


def test_revision_has_closed_state_rollback_provenance_and_component_counts() -> None:
    manifest = _manifest()
    revision = CrmTenantMappingRevision(
        _scope(),
        "revision-2",
        2,
        manifest.digest,
        2,
        1,
        "prepare-request-2",
        _authorization(),
        "prepared",
        CrmTenantMappingRollbackProvenance("revision-1", 1, _DIGEST),
    )

    assert revision.rollback_provenance is not None
    assert revision.company_entry_count == 2
    with pytest.raises(ValueError, match="invalid mapping revision state"):
        replace(revision, state="retired")
    with pytest.raises(ValueError, match="integer >= 1"):
        replace(revision, revision_number=0)


def test_active_head_requires_exact_expected_identity_and_monotonic_revision_order() -> None:
    expected = CrmTenantMappingExpectedHead("head-a", "revision-1", 1, _DIGEST)
    head = CrmTenantActiveMappingHead(
        _scope(),
        "head-a",
        "revision-2",
        2,
        _manifest().digest,
        "2026-08-28T00:00:00Z",
        expected,
    )

    assert head.active_revision_number == 2
    with pytest.raises(ValueError, match="must advance"):
        replace(head, active_revision_number=1)


def test_initial_head_cas_uses_a_strict_absent_predecessor_type() -> None:
    head = CrmTenantActiveMappingHead(
        _scope(),
        "head-a",
        "revision-1",
        1,
        _manifest().digest,
        "2026-08-28T00:00:00Z",
        None,
    )

    assert head.expected_head is None
