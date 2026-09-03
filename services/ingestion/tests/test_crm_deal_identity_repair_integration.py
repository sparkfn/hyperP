from __future__ import annotations

from pathlib import Path

import pytest
from src.crm_deal_identity_repair.cli import parse_arguments
from src.crm_deal_identity_repair.control_models import RepairControlRequest
from src.crm_deal_identity_repair.integration_models import RepairIntegrationRequest
from src.graph.queries import crm_deal_identity_repair_integration as integration_queries


def _control() -> RepairControlRequest:
    return RepairControlRequest("repair", "run", "owner", "secret", 0)


def test_request_never_serializes_rollback_secret() -> None:
    request = RepairIntegrationRequest(
        "rollback-status",
        _control(),
        "approval",
        "unit",
        "ticket",
        "mutation:image",
    )
    payload = request.to_dict()
    forbidden = "authorization" + "_token"
    assert forbidden not in payload
    assert "authorization_token_digest" not in payload


def test_nonrollback_rejects_rollback_evidence() -> None:
    with pytest.raises(ValueError, match="rollback evidence"):
        RepairIntegrationRequest("apply", _control(), "approval", "unit", "ticket", "prior")


def test_cli_rejects_rollback_evidence_for_apply() -> None:
    with pytest.raises(SystemExit):
        parse_arguments(
            [
                "apply",
                "--repair-id",
                "repair",
                "--run-id",
                "run",
                "--owner-id",
                "owner",
                "--expected-revision",
                "0",
                "--approval-id",
                "approval",
                "--unit-id",
                "unit",
                "--authorization-reference",
                "ticket",
            ]
        )


def test_cli_rejects_unit_for_accept() -> None:
    with pytest.raises(SystemExit):
        parse_arguments(
            [
                "accept",
                "--repair-id",
                "repair",
                "--run-id",
                "run",
                "--owner-id",
                "owner",
                "--expected-revision",
                "0",
                "--approval-id",
                "approval",
                "--unit-id",
                "unit",
            ]
        )


@pytest.mark.parametrize("operation", ("apply", "verify", "accept", "release-dispatch"))
def test_cli_rejects_rollback_only_fields_for_every_nonrollback_command(operation: str) -> None:
    args = [
        operation,
        "--repair-id",
        "repair",
        "--run-id",
        "run",
        "--owner-id",
        "owner",
        "--expected-revision",
        "0",
        "--approval-id",
        "approval",
        "--authorization-reference",
        "ticket",
        "--predecessor-transition-id",
        "prior",
    ]
    if operation in {"apply", "verify"}:
        args.extend(("--unit-id", "unit"))
    with pytest.raises(SystemExit):
        parse_arguments(args)


def test_cli_rejects_missing_rollback_predecessor() -> None:
    with pytest.raises(SystemExit):
        parse_arguments(
            [
                "rollback-status",
                "--repair-id",
                "repair",
                "--run-id",
                "run",
                "--owner-id",
                "owner",
                "--expected-revision",
                "0",
                "--approval-id",
                "approval",
                "--unit-id",
                "unit",
                "--authorization-reference",
                "ticket",
            ]
        )


def test_apply_replay_and_post_apply_reads_are_not_limited_to_allocated_state() -> None:
    claimed_unit = integration_queries.CLAIM_ADMITTED_FENCE.split("OPTIONAL MATCH (accepted", 1)[0]
    assert (
        "inventory_binding_digest: $inventory_binding_digest, state: 'allocated'"
        not in claimed_unit
    )
    read_unit = integration_queries.READ_FENCE.split("MATCH (fence", 1)[0]
    allocated_match = "inventory_binding_digest: $inventory_binding_digest, state: 'allocated'"
    assert allocated_match not in read_unit
    assert (
        "unit.state = 'allocated' AND size(stored) = 0" in integration_queries.CLAIM_ADMITTED_FENCE
    )


def test_acceptance_uses_exact_callback_inputs_and_persists_receipt_digest() -> None:
    query = integration_queries.ACCEPT_AND_RELEASE
    assert "computed_allocation_unit_set_digest" in query
    assert "acceptance.receipt_digest" in query
    assert "SET dispatch.blocked = false" not in query


def test_release_authority_allows_exact_receipt_replay_without_dispatch_mutation() -> None:
    query = integration_queries.READ_RELEASE_AUTHORITY
    assert "release IS NOT NULL OR dispatch IS NOT NULL" in query
    assert "CrmDealRepairDispatchRelease" in query


def test_rollback_authorization_identity_is_operation_independent() -> None:
    source = Path("services/ingestion/src/graph/crm_deal_identity_repair_integration.py").read_text(
        encoding="utf-8"
    )
    assert "rollback-authorization-v1" in source
    assert '"authorization_transition_id": request.request_digest' not in source
