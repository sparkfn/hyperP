"""Strict JSON boundary coverage for CRM tenant operator handlers."""

from __future__ import annotations

import json

import pytest
from src.crm_tenant_operator_tasks import parse_prepare, parse_project, parse_rollback

_DIGEST = "sha256:" + "a" * 64
_AUTH = {
    "actor": "operator",
    "authorization_reference": "ticket-1",
    "authorization_digest": _DIGEST,
    "authorized_at": "2026-09-01T00:00:00Z",
    "expires_at": "2026-09-02T00:00:00Z",
}
_SCOPE = {
    "source_key": "bitrix_chat",
    "source_instance_id": "portal-a",
    "control_instance_id": "default",
}
_HEAD = {"head_id": "bad", "expected_head": None}


def _prepare() -> dict[str, object]:
    return {
        "scope": _SCOPE,
        "preparation_request_id": "prepare-1",
        "manifest": {"entries": []},
        "expected_head_boundary": _HEAD,
        "authorization": _AUTH,
        "operation_time": "2026-09-01T00:00:00Z",
    }


def test_prepare_json_round_trip_is_strict() -> None:
    raw = json.loads(json.dumps(_prepare()))
    # deterministic head identifier is deliberately supplied from the real boundary contract.
    from src.crm_tenant_mapping_contracts import CrmTenantMappingScope
    from src.crm_tenant_mapping_identity import mapping_head_id

    raw["expected_head_boundary"]["head_id"] = mapping_head_id(
        CrmTenantMappingScope("bitrix_chat", "portal-a", "default")
    )
    assert parse_prepare(raw).preparation_request_id == "prepare-1"
    raw["extra"] = True
    with pytest.raises(ValueError, match="fields"):
        parse_prepare(raw)


def test_rollback_and_project_reject_malformed_payloads() -> None:
    with pytest.raises(ValueError):
        parse_rollback({})
    with pytest.raises(ValueError):
        parse_project({})


def test_successful_rollback_and_project_json_round_trips() -> None:
    from src.crm_tenant_mapping_contracts import CrmTenantMappingScope
    from src.crm_tenant_mapping_identity import mapping_head_id

    scope = CrmTenantMappingScope("bitrix_chat", "portal-a", "default")
    head = {
        "head_id": mapping_head_id(scope),
        "expected_head": {
            "head_id": mapping_head_id(scope),
            "active_revision_id": "revision-current",
            "active_revision_number": 2,
            "active_manifest_digest": _DIGEST,
        },
    }
    rollback = {
        "scope": _SCOPE,
        "preparation_request_id": "rollback-1",
        "rollback_of_revision_id": "revision-old",
        "rollback_of_manifest_digest": _DIGEST,
        "expected_head_boundary": head,
        "authorization": _AUTH,
        "operation_time": "2026-09-01T00:00:00Z",
    }
    assert parse_rollback(json.loads(json.dumps(rollback))).preparation_request_id == "rollback-1"
    project = {
        "scope": _SCOPE,
        "request_id": "project-1",
        "source_census_id": "source-1",
        "source_census_fingerprint": _DIGEST,
        "mapping_revision_id": "revision-current",
        "mapping_manifest_digest": _DIGEST,
        "expected_mapping_head_boundary": head,
        "expected_prior_head": None,
        "page_limit": 10,
    }
    assert parse_project(json.loads(json.dumps(project))).request_id == "project-1"
