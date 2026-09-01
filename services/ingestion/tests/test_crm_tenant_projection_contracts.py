"""Focused contract checks for CRM tenant projection value objects."""

from __future__ import annotations

from dataclasses import fields, replace

import pytest
from _crm_tenant_projection_projection_fakes import prepared_projection_release
from _standalone_crm_lane_a_fakes import (
    active_mapping_revision,
    active_projection_head,
    projection_release,
)
from src import crm_tenant_projection_contracts as projection_contracts
from src.crm_tenant_projection_contracts import (
    CrmTenantProjectionAssociation,
    CrmTenantProjectionDecision,
    CrmTenantProjectionExpectedHead,
    CrmTenantProjectionScope,
)

_DIGEST = "sha256:" + "a" * 64


def test_scope_uses_the_canonical_mapping_contract_scope() -> None:
    scope = CrmTenantProjectionScope("bitrix_chat", "portal-a", "control-a")

    assert scope.mapping_scope.source_instance_id == "portal-a"
    with pytest.raises(ValueError, match="source_key"):
        CrmTenantProjectionScope("other", "portal-a", "control-a")


def test_public_facade_reexports_projection_literal_contract_types() -> None:
    expected = {
        "CrmTenantProjectionReleaseState",
        "CrmTenantProjectionDecisionKind",
        "CrmTenantProjectionZeroTargetReason",
    }

    assert expected <= set(projection_contracts.__all__)
    assert all(hasattr(projection_contracts, name) for name in expected)


def test_decision_closes_associated_and_zero_target_outcomes() -> None:
    assert (
        CrmTenantProjectionDecision("release-a", "input-a", "associated").zero_target_reason is None
    )
    assert CrmTenantProjectionDecision("release-a", "input-a", "zero_target", "empty_membership")
    with pytest.raises(ValueError, match="zero-target reason"):
        CrmTenantProjectionDecision("release-a", "input-a", "associated", "empty_membership")


def test_association_deduplication_identity_excludes_company_id() -> None:
    association = CrmTenantProjectionAssociation(
        "release-a", "input-a", "contact", "101", "entity-a"
    )
    replay = CrmTenantProjectionAssociation("release-a", "input-a", "contact", "101", "entity-a")

    assert "company_id" not in {field.name for field in fields(association)}
    assert "association_id" not in {field.name for field in fields(association)}
    assert association.association_id == replay.association_id
    assert association.association_id.startswith("sha256:")


def test_expected_head_requires_a_monotonic_canonical_identity() -> None:
    expected = CrmTenantProjectionExpectedHead("head-a", "release-a", 1, _DIGEST)

    assert expected.active_release_number == 1
    with pytest.raises(ValueError, match="canonical sha256"):
        CrmTenantProjectionExpectedHead("head-a", "release-a", 1, "not-a-digest")


def test_active_head_requires_the_release_exact_expected_prior_head() -> None:
    expected = CrmTenantProjectionExpectedHead(
        "projection-head-prior",
        "projection-release-prior",
        1,
        _DIGEST,
    )
    release = replace(projection_release(), release_number=2, expected_prior_head=expected)
    matching_head = replace(
        active_projection_head(),
        active_release=release,
        expected_head=expected,
    )

    assert matching_head.expected_head == release.expected_prior_head
    with pytest.raises(ValueError, match="expected prior head"):
        replace(active_projection_head(), active_release=release)
    with pytest.raises(ValueError, match="expected prior head"):
        replace(active_projection_head(), expected_head=expected)
    with pytest.raises(ValueError, match="expected prior head"):
        replace(
            active_projection_head(),
            active_release=release,
            expected_head=replace(expected, head_id="projection-head-other"),
        )
    with pytest.raises(ValueError, match="expected prior head"):
        replace(
            active_projection_head(),
            active_release=release,
            expected_head=replace(
                expected,
                active_release_fingerprint="sha256:" + "b" * 64,
            ),
        )


def test_release_carries_frozen_inputs_decisions_correlated_support_and_published_head() -> None:
    release = projection_release()
    head = active_projection_head()
    input_item = release.inputs[0]
    decision = release.decisions[0]
    association = release.associations[0]
    support = release.supports[0]

    assert release.state == "published"
    assert input_item.input_digest.startswith("sha256:")
    assert decision.decision_digest.startswith("sha256:")
    assert association.association_id == support.association_id
    assert support.mapping_target.entry.company_id == support.membership_observation.company_id
    assert support.support_digest.startswith("sha256:")
    assert head.active_release_id == release.release_id
    assert head.published_at == "2026-08-28T00:00:00Z"
    assert support.support_digest == release.supports[0].support_digest
    with pytest.raises(ValueError, match="release exact association"):
        replace(support, release_id="projection-release-other")
    with pytest.raises(ValueError, match="release mapping revision"):
        replace(
            release,
            mapping_revision=replace(
                active_mapping_revision(),
                revision_id="mapping-revision-other",
            ),
        )


def test_completed_or_published_release_requires_complete_boundary_input_coverage() -> None:
    release = prepared_projection_release()
    partial_building = replace(
        release,
        state="building",
        inputs=(),
        decisions=(),
        associations=(),
        supports=(),
    )

    assert partial_building.inputs == ()
    with pytest.raises(ValueError, match="exactly cover the frozen boundary"):
        replace(partial_building, state="completed")
    with pytest.raises(ValueError, match="exactly cover the frozen boundary"):
        replace(
            partial_building,
            state="published",
            mapping_revision=active_mapping_revision(),
        )


def test_materialization_release_accepts_prepared_mapping_but_published_requires_active() -> None:
    prepared = prepared_projection_release()

    assert prepared.mapping_revision.state == "prepared"
    with pytest.raises(ValueError, match="active mapping revision"):
        replace(prepared, state="published")
