"""Model contracts for immutable CRM tenant mapping revision authority (#304)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from src.crm_tenant_mapping_contracts import (
    CrmTenantMappingAuthorization,
    CrmTenantMappingCompanyEntry,
    CrmTenantMappingExpectedHead,
    CrmTenantMappingManifest,
    CrmTenantMappingScope,
    CrmTenantMappingTarget,
)
from src.crm_tenant_mapping_models import (
    CrmTenantMappingExpectedHeadBoundary,
    CrmTenantMappingPrepareCommand,
    CrmTenantMappingRejectCommand,
    CrmTenantMappingRejection,
    CrmTenantMappingRollbackCommand,
    authorization_is_current,
    mapping_head_id,
    mapping_revision_id,
)

_DIGEST = "sha256:" + "a" * 64


def _scope() -> CrmTenantMappingScope:
    return CrmTenantMappingScope("bitrix_chat", "portal-a", "control-a")


def _authorization() -> CrmTenantMappingAuthorization:
    return CrmTenantMappingAuthorization(
        "reviewer", "approval-304", _DIGEST, "2026-08-29T00:00:00Z", "2026-08-30T00:00:00Z"
    )


def _manifest() -> CrmTenantMappingManifest:
    return CrmTenantMappingManifest(
        _scope(),
        (
            CrmTenantMappingCompanyEntry("10", (CrmTenantMappingTarget("entity-a"),)),
            CrmTenantMappingCompanyEntry("20", ()),
        ),
    )


def test_deterministic_scope_identities_and_absent_boundary_are_strict() -> None:
    scope = _scope()
    boundary = CrmTenantMappingExpectedHeadBoundary(scope, mapping_head_id(scope), None)

    assert boundary.is_absent is True
    assert mapping_revision_id(scope, 1) != mapping_revision_id(scope, 2)
    with pytest.raises(ValueError, match="deterministic"):
        CrmTenantMappingExpectedHeadBoundary(scope, "not-the-scope-head", None)


def test_prepare_fingerprint_preserves_explicit_empty_but_excludes_operation_clock() -> None:
    scope = _scope()
    boundary = CrmTenantMappingExpectedHeadBoundary(scope, mapping_head_id(scope), None)
    command = CrmTenantMappingPrepareCommand(
        scope, "request-a", _manifest(), boundary, _authorization(), "2026-08-29T01:00:00Z"
    )
    replay_at_later_time = replace(command, operation_time="2026-08-29T02:00:00Z")
    omitted = CrmTenantMappingManifest(
        scope, (CrmTenantMappingCompanyEntry("10", (CrmTenantMappingTarget("entity-a"),)),)
    )

    assert command.request_fingerprint == replay_at_later_time.request_fingerprint
    assert command.manifest.digest != omitted.digest


def test_present_boundary_and_rollback_require_exact_current_head() -> None:
    scope = _scope()
    expected = CrmTenantMappingExpectedHead(mapping_head_id(scope), "revision-2", 2, _DIGEST)
    boundary = CrmTenantMappingExpectedHeadBoundary(scope, mapping_head_id(scope), expected)
    rollback = CrmTenantMappingRollbackCommand(
        scope,
        "rollback-request",
        "revision-1",
        _DIGEST,
        boundary,
        _authorization(),
        "2026-08-29T01:00:00Z",
    )

    assert rollback.expected_head_boundary.expected_head == expected
    with pytest.raises(ValueError, match="present current active head"):
        CrmTenantMappingRollbackCommand(
            scope,
            "rollback-request",
            "revision-1",
            _DIGEST,
            CrmTenantMappingExpectedHeadBoundary(scope, mapping_head_id(scope), None),
            _authorization(),
            "2026-08-29T01:00:00Z",
        )


def test_rejection_is_bounded_and_authorization_time_is_inclusive() -> None:
    rejection = CrmTenantMappingRejection("reviewer", "case-304", "bad target")
    authorization = _authorization()

    assert rejection.reason == "bad target"
    assert authorization_is_current(authorization, authorization.authorized_at)
    assert authorization_is_current(authorization, authorization.expires_at)
    assert not authorization_is_current(authorization, "2026-08-30T00:00:01Z")
    with pytest.raises(ValueError, match="bounded"):
        replace(rejection, reason="x" * 513)


def test_rejection_fingerprint_binds_authorization_evidence_but_not_clock() -> None:
    rejection = CrmTenantMappingRejection("reviewer", "case-304", "bad target")
    command = CrmTenantMappingRejectCommand(
        _scope(),
        "revision-a",
        _DIGEST,
        rejection,
        _authorization(),
        "2026-08-29T01:00:00Z",
    )
    changed_authorization = replace(
        command,
        authorization=CrmTenantMappingAuthorization(
            "reviewer",
            "other-approval",
            _DIGEST,
            "2026-08-29T00:00:00Z",
            "2026-08-30T00:00:00Z",
        ),
    )

    replay = replace(command, operation_time="2026-08-29T02:00:00Z")
    assert command.request_fingerprint == replay.request_fingerprint
    assert command.request_fingerprint != changed_authorization.request_fingerprint
