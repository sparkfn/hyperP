"""Focused contract coverage for issue #424's non-executable boundary rebase."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.crm_deal_identity_repair.cli import parse_arguments
from src.crm_deal_identity_repair.control_models import RepairControlRequest, RepairDispatchLease
from src.crm_deal_identity_repair.rebase import (
    RepairBoundaryRebaseRequest,
    RepairBoundaryRebaseResult,
    rebase_audit_digest,
    rebase_hmac,
    rebase_receipt_digest,
    validate_rebase_hmac,
)
from src.graph.queries import crm_deal_identity_repair_rebase as queries

_DIGEST = "sha256:" + "a" * 64


def _request() -> RepairBoundaryRebaseRequest:
    return RepairBoundaryRebaseRequest(
        RepairControlRequest("repair-424", "run-424", "owner-424", "secret-424", 5),
        "approval-424",
        "fresh-artifact-424",
        _DIGEST,
    )


def test_rebase_cli_requires_distinct_fresh_artifact_and_observed_boundary() -> None:
    common = (
        "rebase-boundary",
        "--repair-id",
        "repair-424",
        "--run-id",
        "run-424",
        "--owner-id",
        "owner-424",
        "--expected-revision",
        "5",
        "--approval-id",
        "approval-424",
    )
    with pytest.raises(SystemExit):
        parse_arguments(common)
    with pytest.raises(SystemExit):
        parse_arguments((*common, "--fresh-artifact-id", "fresh-424"))
    parsed = parse_arguments(
        (
            *common,
            "--fresh-artifact-id",
            "fresh-424",
            "--expected-observed-boundary-digest",
            _DIGEST,
        )
    )
    assert parsed.command == "rebase-boundary"
    assert parsed.fresh_artifact_id == "fresh-424"


def test_rebase_audit_and_hmac_bind_old_and_new_authority() -> None:
    request = _request()
    audit = rebase_audit_digest(
        request=request,
        completion_id="completion-424",
        allocation_digest=_DIGEST,
        unit_set_digest=_DIGEST,
        fresh_artifact_manifest_hmac="b" * 64,
        fresh_inventory_digest=_DIGEST,
        fresh_producer_repository_sha="a" * 40,
        fresh_producer_image_digest="sha256:" + "b" * 64,
        previous_boundary_digest=_DIGEST,
        replacement_boundary_digest="sha256:" + "c" * 64,
        replacement_components={
            "source_records_digest": _DIGEST,
            "source_instance_digest": _DIGEST,
            "stale_run_evidence_digest": _DIGEST,
            "control_digest": _DIGEST,
            "inventory_digest": _DIGEST,
            "inventory_row_count": 1,
            "eligible_unit_count": 1,
            "negative_control_count": 0,
        },
        previous_receipt_digest=_DIGEST,
        previous_origin_hmac="d" * 64,
        revision=6,
    )
    signature = rebase_hmac(secret=b"approval-secret", key_id="approval-key", audit_digest=audit)
    validate_rebase_hmac(
        secret=b"approval-secret",
        key_id="approval-key",
        audit_digest=audit,
        supplied_hmac=signature,
    )
    with pytest.raises(RuntimeError, match="HMAC"):
        validate_rebase_hmac(
            secret=b"approval-secret",
            key_id="approval-key",
            audit_digest="sha256:" + "e" * 64,
            supplied_hmac=signature,
        )
    assert rebase_receipt_digest(
        request_digest=request.request_digest,
        run_id="run-424",
        completion_id="completion-424",
        previous_boundary_digest=_DIGEST,
        replacement_boundary_digest="sha256:" + "c" * 64,
        revision=6,
    ) != rebase_receipt_digest(
        request_digest=request.request_digest,
        run_id="run-424",
        completion_id="completion-424",
        previous_boundary_digest=_DIGEST,
        replacement_boundary_digest="sha256:" + "f" * 64,
        revision=6,
    )


def test_rebase_result_remains_nonsecret_and_replay_explicit() -> None:
    request = _request()
    result = RepairBoundaryRebaseResult(
        RepairDispatchLease(
            "control-424",
            "run-424",
            "owner-424",
            request.control.token_digest,
            6,
            "allocated",
            _DIGEST,
        ),
        _DIGEST,
        "sha256:" + "c" * 64,
        _DIGEST,
        "sha256:" + "d" * 64,
        True,
    )
    assert result.replayed is True
    assert result.lease.state == "allocated"


def test_rebase_query_is_guarded_and_never_creates_units_or_dispatches() -> None:
    full_query = "\n".join(
        (
            queries.READ_REBASE_REPLAY,
            queries.LOCK_REBASE_AUTHORITY,
            queries.READ_REBASE_GUARDS,
            queries.ADVANCE_REBASE_CONTROL,
            queries.COMMIT_REBASE_BOUNDARY,
        )
    )
    assert "CREATE (allocated:CrmDealRepairUnit" not in full_query
    assert "SET dispatch.blocked = false" not in full_query
    assert "CrmDealRepairMutationResult" in queries.READ_REBASE_GUARDS
    assert "CrmDealRepairVerification" in queries.READ_REBASE_GUARDS
    assert "CrmDealRepairSecondaryDisposition" in queries.READ_REBASE_GUARDS
    assert "CrmDealRepairFence {run_id: $run_id, state: 'claimed'}" in queries.READ_REBASE_GUARDS
    assert "stored_unit_count = completion.unit_count" in queries.READ_REBASE_GUARDS
    assert "completion.rebase_request_digest IS NULL" in queries.COMMIT_REBASE_BOUNDARY
    assert "SET dispatch.repair_run_id = dispatch.repair_run_id" in queries.LOCK_REBASE_AUTHORITY


def test_rebase_repository_uses_bounded_snapshot_and_transaction_local_effective_reader() -> None:
    from src.graph import crm_deal_identity_repair_rebase as repository

    source = Path(repository.__file__).read_text(encoding="utf-8")
    assert "status_snapshot_from_transaction" in source
    assert "_snapshot_from_transaction" not in source
    assert "def effective_boundary_digest_from_transaction" in source
    assert "return self._client.execute_read" in source
