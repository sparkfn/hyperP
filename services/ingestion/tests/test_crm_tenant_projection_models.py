"""Focused immutable-model tests for CRM tenant projection materialization."""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

import pytest
from _standalone_crm_lane_a_fakes import prepared_mapping_revision, projection_scope
from src.crm_tenant_mapping_identity import mapping_head_id
from src.crm_tenant_mapping_models import CrmTenantMappingExpectedHeadBoundary
from src.crm_tenant_projection_identity import (
    command_fingerprint,
    projection_association_id,
    projection_head_id,
    projection_input_id,
    projection_release_id,
    projection_support_id,
)
from src.crm_tenant_projection_models import (
    CrmTenantProjectionCursor,
    CrmTenantProjectionMaterializationCommand,
    CrmTenantProjectionReleaseSummary,
)

_DIGEST = "sha256:" + "a" * 64


def _command() -> CrmTenantProjectionMaterializationCommand:
    scope = projection_scope()
    return CrmTenantProjectionMaterializationCommand(
        scope,
        "request-a",
        "census-a",
        _DIGEST,
        prepared_mapping_revision().revision_id,
        prepared_mapping_revision().manifest_digest,
        CrmTenantMappingExpectedHeadBoundary(
            scope.mapping_scope, mapping_head_id(scope.mapping_scope), None
        ),
        None,
        2,
    )


def _summary(
    phase: Literal["capture", "projection", "complete"] = "capture",
) -> CrmTenantProjectionReleaseSummary:
    scope = projection_scope()
    return CrmTenantProjectionReleaseSummary(
        scope,
        projection_release_id(scope, 1),
        1,
        "request-a",
        _DIGEST,
        "census-a",
        prepared_mapping_revision().revision_id,
        prepared_mapping_revision().manifest_digest,
        "building",
        phase,
        None,
        None,
        0,
        0,
        0,
        0,
    )


def test_deterministic_identities_and_fingerprint_are_scope_bound() -> None:
    command = _command()

    assert projection_head_id(command.scope).startswith("sha256:")
    assert projection_release_id(command.scope, 1) != projection_release_id(command.scope, 2)
    assert projection_input_id("release", "contact", "1") != projection_input_id(
        "release", "lead", "1"
    )
    association = projection_association_id(
        "release", "input", "contact", "1", "entity", "tenant_member"
    )
    assert projection_support_id(association, "obs", "target").startswith("sha256:")
    assert command_fingerprint(command) == command.release_fingerprint
    assert (
        command_fingerprint(replace(command, request_id="request-b")) != command.release_fingerprint
    )


def test_cursor_and_page_limit_are_strict() -> None:
    assert CrmTenantProjectionCursor("contact", 1).subject_id == 1
    with pytest.raises(ValueError, match="between"):
        replace(_command(), page_limit=0)
    with pytest.raises(ValueError, match="cursor subject"):
        CrmTenantProjectionCursor("lead", 0)


def test_completed_summary_requires_terminal_phase() -> None:
    with pytest.raises(ValueError, match="complete phase"):
        replace(_summary(), state="completed")
    assert replace(_summary("complete"), state="completed").terminal


def test_terminal_summary_requires_state_consistent_failure_code() -> None:
    with pytest.raises(ValueError, match="requires a failure_code"):
        replace(_summary(), state="failed")
    with pytest.raises(ValueError, match="unsupported projection failure code"):
        replace(_summary(), state="failed", failure_code="unsupported")
    with pytest.raises(ValueError, match="non-failed"):
        replace(_summary("complete"), state="completed", failure_code="integrity_error")
    with pytest.raises(ValueError, match="non-failed"):
        replace(_summary(), state="cancelled", failure_code="boundary_conflict")
    assert replace(_summary(), state="failed", failure_code="integrity_error").terminal
