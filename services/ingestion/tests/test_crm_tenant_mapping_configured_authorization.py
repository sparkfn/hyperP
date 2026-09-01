"""Exact configured grant authorization coverage for #307 mapping mutations."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from src.crm_tenant_mapping_configured_authorization import (
    ConfiguredCrmTenantMappingAuthorizer,
    CrmTenantMappingConfiguredGrant,
)
from src.crm_tenant_mapping_contracts import (
    CrmTenantMappingAuthorization,
    CrmTenantMappingExpectedHead,
    CrmTenantMappingScope,
)
from src.crm_tenant_mapping_identity import mapping_head_id
from src.crm_tenant_mapping_models import (
    CrmTenantMappingAuthorizationError,
    CrmTenantMappingAuthorizationRequest,
    CrmTenantMappingExpectedHeadBoundary,
)

_DIGEST = "sha256:" + "a" * 64
_SCOPE = CrmTenantMappingScope("bitrix_chat", "portal-a", "control-a")
_HEAD = CrmTenantMappingExpectedHead(mapping_head_id(_SCOPE), "revision-a", 3, _DIGEST)


def _request() -> CrmTenantMappingAuthorizationRequest:
    return CrmTenantMappingAuthorizationRequest(
        "prepare",
        _SCOPE,
        "prepare-a",
        None,
        _DIGEST,
        ("entity-a",),
        CrmTenantMappingExpectedHeadBoundary(_SCOPE, mapping_head_id(_SCOPE), _HEAD),
        CrmTenantMappingAuthorization(
            "reviewer",
            "case-a",
            _DIGEST,
            "2026-09-01T00:00:00Z",
            "2099-01-01T00:00:00Z",
        ),
        "2026-09-01T01:00:00Z",
    )


def _grant() -> CrmTenantMappingConfiguredGrant:
    return CrmTenantMappingConfiguredGrant(
        "prepare",
        "bitrix_chat",
        "portal-a",
        "control-a",
        "prepare-a",
        _DIGEST,
        ("entity-a",),
        _HEAD,
        None,
        None,
        None,
        "reviewer",
        "case-a",
        _DIGEST,
        datetime(2099, 1, 1, tzinfo=UTC),
    )


def test_exact_configured_grant_allows_only_the_complete_prepare_operation() -> None:
    authorizer = ConfiguredCrmTenantMappingAuthorizer(
        (_grant(),), clock=lambda: datetime(2026, 9, 1, tzinfo=UTC)
    )

    authorizer.authorize(_request())


@pytest.mark.parametrize(
    "change",
    [
        lambda request: request.__class__(
            request.action,
            request.scope,
            "prepare-b",
            request.revision_id,
            request.manifest_digest,
            request.target_entity_keys,
            request.expected_head_boundary,
            request.authorization,
            request.operation_time,
        ),
        lambda request: request.__class__(
            request.action,
            request.scope,
            request.preparation_request_id,
            request.revision_id,
            request.manifest_digest,
            ("entity-b",),
            request.expected_head_boundary,
            request.authorization,
            request.operation_time,
        ),
    ],
)
def test_configured_grant_rejects_any_non_exact_operation(
    change: Callable[[CrmTenantMappingAuthorizationRequest], CrmTenantMappingAuthorizationRequest],
) -> None:
    request = _request()
    changed = change(request)
    authorizer = ConfiguredCrmTenantMappingAuthorizer(
        (_grant(),), clock=lambda: datetime(2026, 9, 1, tzinfo=UTC)
    )

    with pytest.raises(CrmTenantMappingAuthorizationError, match="no current exact"):
        authorizer.authorize(changed)


def test_configured_grant_fails_closed_when_expired() -> None:
    authorizer = ConfiguredCrmTenantMappingAuthorizer(
        (_grant(),), clock=lambda: datetime(2100, 1, 1, tzinfo=UTC)
    )

    with pytest.raises(CrmTenantMappingAuthorizationError, match="no current exact"):
        authorizer.authorize(_request())
